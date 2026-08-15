"""Paging guarantees for the longitudinal record.

``RecordService.assemble`` used to read each of a patient's five collections in full. The
response size was therefore a function of how long someone had been a patient, which is the
one variable a clinical record is guaranteed to grow in.

Bounding it introduces two failure modes that a naive LIMIT/OFFSET has and a whole-set read
cannot, and most of this file is about those rather than about the cap itself:

* **A page boundary that lies.** LIMIT/OFFSET over a sort with ties is not a partition of the
  set: rows that compare equal may come back in a different order between two requests, so
  walking the pages can serve a row twice and skip another entirely. Labs are the realistic
  case -- a panel drawn in one sitting shares a ``sample_date`` across every marker in it, and
  a repeat of the same marker shares the name too.
* **A short page that reads as the end of the data.** A clinician who is shown 100 of 4000
  labs and told nothing must not conclude they have seen the history. Every section therefore
  carries a counted total and an explicit ``has_more``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.services.record_service import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    REASONING_SNAPSHOT_LIMIT,
    RecordService,
)
from tests.conftest import create_patient

pytestmark = pytest.mark.asyncio


async def _patient(db) -> Patient:
    from app.models.user import Account

    account_id = (await db.execute(select(Account.id))).scalars().first()
    if account_id is None:
        account = Account(
            email="record-paging@example.com",
            password_hash="x",
            display_name="Dr Paging",
        )
        db.add(account)
        await db.flush()
        account_id = account.id
    patient = Patient(
        account_id=account_id,
        full_name="Paging Patient",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


async def _add_labs(db, patient_id: uuid.UUID, n: int, **overrides) -> None:
    """``n`` labs that differ only in marker name, unless overridden."""
    for i in range(n):
        db.add(
            LabResult(
                patient_id=patient_id,
                marker_name=overrides.get("marker_name", f"Marker{i:04d}"),
                value_numeric=i,
                sample_date=overrides.get("sample_date", datetime(2026, 1, 1, tzinfo=UTC)),
            )
        )
    await db.commit()


# ----------------------------------------------------------------------------------------
# The cap itself.
# ----------------------------------------------------------------------------------------


async def test_a_long_lab_history_is_capped_at_the_default_limit(db):
    """The regression this whole change exists for: the read no longer scales with the chart."""
    patient = await _patient(db)
    await _add_labs(db, patient.id, DEFAULT_PAGE_LIMIT + 37)

    record = await RecordService(db).assemble(patient.id)

    assert len(record.lab_results) == DEFAULT_PAGE_LIMIT
    assert record.pagination.lab_results.total == DEFAULT_PAGE_LIMIT + 37
    assert record.pagination.lab_results.has_more is True


async def test_every_section_is_capped_not_only_labs(db):
    """Labs are the fastest-growing set, but nothing here is bounded by clinical reality.

    Medication events accumulate per prescription ingested and are append-only, so a chart with
    thousands of them is a long chart, not a corrupt one. A cap on labs alone would move the
    unbounded read rather than remove it.
    """
    patient = await _patient(db)
    limit = 3
    for i in range(limit + 2):
        db.add(
            MedicationEvent(
                patient_id=patient.id,
                generic_name=f"Drug{i}",
                event_type="start",
                is_current=True,
                event_date=date(2026, 1, 1),
            )
        )
        db.add(Condition(patient_id=patient.id, condition_name=f"Condition{i}", status="active"))
        db.add(
            Allergy(
                patient_id=patient.id,
                allergen_name=f"Allergen{i}",
                allergen_type="drug",
                status="active",
            )
        )
        db.add(
            DerivedMarker(
                patient_id=patient.id,
                marker_name=f"Derived{i}",
                # Offset off zero: derived markers are strictly positive by construction
                # (ck_derived_markers_value_positive). The value is filler for a paging test,
                # so the row just has to be one this table would really hold.
                value_numeric=i + 1,
                formula_name="egfr_ckd_epi",
                formula_version="2021",
                input_values={},
                computed_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    await _add_labs(db, patient.id, limit + 2)

    record = await RecordService(db).assemble(patient.id, limit=limit)

    for section in ("medications", "lab_results", "conditions", "allergies", "derived_markers"):
        assert len(getattr(record, section)) == limit, section
        meta = getattr(record.pagination, section)
        assert meta.total == limit + 2, section
        assert meta.has_more is True, section


async def test_a_short_chart_is_returned_whole_and_says_so(db):
    """The common case must not look truncated: `has_more` false, total equal to the page."""
    patient = await _patient(db)
    await _add_labs(db, patient.id, 3)

    record = await RecordService(db).assemble(patient.id)

    assert len(record.lab_results) == 3
    assert record.pagination.lab_results.total == 3
    assert record.pagination.lab_results.has_more is False


# ----------------------------------------------------------------------------------------
# Page boundaries.
# ----------------------------------------------------------------------------------------


async def test_paging_a_tied_sort_serves_every_row_exactly_once(db):
    """The tiebreaker, tested where it actually bites.

    Twenty labs sharing one sample date *and* one marker name — a single repeated assay, which
    is what a monitored patient's potassium looks like. Every column in the ORDER BY except the
    primary key compares equal, so without that final key the sort order is whatever the
    planner happens to emit and each page is drawn from an independently-ordered set. Walking
    one row at a time is the sharpest form of the test: 20 pages, 20 chances to repeat a row.
    """
    patient = await _patient(db)
    await _add_labs(
        db,
        patient.id,
        20,
        marker_name="Potassium",
        sample_date=datetime(2026, 3, 3, tzinfo=UTC),
    )

    service = RecordService(db)
    seen: list[uuid.UUID] = []
    for offset in range(20):
        page = await service.assemble(patient.id, limit=1, offset=offset)
        assert len(page.lab_results) == 1, offset
        seen.append(page.lab_results[0].id)

    assert len(set(seen)) == 20, "a row was served on two different pages"
    stored = (
        (await db.execute(select(LabResult.id).where(LabResult.patient_id == patient.id)))
        .scalars()
        .all()
    )
    assert set(seen) == set(stored), "walking every page did not cover the whole set"


async def test_the_page_walk_is_repeatable(db):
    """Same request, same rows. A total order is only useful if it is the *same* total order."""
    patient = await _patient(db)
    await _add_labs(
        db, patient.id, 12, marker_name="Creatinine", sample_date=datetime(2026, 4, 1, tzinfo=UTC)
    )

    service = RecordService(db)
    first = await service.assemble(patient.id, limit=5, offset=5)
    second = await service.assemble(patient.id, limit=5, offset=5)

    assert [lab.id for lab in first.lab_results] == [lab.id for lab in second.lab_results]


async def test_an_exactly_full_last_page_is_not_reported_as_having_more(db):
    """`has_more` is computed against the total, not from `len(page) == limit`.

    Ten rows read five at a time: the second page is full and is also the last. A client that
    trusted a full page to mean "there is more" would fetch an empty third one; one that
    trusted it on a *record* would tell a clinician there are labs it cannot show.
    """
    patient = await _patient(db)
    await _add_labs(db, patient.id, 10)

    page = await RecordService(db).assemble(patient.id, limit=5, offset=5)

    assert len(page.lab_results) == 5
    assert page.pagination.lab_results.total == 10
    assert page.pagination.lab_results.has_more is False


async def test_an_offset_past_the_end_is_empty_and_final(db):
    """Not an error, and specifically not `has_more` true — the pathology of the `len == limit`
    shortcut in the other direction, where an empty page would claim more was coming."""
    patient = await _patient(db)
    await _add_labs(db, patient.id, 4)

    page = await RecordService(db).assemble(patient.id, limit=10, offset=99)

    assert page.lab_results == []
    assert page.pagination.lab_results.total == 4
    assert page.pagination.lab_results.has_more is False


async def test_sections_page_independently_so_a_deep_offset_empties_the_short_ones(db):
    """The documented consequence of one offset across five collections.

    There is no hundredth allergy on any real chart, so paging into a long lab history returns
    the short sections empty. That is the shape of the resource rather than a bug, and the
    per-section totals are what let a client tell the two apart.
    """
    patient = await _patient(db)
    db.add(
        Allergy(
            patient_id=patient.id, allergen_name="Penicillin", allergen_type="drug", status="active"
        )
    )
    await _add_labs(db, patient.id, 30)

    page = await RecordService(db).assemble(patient.id, limit=10, offset=10)

    assert len(page.lab_results) == 10
    assert page.allergies == []
    # Empty because it was paged past, not because the patient has no allergies.
    assert page.pagination.allergies.total == 1
    assert page.pagination.allergies.has_more is False


# ----------------------------------------------------------------------------------------
# Truthfulness of the totals.
# ----------------------------------------------------------------------------------------


async def test_soft_deleted_rows_are_absent_from_the_page_and_from_the_total(db):
    """The count is derived from the same statement that pages, so the two cannot disagree.

    A total rebuilt from the model by hand is where the soft-delete predicate gets dropped, and
    the symptom is quiet: a chart that reports more labs than it will ever serve, and a client
    that pages forever looking for the missing ones.
    """
    patient = await _patient(db)
    await _add_labs(db, patient.id, 6)
    rows = (
        (await db.execute(select(LabResult).where(LabResult.patient_id == patient.id)))
        .scalars()
        .all()
    )
    for row in rows[:4]:
        row.is_deleted = True
    await db.commit()

    record = await RecordService(db).assemble(patient.id)

    assert len(record.lab_results) == 2
    assert record.pagination.lab_results.total == 2
    assert record.pagination.lab_results.has_more is False


async def test_another_patients_rows_are_not_counted(db):
    """Paging metadata is a read of the chart and is scoped like one."""
    patient = await _patient(db)
    other = await _patient(db)
    await _add_labs(db, patient.id, 3)
    await _add_labs(db, other.id, 9)

    record = await RecordService(db).assemble(patient.id)

    assert record.pagination.lab_results.total == 3


async def test_ordering_survived_the_tiebreaker(db):
    """The primary key is last in the ORDER BY, so it decides ties and nothing else.

    Worth pinning next to the paging tests: a tiebreaker inserted anywhere but last silently
    re-sorts the record into UUID order, which no test of *counts* would catch.
    """
    patient = await _patient(db)
    db.add_all(
        [
            LabResult(patient_id=patient.id, marker_name="HbA1c", sample_date=None),
            LabResult(
                patient_id=patient.id,
                marker_name="Creatinine",
                sample_date=datetime(2026, 1, 5, tzinfo=UTC),
            ),
            LabResult(
                patient_id=patient.id,
                marker_name="Potassium",
                sample_date=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        ]
    )
    await db.commit()

    record = await RecordService(db).assemble(patient.id)

    assert [lab.marker_name for lab in record.lab_results] == [
        "Potassium",
        "Creatinine",
        "HbA1c",
    ]


# ----------------------------------------------------------------------------------------
# The endpoint.
# ----------------------------------------------------------------------------------------


async def _seed_labs_via_api(db, auth_client: AsyncClient, n: int) -> dict:
    patient = await create_patient(auth_client, full_name="Endpoint Paging")
    await _add_labs(db, uuid.UUID(patient["id"]), n)
    return patient


async def test_the_endpoint_defaults_to_the_service_default(db, auth_client):
    patient = await _seed_labs_via_api(db, auth_client, DEFAULT_PAGE_LIMIT + 5)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/record")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["lab_results"]) == DEFAULT_PAGE_LIMIT
    assert body["pagination"]["lab_results"] == {
        "total": DEFAULT_PAGE_LIMIT + 5,
        "limit": DEFAULT_PAGE_LIMIT,
        "offset": 0,
        "has_more": True,
    }


async def test_the_endpoint_honours_limit_and_offset(db, auth_client):
    patient = await _seed_labs_via_api(db, auth_client, 12)

    first = await auth_client.get(f"/api/v1/patients/{patient['id']}/record?limit=5&offset=0")
    second = await auth_client.get(f"/api/v1/patients/{patient['id']}/record?limit=5&offset=5")

    assert first.status_code == 200 and second.status_code == 200
    ids = [lab["id"] for lab in first.json()["lab_results"]]
    assert len(ids) == 5
    assert not set(ids) & {lab["id"] for lab in second.json()["lab_results"]}
    assert second.json()["pagination"]["lab_results"]["has_more"] is True


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=-1",
        f"limit={MAX_PAGE_LIMIT + 1}",
        "offset=-1",
        "limit=notanumber",
    ],
)
async def test_the_endpoint_rejects_out_of_range_paging(db, auth_client, query):
    """The ceiling is part of the contract: without it the cap is advisory and a caller can ask
    for the unbounded read back."""
    patient = await _seed_labs_via_api(db, auth_client, 2)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/record?{query}")

    assert resp.status_code == 422, resp.text


async def test_the_maximum_limit_is_accepted(db, auth_client):
    """The boundary itself, so `le=` can never be off by one against the service constant."""
    patient = await _seed_labs_via_api(db, auth_client, 2)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/record?limit={MAX_PAGE_LIMIT}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["pagination"]["lab_results"]["limit"] == MAX_PAGE_LIMIT


async def test_paging_the_record_is_still_audited_as_a_phi_read(db, auth_client):
    """A page of a chart is a disclosure of that chart. Paging must not become a way to read a
    record without leaving the `patient_record_viewed` entry behind."""
    from app.models.audit_log import AuditLog

    patient = await _seed_labs_via_api(db, auth_client, 20)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/record?limit=1&offset=15")
    assert resp.status_code == 200, resp.text

    actions = (
        (
            await db.execute(
                select(AuditLog.action).where(AuditLog.patient_id == uuid.UUID(patient["id"]))
            )
        )
        .scalars()
        .all()
    )
    assert "patient_record_viewed" in actions


# ----------------------------------------------------------------------------------------
# The reasoning snapshot.
# ----------------------------------------------------------------------------------------


async def test_the_reasoning_snapshot_takes_the_wider_bound(db):
    """The agents' view of the patient is not sized by what fits on a screen.

    Pinned because the default is the easy thing to inherit here, and inheriting it would
    quietly narrow the evidence a differential is built from without changing any test that
    looks at the record endpoint.
    """
    from app.services.reasoning_service import ReasoningService

    assert REASONING_SNAPSHOT_LIMIT > DEFAULT_PAGE_LIMIT
    patient = await _patient(db)
    await _add_labs(db, patient.id, DEFAULT_PAGE_LIMIT + 20)

    snapshot = await ReasoningService(db)._snapshot(patient.id)  # noqa: SLF001

    assert len(snapshot["lab_results"]) == DEFAULT_PAGE_LIMIT + 20
    assert snapshot["pagination"]["lab_results"]["has_more"] is False


async def test_a_truncated_snapshot_says_so(db):
    """A session run against a chart too long to fit records that fact in `case_state`.

    The bound is real — a chart can exceed it — and the difference between "this is the whole
    history" and "this is as much of it as was fetched" has to survive into the immutable
    session state, not be reconstructable only by re-querying the database later.
    """
    from app.services.reasoning_service import ReasoningService

    patient = await _patient(db)
    await _add_labs(db, patient.id, REASONING_SNAPSHOT_LIMIT + 3)

    snapshot = await ReasoningService(db)._snapshot(patient.id)  # noqa: SLF001

    assert len(snapshot["lab_results"]) == REASONING_SNAPSHOT_LIMIT
    assert snapshot["pagination"]["lab_results"]["has_more"] is True
    assert snapshot["pagination"]["lab_results"]["total"] == REASONING_SNAPSHOT_LIMIT + 3
