"""The two bounds on the prescribing-history read, and why they are two.

``GET ../medications/timeline`` was the one whole-chart read in this API with no bound in either
direction: every medication event the chart held was loaded, grouped and serialised into a single
response. That is fine for the twelve-event chart it was written against and wrong as a *shape* —
a patient with twenty years of prescribing, or one whose list arrived through the bulk importer,
is the same route returning megabytes assembled in memory on the ordinary authenticated rate
budget. The paged chart read and the FHIR export had both bounded themselves; this one, added
after them, had not.

The fix is deliberately not one bound, and the distinction is the thing worth pinning:

* the **event ceiling** applies to the rows read, *before* grouping, and cannot be a page —
  ``started_on``, the dose sequence and ``is_current`` are all computed across a whole group, so
  paging the SQL would produce groups that are confidently wrong rather than visibly short;
* **limit/offset** page the drug groups, *after* grouping, which is safe precisely because every
  per-group figure has been computed from the full set by then.

A short answer must never be silent, so the response says which of the two shortened it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services import prescription_timeline_service as service_module
from app.services.prescription_timeline_service import PrescriptionTimelineService

pytestmark = pytest.mark.asyncio


async def _patient(db) -> Patient:
    account = Account(email=f"bounds-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Bounds Patient",
        sex="female",
        date_of_birth=date(1970, 1, 1),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


async def _drugs(db, patient: Patient, count: int, *, events_each: int = 1) -> None:
    """``count`` distinct drugs, ``events_each`` events apiece, dated so the order is total."""
    start = date(2000, 1, 1)
    for drug in range(count):
        for event in range(events_each):
            db.add(
                MedicationEvent(
                    patient_id=patient.id,
                    generic_name=f"Drug {drug:04d}",
                    event_type="start" if event == 0 else "change",
                    is_current=True,
                    dose=str(100 + event),
                    dose_unit="mg",
                    event_date=start + timedelta(days=drug * 400 + event),
                )
            )
    await db.flush()


# --- Paging the groups ------------------------------------------------------------------------


async def test_a_chart_inside_one_page_is_unchanged(db) -> None:
    patient = await _patient(db)
    await _drugs(db, patient, 3)

    timeline = await PrescriptionTimelineService(db).timeline(patient.id)

    assert len(timeline.drugs) == 3
    assert (timeline.total_drugs, timeline.total_events) == (3, 3)
    assert timeline.events_truncated is False


async def test_the_page_is_a_window_over_a_stable_order(db) -> None:
    """Two pages must partition the list, not overlap it. The service's sort ends in the display
    name, so there are no ties for ``OFFSET`` to reorder across."""
    patient = await _patient(db)
    await _drugs(db, patient, 8)
    svc = PrescriptionTimelineService(db)

    first = await svc.timeline(patient.id, limit=5, offset=0)
    second = await svc.timeline(patient.id, limit=5, offset=5)
    whole = await svc.timeline(patient.id, limit=100, offset=0)

    assert [d.display_name for d in first.drugs + second.drugs] == [
        d.display_name for d in whole.drugs
    ]


async def test_the_totals_are_the_charts_and_not_the_pages(db) -> None:
    """A client must not have to infer "is there more" from the length of the array it was
    handed — that is the mistake this whole shape exists to prevent."""
    patient = await _patient(db)
    await _drugs(db, patient, 8, events_each=2)

    page = await PrescriptionTimelineService(db).timeline(patient.id, limit=3)

    assert len(page.drugs) == 3
    assert page.total_drugs == 8
    assert page.total_events == 16


async def test_every_figure_in_a_group_is_computed_from_the_whole_group_not_the_page(db) -> None:
    """The reason the events are *not* paged. A group's start date and dose sequence are
    properties of its whole history, and a page that recomputed them from a slice would report a
    start date the patient never had."""
    patient = await _patient(db)
    await _drugs(db, patient, 1, events_each=6)

    page = await PrescriptionTimelineService(db).timeline(patient.id, limit=1)

    group = page.drugs[0]
    assert group.started_on == date(2000, 1, 1)
    assert len(group.entries) == 6
    assert group.dose_change_count == 5


async def test_an_offset_past_the_end_is_an_empty_page_and_not_an_error(db) -> None:
    patient = await _patient(db)
    await _drugs(db, patient, 2)

    page = await PrescriptionTimelineService(db).timeline(patient.id, offset=50)

    assert page.drugs == []
    assert page.total_drugs == 2


# --- The event ceiling ------------------------------------------------------------------------


async def test_the_ceiling_bounds_what_is_read_and_says_that_it_did(db, monkeypatch) -> None:
    monkeypatch.setattr(service_module, "EVENT_CEILING", 5)
    patient = await _patient(db)
    await _drugs(db, patient, 4, events_each=3)  # 12 events

    timeline = await PrescriptionTimelineService(db).timeline(patient.id, limit=100)

    assert timeline.events_truncated is True
    assert timeline.total_events == 5


async def test_the_oldest_events_are_the_ones_kept(db, monkeypatch) -> None:
    """Not an arbitrary choice. The groups are built forwards — ``started_on`` and every
    ``previous_dose_text`` depend on the beginning being present — so dropping the head would
    silently invent a start date and a first dose for every drug on the chart. Dropping the tail
    loses recent entries, which the response admits."""
    monkeypatch.setattr(service_module, "EVENT_CEILING", 2)
    patient = await _patient(db)
    await _drugs(db, patient, 1, events_each=5)

    timeline = await PrescriptionTimelineService(db).timeline(patient.id)

    dates = [entry.event_date for entry in timeline.drugs[0].entries]
    assert dates == [date(2000, 1, 1), date(2000, 1, 2)]


async def test_a_chart_exactly_at_the_ceiling_is_not_reported_as_truncated(db, monkeypatch):
    """The off-by-one that would make the flag useless: reading one row past the ceiling is what
    tells "we read exactly the ceiling" apart from "there was more", without a second COUNT."""
    monkeypatch.setattr(service_module, "EVENT_CEILING", 4)
    patient = await _patient(db)
    await _drugs(db, patient, 4, events_each=1)

    timeline = await PrescriptionTimelineService(db).timeline(patient.id)

    assert timeline.events_truncated is False
    assert timeline.total_events == 4


async def test_an_empty_chart_is_not_truncated(db) -> None:
    patient = await _patient(db)

    timeline = await PrescriptionTimelineService(db).timeline(patient.id)

    assert (timeline.drugs, timeline.total_drugs, timeline.events_truncated) == ([], 0, False)


# --- Over the wire ----------------------------------------------------------------------------


async def test_the_route_pages_and_reports_it(auth_client, db) -> None:
    from tests.conftest import create_patient

    created = await create_patient(auth_client)
    patient = await db.get(Patient, uuid.UUID(created["id"]))
    assert patient is not None
    await _drugs(db, patient, 6)
    await db.commit()

    resp = await auth_client.get(
        f"/api/v1/patients/{created['id']}/medications/timeline?limit=2&offset=0"
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["medications"]) == 2
    assert body["total_drugs"] == 6
    assert body["pagination"] == {"total": 6, "limit": 2, "offset": 0, "has_more": True}
    assert body["events_truncated"] is False


async def test_the_last_page_says_it_is_the_last(auth_client, db) -> None:
    from tests.conftest import create_patient

    created = await create_patient(auth_client)
    patient = await db.get(Patient, uuid.UUID(created["id"]))
    assert patient is not None
    await _drugs(db, patient, 3)
    await db.commit()

    resp = await auth_client.get(
        f"/api/v1/patients/{created['id']}/medications/timeline?limit=2&offset=2"
    )

    assert resp.json()["pagination"]["has_more"] is False


async def test_the_route_refuses_a_page_size_over_the_ceiling(auth_client) -> None:
    """A ceiling a client can ask past is not a ceiling. 422 from the query model rather than a
    silent clamp, so a caller that wanted 10,000 groups finds out."""
    from tests.conftest import create_patient

    created = await create_patient(auth_client)

    resp = await auth_client.get(
        f"/api/v1/patients/{created['id']}/medications/timeline"
        f"?limit={service_module.MAX_DRUG_LIMIT + 1}"
    )

    assert resp.status_code == 422


async def test_the_trail_records_the_charts_totals_not_the_page_size(auth_client, db) -> None:
    """A page size is a fact about the client. What a DPDP review asks of this entry is how much
    of the patient's prescribing history was assembled for someone."""
    from tests.conftest import create_patient

    created = await create_patient(auth_client)
    patient = await db.get(Patient, uuid.UUID(created["id"]))
    assert patient is not None
    await _drugs(db, patient, 5)
    await db.commit()
    await auth_client.get(f"/api/v1/patients/{created['id']}/medications/timeline?limit=1")

    trail = (await auth_client.get(f"/api/v1/patients/{created['id']}/audit")).json()
    entry = next(e for e in trail["items"] if e["action"] == "prescription_timeline_viewed")

    assert entry["payload"]["drugs"] == 5
    assert entry["payload"]["returned_drugs"] == 1
    assert entry["payload"]["events_truncated"] is False
