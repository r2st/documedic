"""The critical-value screen finally has a reader, and something that closes the loop.

``app.core.lab_safety`` has detected panic values correctly for several rounds. What it never
had was anybody to tell. Every surfacing of a critical value — the screen that runs on document
approval, the ``critical_lab_value_detected`` audit entry, ``GET ../labs/critical-flags`` —
requires somebody to already have that patient's chart open. A potassium of 6.8 extracted from
a report uploaded at 2am wrote an audit entry nobody reads and set a flag on a chart nobody is
looking at.

Two things are tested here:

* the **queue** — panel-wide, so the question "who on my list has a dangerous result right now"
  can be asked without guessing which chart to open;
* the **acknowledgement** — the only thing that takes an entry off it. No expiry, no
  auto-dismiss, nothing inferred from having viewed the chart: a queue that empties itself can
  be empty because nobody looked, and that is the failure this whole feature exists to prevent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.critical_lab_acknowledgement import CriticalLabAcknowledgement
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.models.user import Account
from app.services.lab_safety_service import LabSafetyService

pytestmark = pytest.mark.asyncio

# Curated thresholds from app.core.lab_safety: potassium panics above 6.5 mmol/L, glucose below
# 40 mg/dL. Both are named in the round's brief, and both are already in the table.
PANIC_K = 7.4
NORMAL_K = 4.0
PANIC_GLUCOSE = 32.0


async def _account(db, email: str | None = None) -> Account:
    account = Account(
        email=email or f"queue-{uuid.uuid4().hex}@example.com",
        password_hash="x",
        display_name="Dr Queue",
    )
    db.add(account)
    await db.flush()
    return account


async def _patient(db, account: Account, name: str = "Queue Patient") -> Patient:
    patient = Patient(
        account_id=account.id,
        full_name=name,
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


async def _lab(
    db,
    patient: Patient,
    marker: str = "Potassium",
    value: float = PANIC_K,
    unit: str = "mmol/L",
    when: datetime | None = None,
) -> LabResult:
    row = LabResult(
        patient_id=patient.id,
        marker_name=marker,
        value_numeric=value,
        unit=unit,
        sample_date=when or datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    return row


# --- The queue ----------------------------------------------------------------------------------


async def test_a_panic_value_on_a_chart_nobody_has_open_appears_on_the_queue(db):
    """The whole point. Nothing here opens the patient's chart or names the patient."""
    account = await _account(db)
    patient = await _patient(db, account)
    await _lab(db, patient)

    queue = await LabSafetyService(db).outstanding_critical_values(account_id=account.id)
    assert len(queue.entries) == 1
    lab, flag = queue.entries[0]
    assert lab.patient_id == patient.id
    assert flag.severity == "panic_high"


async def test_the_queue_spans_every_patient_on_the_panel(db):
    account = await _account(db)
    first = await _patient(db, account, "Patient One")
    second = await _patient(db, account, "Patient Two")
    await _lab(db, first, "Potassium", PANIC_K)
    await _lab(db, second, "Glucose", PANIC_GLUCOSE, unit="mg/dL")

    queue = await LabSafetyService(db).outstanding_critical_values(account_id=account.id)
    assert {lab.patient_id for lab, _f in queue.entries} == {first.id, second.id}


async def test_a_normal_value_is_not_on_the_queue(db):
    account = await _account(db)
    patient = await _patient(db, account)
    await _lab(db, patient, value=NORMAL_K)

    queue = await LabSafetyService(db).outstanding_critical_values(account_id=account.id)
    assert queue.entries == []


async def test_another_accounts_panic_value_is_not_on_this_queue(db):
    mine = await _account(db)
    theirs = await _account(db)
    await _lab(db, await _patient(db, theirs))

    queue = await LabSafetyService(db).outstanding_critical_values(account_id=mine.id)
    assert queue.entries == []


async def test_a_withdrawn_chart_drops_off_the_queue(db):
    """Consent withdrawal means this record is not to be worked from.

    A queue entry is an instruction to go and work from it, so it has to follow the same rule as
    every other read surface rather than being an exception nobody thought about.
    """
    account = await _account(db)
    patient = await _patient(db, account)
    await _lab(db, patient)
    patient.is_deleted = True
    await db.flush()

    queue = await LabSafetyService(db).outstanding_critical_values(account_id=account.id)
    assert queue.entries == []


async def test_only_the_latest_result_per_marker_is_queued(db):
    """A panic potassium that has since been repeated and come back normal is not outstanding.

    The queue must pick the same "latest per marker" row the chart screen does, or a clinician
    opening the chart from a queue entry sees something the queue does not.
    """
    account = await _account(db)
    patient = await _patient(db, account)
    await _lab(db, patient, value=PANIC_K, when=datetime(2026, 1, 1, tzinfo=UTC))
    await _lab(db, patient, value=NORMAL_K, when=datetime(2026, 6, 1, tzinfo=UTC))

    queue = await LabSafetyService(db).outstanding_critical_values(account_id=account.id)
    assert queue.entries == []


async def test_a_newly_critical_repeat_is_queued_even_though_an_older_one_was_normal(db):
    account = await _account(db)
    patient = await _patient(db, account)
    await _lab(db, patient, value=NORMAL_K, when=datetime(2026, 1, 1, tzinfo=UTC))
    await _lab(db, patient, value=PANIC_K, when=datetime(2026, 6, 1, tzinfo=UTC))

    queue = await LabSafetyService(db).outstanding_critical_values(account_id=account.id)
    assert len(queue.entries) == 1


async def test_a_truncated_queue_says_so(db):
    """A safety queue that quietly stops at n is indistinguishable from one with n entries."""
    account = await _account(db)
    patient = await _patient(db, account)
    for i in range(4):
        await _lab(db, patient, marker=f"Potassium {i}", value=PANIC_K)

    queue = await LabSafetyService(db).outstanding_critical_values(account_id=account.id, limit=2)
    assert queue.truncated is True
    assert len(queue.entries) <= 2


async def test_an_untruncated_queue_says_that_too(db):
    account = await _account(db)
    patient = await _patient(db, account)
    await _lab(db, patient)

    queue = await LabSafetyService(db).outstanding_critical_values(account_id=account.id, limit=50)
    assert queue.truncated is False


# --- Acknowledgement ----------------------------------------------------------------------------


async def test_acknowledging_takes_the_entry_off_the_queue(db):
    account = await _account(db)
    patient = await _patient(db, account)
    lab = await _lab(db, patient)
    service = LabSafetyService(db)

    assert len((await service.outstanding_critical_values(account_id=account.id)).entries) == 1
    await service.acknowledge(
        account_id=account.id,
        patient_id=patient.id,
        lab_result_id=lab.id,
        acknowledged_by="Dr Rao",
        action_note="Repeat sample sent; patient recalled.",
    )
    queue = await service.outstanding_critical_values(account_id=account.id)
    assert queue.entries == []
    assert queue.acknowledged_count == 1, (
        "an empty queue must distinguish 'nothing outstanding' from 'nothing critical'"
    )


async def test_the_acknowledgement_records_the_value_that_was_on_the_screen(db):
    """Not only a reference to the row.

    A lab row can be superseded by a corrected result from the same document, and an
    acknowledgement that only pointed at the row would then read as though the clinician had
    seen the corrected figure.
    """
    account = await _account(db)
    patient = await _patient(db, account)
    lab = await _lab(db, patient)

    row = await LabSafetyService(db).acknowledge(
        account_id=account.id,
        patient_id=patient.id,
        lab_result_id=lab.id,
        acknowledged_by="Dr Rao",
        action_note=None,
    )
    assert float(row.value) == PANIC_K
    assert row.marker_name == "Potassium"
    assert row.severity == "panic_high"
    assert row.acknowledged_by == "Dr Rao"


async def test_a_normal_result_cannot_be_acknowledged(db):
    """An acknowledgement is a clinical attestation, so one against a normal value was never true.

    It also catches the client bug that matters most — acknowledging by the wrong id, which
    would otherwise silently clear a *different* dangerous value off the queue.
    """
    from app.exceptions import ValidationError

    account = await _account(db)
    patient = await _patient(db, account)
    lab = await _lab(db, patient, value=NORMAL_K)

    with pytest.raises(ValidationError):
        await LabSafetyService(db).acknowledge(
            account_id=account.id,
            patient_id=patient.id,
            lab_result_id=lab.id,
            acknowledged_by="Dr Rao",
            action_note=None,
        )


async def test_a_lab_belonging_to_a_different_patient_cannot_be_acknowledged(db):
    """The path names a patient and a result; they have to be the same chart."""
    from app.exceptions import NotFoundError

    account = await _account(db)
    mine = await _patient(db, account, "Mine")
    other = await _patient(db, account, "Other")
    lab = await _lab(db, other)

    with pytest.raises(NotFoundError):
        await LabSafetyService(db).acknowledge(
            account_id=account.id,
            patient_id=mine.id,
            lab_result_id=lab.id,
            acknowledged_by="Dr Rao",
            action_note=None,
        )


async def test_another_accounts_result_cannot_be_acknowledged(db):
    from app.exceptions import NotFoundError

    mine = await _account(db)
    theirs = await _account(db)
    patient = await _patient(db, theirs)
    lab = await _lab(db, patient)

    with pytest.raises(NotFoundError):
        await LabSafetyService(db).acknowledge(
            account_id=mine.id,
            patient_id=patient.id,
            lab_result_id=lab.id,
            acknowledged_by="Dr Rao",
            action_note=None,
        )


async def test_acknowledging_twice_writes_a_second_row_and_edits_nothing(db):
    """Append-only, like clinical_suggestions and drug_safety_overrides (Rule #7).

    A mistaken acknowledgement is corrected by acknowledging again; both attestations stay on
    the record, because a record of a clinical decision that can be rewritten is not a record
    of what was decided.
    """
    account = await _account(db)
    patient = await _patient(db, account)
    lab = await _lab(db, patient)
    service = LabSafetyService(db)

    for name in ("Dr Rao", "Dr Iyer"):
        await service.acknowledge(
            account_id=account.id,
            patient_id=patient.id,
            lab_result_id=lab.id,
            acknowledged_by=name,
            action_note=None,
        )
    rows = (
        (
            await db.execute(
                select(CriticalLabAcknowledgement).where(
                    CriticalLabAcknowledgement.lab_result_id == lab.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(r.acknowledged_by for r in rows) == ["Dr Iyer", "Dr Rao"]
    assert not hasattr(CriticalLabAcknowledgement, "updated_at"), "append-only"
    assert not hasattr(CriticalLabAcknowledgement, "is_deleted"), "append-only"


async def test_a_later_critical_result_for_the_same_marker_returns_to_the_queue(db):
    """Acknowledging today's potassium does not acknowledge tomorrow's.

    Falls out of keying on the lab row rather than on the marker, and is the property that makes
    the queue safe to use twice.
    """
    account = await _account(db)
    patient = await _patient(db, account)
    first = await _lab(db, patient, value=PANIC_K, when=datetime(2026, 1, 1, tzinfo=UTC))
    service = LabSafetyService(db)
    await service.acknowledge(
        account_id=account.id,
        patient_id=patient.id,
        lab_result_id=first.id,
        acknowledged_by="Dr Rao",
        action_note=None,
    )
    assert (await service.outstanding_critical_values(account_id=account.id)).entries == []

    await _lab(db, patient, value=PANIC_K, when=datetime(2026, 6, 1, tzinfo=UTC))
    queue = await service.outstanding_critical_values(account_id=account.id)
    assert len(queue.entries) == 1, "a new dangerous result is a new thing to see"


async def test_the_acknowledgement_audit_entry_carries_the_name_and_not_the_note(db):
    account = await _account(db)
    patient = await _patient(db, account)
    lab = await _lab(db, patient)

    await LabSafetyService(db).acknowledge(
        account_id=account.id,
        patient_id=patient.id,
        lab_result_id=lab.id,
        acknowledged_by="Dr Rao",
        action_note="Spoke to Mrs Kumar; she is coming in now.",
    )
    await db.flush()
    row = (
        (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "critical_lab_value_acknowledged")
            )
        )
        .scalars()
        .one()
    )
    assert row.payload["acknowledged_by"] == "Dr Rao"
    assert row.payload["severity"] == "panic_high"
    assert row.payload["action_note_chars"] == len("Spoke to Mrs Kumar; she is coming in now.")
    assert "Kumar" not in str(row.payload), "clinician prose stays off the immutable trail"


# --- The HTTP surface ---------------------------------------------------------------------------


async def _http_patient_with_panic_lab(auth_client, db) -> tuple[str, LabResult]:
    from tests.conftest import create_patient

    patient = await create_patient(auth_client)
    row = LabResult(
        patient_id=uuid.UUID(patient["id"]),
        marker_name="Potassium",
        value_numeric=PANIC_K,
        unit="mmol/L",
        sample_date=datetime.now(UTC) - timedelta(days=3),
    )
    db.add(row)
    await db.commit()
    return patient["id"], row


async def test_the_queue_route_returns_the_outstanding_value_with_its_age(auth_client, db):
    patient_id, lab = await _http_patient_with_panic_lab(auth_client, db)

    resp = await auth_client.get("/api/v1/labs/critical-queue")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["patient_id"] == patient_id
    assert entry["lab_result_id"] == str(lab.id)
    assert entry["severity"] == "panic_high"
    assert entry["detected_days_ago"] == 3, (
        "the queue's clinical meaning is mostly in how long this has been sitting there"
    )
    assert body["truncated"] is False
    assert body["offline_capable"] is True


async def test_a_sample_taken_today_reports_zero_days_not_null(auth_client, db):
    """0 is fresh, not falsy — an ``or`` in the fallback would have re-aged it silently."""
    from tests.conftest import create_patient

    patient = await create_patient(auth_client)
    db.add(
        LabResult(
            patient_id=uuid.UUID(patient["id"]),
            marker_name="Potassium",
            value_numeric=PANIC_K,
            unit="mmol/L",
            sample_date=datetime.now(UTC),
        )
    )
    await db.commit()

    resp = await auth_client.get("/api/v1/labs/critical-queue")
    assert resp.json()["entries"][0]["detected_days_ago"] == 0


async def test_the_acknowledge_route_clears_the_entry(auth_client, db):
    patient_id, lab = await _http_patient_with_panic_lab(auth_client, db)

    resp = await auth_client.post(
        f"/api/v1/patients/{patient_id}/labs/critical-flags/{lab.id}/acknowledge",
        json={"acknowledged_by": "Dr Rao", "action_note": "Recalled."},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["acknowledged_by"] == "Dr Rao"

    queue = await auth_client.get("/api/v1/labs/critical-queue")
    assert queue.json()["entries"] == []
    assert queue.json()["acknowledged_count"] == 1


async def test_the_acknowledge_route_requires_a_readable_clinician_name(auth_client, db):
    """A single space must not clear a panic value off the queue.

    ``min_length`` counts raw characters, so the floor is applied to the stripped text — the
    same hole that was closed on hard-block override reasoning.
    """
    patient_id, lab = await _http_patient_with_panic_lab(auth_client, db)
    for name in (" ", "", "   "):
        resp = await auth_client.post(
            f"/api/v1/patients/{patient_id}/labs/critical-flags/{lab.id}/acknowledge",
            json={"acknowledged_by": name},
        )
        assert resp.status_code == 422, name


async def test_the_action_note_is_optional(auth_client, db):
    """Required, it becomes a text box between a clinician and clearing a queue at 2am."""
    patient_id, lab = await _http_patient_with_panic_lab(auth_client, db)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient_id}/labs/critical-flags/{lab.id}/acknowledge",
        json={"acknowledged_by": "Dr Rao"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["action_note"] is None


async def test_acknowledging_a_result_that_is_not_critical_is_refused_over_http(auth_client, db):
    from tests.conftest import create_patient

    patient = await create_patient(auth_client)
    row = LabResult(
        patient_id=uuid.UUID(patient["id"]),
        marker_name="Potassium",
        value_numeric=NORMAL_K,
        unit="mmol/L",
        sample_date=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/labs/critical-flags/{row.id}/acknowledge",
        json={"acknowledged_by": "Dr Rao"},
    )
    assert resp.status_code == 422


async def test_another_account_sees_neither_the_queue_entry_nor_the_route(
    auth_client, second_auth_client, db
):
    patient_id, lab = await _http_patient_with_panic_lab(auth_client, db)

    assert (await second_auth_client.get("/api/v1/labs/critical-queue")).json()["entries"] == []
    resp = await second_auth_client.post(
        f"/api/v1/patients/{patient_id}/labs/critical-flags/{lab.id}/acknowledge",
        json={"acknowledged_by": "Dr Other"},
    )
    assert resp.status_code == 404


async def test_the_queue_read_is_audited(auth_client, db):
    await _http_patient_with_panic_lab(auth_client, db)
    await auth_client.get("/api/v1/labs/critical-queue")

    row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "critical_lab_queue_viewed")))
        .scalars()
        .first()
    )
    assert row is not None, "reading it discloses lab values across the whole panel"
    assert row.payload["outstanding"] == 1
    assert row.patient_id is None, "the read is not about one chart"
