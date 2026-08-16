"""A patient's prescribing history, assembled per drug rather than per row.

``medication_events`` is an event log and every read of it returned it as one, so the question a
clinician actually asks about a drug — when did this start, what has the dose been, who changed
it — could only be answered by scrolling a mixed list of every drug's events and reconstructing
each story by eye.

Two things here are load-bearing beyond the regrouping itself, and both are about not inventing
facts that the record does not hold:

* **Grouping never guesses.** A resolved row and an unresolved row that merely look alike stay
  separate groups (CLAUDE.md pitfall #4), because merging them would fabricate a dose change
  between two prescriptions that were never the same drug.
* **"Is it current" follows the chart's own precedence.** ``is_current`` is written per row at
  merge time and never revisited, so a later ``stop`` ends the therapy whatever an earlier row's
  flag says — the same rule the FHIR export applies. Reading "any row is current" first would
  leave discontinued drugs on the current list forever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.models.drug_vocabulary import DrugVocabulary
from app.models.encounter import Encounter
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.prescription_timeline_service import PrescriptionTimelineService

from .conftest import create_patient

pytestmark = pytest.mark.asyncio


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"timeline-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Timeline Patient",
        sex="male",
        date_of_birth=date(1970, 1, 1),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _event(db, patient: Patient, **fields) -> MedicationEvent:
    defaults = {"event_type": "start", "is_current": True}
    defaults.update(fields)
    row = MedicationEvent(patient_id=patient.id, **defaults)
    db.add(row)
    await db.flush()
    return row


async def _timeline(db, patient: Patient, **kwargs):
    """The page's drug groups. The tests below are about grouping, and every chart they build is
    far inside one page — the bounds themselves are asserted in ``test_prescription_timeline_
    bounds.py``, against charts built to exceed them."""
    return (await PrescriptionTimelineService(db).timeline(patient.id, **kwargs)).drugs


# --- grouping ---------------------------------------------------------------------------------


async def test_an_empty_chart_returns_nothing_rather_than_an_empty_group(db) -> None:
    _, patient = await _account_and_patient(db)

    assert await _timeline(db, patient) == []


async def test_every_event_for_one_drug_lands_in_one_group_oldest_first(db) -> None:
    _, patient = await _account_and_patient(db)
    await _event(
        db,
        patient,
        generic_name="Metformin",
        dose="500",
        dose_unit="mg",
        frequency="OD",
        event_date=date(2024, 1, 1),
    )
    await _event(
        db,
        patient,
        generic_name="Metformin",
        event_type="change",
        dose="1000",
        dose_unit="mg",
        frequency="BD",
        event_date=date(2025, 3, 1),
    )

    groups = await _timeline(db, patient)

    assert len(groups) == 1
    assert [e.event_date for e in groups[0].entries] == [date(2024, 1, 1), date(2025, 3, 1)]
    assert len(groups[0].entries) == 2


async def test_two_drugs_are_two_groups(db) -> None:
    _, patient = await _account_and_patient(db)
    await _event(db, patient, generic_name="Metformin", event_date=date(2024, 1, 1))
    await _event(db, patient, generic_name="Amlodipine", event_date=date(2024, 1, 1))

    groups = await _timeline(db, patient)

    assert {g.display_name for g in groups} == {"Metformin", "Amlodipine"}


async def test_the_same_name_written_differently_still_groups(db) -> None:
    _, patient = await _account_and_patient(db)
    await _event(db, patient, generic_name="Metformin", event_date=date(2024, 1, 1))
    await _event(db, patient, generic_name="  metformin ", event_date=date(2024, 6, 1))

    assert len(await _timeline(db, patient)) == 1


async def test_a_resolved_row_and_a_look_alike_unresolved_row_are_not_merged(db) -> None:
    """Merging them would fabricate a dose change between two different prescriptions."""
    _, patient = await _account_and_patient(db)
    vocab = DrugVocabulary(
        reference_id=f"MET-{uuid.uuid4().hex[:6]}",
        generic_name="Metformin",
        brand_name="Glycomet",
        is_active=True,
    )
    db.add(vocab)
    await db.flush()
    await _event(
        db,
        patient,
        generic_name="Metformin",
        drug_vocabulary_id=vocab.id,
        dose="500",
        dose_unit="mg",
        event_date=date(2024, 1, 1),
    )
    await _event(
        db,
        patient,
        brand_name_raw="Metformin",
        dose="1000",
        dose_unit="mg",
        event_date=date(2024, 6, 1),
    )

    groups = await _timeline(db, patient)

    assert len(groups) == 2
    resolved = next(g for g in groups if not g.unresolved)
    unresolved = next(g for g in groups if g.unresolved)
    assert resolved.reference_id == vocab.reference_id
    assert unresolved.reference_id is None
    # And no dose change was invented across the boundary.
    assert resolved.dose_change_count == 0
    assert unresolved.dose_change_count == 0


async def test_a_row_with_no_name_at_all_is_grouped_rather_than_dropped(db) -> None:
    _, patient = await _account_and_patient(db)
    await _event(db, patient, event_date=date(2024, 1, 1))

    groups = await _timeline(db, patient)

    assert len(groups) == 1
    assert groups[0].display_name == "unnamed medication"


# --- dose changes -----------------------------------------------------------------------------


async def test_a_dose_change_names_what_the_dose_was_before_it(db) -> None:
    _, patient = await _account_and_patient(db)
    await _event(
        db,
        patient,
        generic_name="Metformin",
        dose="500",
        dose_unit="mg",
        frequency="OD",
        event_date=date(2024, 1, 1),
    )
    await _event(
        db,
        patient,
        generic_name="Metformin",
        event_type="change",
        dose="1000",
        dose_unit="mg",
        frequency="BD",
        event_date=date(2025, 3, 1),
    )

    group = (await _timeline(db, patient))[0]

    assert group.entries[0].dose_changed is False
    assert group.entries[0].previous_dose_text is None
    assert group.entries[1].dose_changed is True
    assert group.entries[1].previous_dose_text == "500 mg OD"
    assert group.entries[1].dose_text == "1000 mg BD"
    assert group.dose_change_count == 1


async def test_a_frequency_change_at_the_same_strength_is_a_dose_change(db) -> None:
    """ "500 mg twice daily" -> "500 mg once daily" halves the dose."""
    _, patient = await _account_and_patient(db)
    await _event(
        db,
        patient,
        generic_name="Metformin",
        dose="500",
        dose_unit="mg",
        frequency="BD",
        event_date=date(2024, 1, 1),
    )
    await _event(
        db,
        patient,
        generic_name="Metformin",
        event_type="change",
        dose="500",
        dose_unit="mg",
        frequency="OD",
        event_date=date(2025, 1, 1),
    )

    assert (await _timeline(db, patient))[0].dose_change_count == 1


async def test_re_charting_the_same_dose_is_not_a_change(db) -> None:
    _, patient = await _account_and_patient(db)
    for event_date in (date(2024, 1, 1), date(2024, 7, 1), date(2025, 1, 1)):
        await _event(
            db,
            patient,
            generic_name="Metformin",
            event_type="continue",
            dose="500",
            dose_unit="mg",
            frequency="BD",
            event_date=event_date,
        )

    assert (await _timeline(db, patient))[0].dose_change_count == 0


async def test_a_stop_and_restart_at_the_same_dose_invents_no_change(db) -> None:
    """A stop line carries no dose; reading its absence as a change would count two."""
    _, patient = await _account_and_patient(db)
    await _event(
        db, patient, generic_name="Warfarin", dose="5", dose_unit="mg", event_date=date(2024, 1, 1)
    )
    await _event(
        db,
        patient,
        generic_name="Warfarin",
        event_type="stop",
        is_current=False,
        event_date=date(2024, 6, 1),
    )
    await _event(
        db, patient, generic_name="Warfarin", dose="5", dose_unit="mg", event_date=date(2024, 9, 1)
    )

    assert (await _timeline(db, patient))[0].dose_change_count == 0


# --- span and currency -------------------------------------------------------------------------


async def test_the_span_runs_from_the_first_event_to_the_stop(db) -> None:
    _, patient = await _account_and_patient(db)
    await _event(db, patient, generic_name="Amoxicillin", event_date=date(2025, 1, 1))
    await _event(
        db,
        patient,
        generic_name="Amoxicillin",
        event_type="stop",
        is_current=False,
        event_date=date(2025, 1, 8),
    )

    group = (await _timeline(db, patient))[0]

    assert group.started_on == date(2025, 1, 1)
    assert group.stopped_on == date(2025, 1, 8)
    assert group.is_current is False


async def test_a_stop_ends_the_therapy_even_when_an_earlier_row_is_still_flagged_current(
    db,
) -> None:
    """The merge writes ``is_current`` per row and never revisits its predecessors."""
    _, patient = await _account_and_patient(db)
    await _event(db, patient, generic_name="Warfarin", is_current=True, event_date=date(2024, 1, 1))
    await _event(
        db,
        patient,
        generic_name="Warfarin",
        event_type="stop",
        is_current=False,
        event_date=date(2025, 1, 1),
    )

    assert (await _timeline(db, patient))[0].is_current is False


async def test_a_restart_after_a_stop_is_current_again(db) -> None:
    _, patient = await _account_and_patient(db)
    await _event(db, patient, generic_name="Warfarin", event_date=date(2024, 1, 1))
    await _event(
        db,
        patient,
        generic_name="Warfarin",
        event_type="stop",
        is_current=False,
        event_date=date(2024, 6, 1),
    )
    await _event(db, patient, generic_name="Warfarin", is_current=True, event_date=date(2024, 9, 1))

    group = (await _timeline(db, patient))[0]

    assert group.is_current is True
    assert group.stopped_on is None


async def test_an_undated_stop_is_treated_as_the_last_word(db) -> None:
    """It cannot be placed in the sequence, and claiming the drug is current is the answer that
    puts it back in front of a prescriber."""
    _, patient = await _account_and_patient(db)
    await _event(db, patient, generic_name="Warfarin", event_date=date(2024, 1, 1))
    await _event(db, patient, generic_name="Warfarin", event_type="stop", is_current=False)

    assert (await _timeline(db, patient))[0].is_current is False


# --- ordering ----------------------------------------------------------------------------------


async def test_current_therapy_sorts_before_stopped_therapy(db) -> None:
    _, patient = await _account_and_patient(db)
    await _event(db, patient, generic_name="Amoxicillin", event_date=date(2025, 6, 1))
    await _event(
        db,
        patient,
        generic_name="Amoxicillin",
        event_type="stop",
        is_current=False,
        event_date=date(2025, 6, 8),
    )
    await _event(db, patient, generic_name="Metformin", event_date=date(2020, 1, 1))

    groups = await _timeline(db, patient)

    assert [g.display_name for g in groups] == ["Metformin", "Amoxicillin"]


async def test_undated_events_sort_last_so_nothing_reads_as_a_change_from_them(db) -> None:
    _, patient = await _account_and_patient(db)
    await _event(db, patient, generic_name="Metformin", dose="500", dose_unit="mg")
    await _event(
        db,
        patient,
        generic_name="Metformin",
        dose="1000",
        dose_unit="mg",
        event_date=date(2024, 1, 1),
    )

    entries = (await _timeline(db, patient))[0].entries

    assert entries[0].event_date == date(2024, 1, 1)
    assert entries[1].event_date is None


# --- the prescribing context --------------------------------------------------------------------


async def test_an_entry_carries_the_visit_it_was_recorded_at(db) -> None:
    _, patient = await _account_and_patient(db)
    encounter = Encounter(
        patient_id=patient.id,
        encounter_date=date(2025, 4, 2),
        encounter_type="outpatient",
        status="draft",
    )
    db.add(encounter)
    await db.flush()
    await _event(
        db,
        patient,
        generic_name="Metformin",
        encounter_id=encounter.id,
        event_date=date(2025, 4, 2),
        prescriber_name="Dr A Sharma",
    )

    entry = (await _timeline(db, patient))[0].entries[0]

    assert entry.encounter_id == encounter.id
    assert entry.encounter_date == date(2025, 4, 2)
    assert entry.encounter_type == "outpatient"
    assert entry.prescriber_name == "Dr A Sharma"


async def test_a_soft_deleted_event_is_not_on_the_timeline(db) -> None:
    _, patient = await _account_and_patient(db)
    row = await _event(db, patient, generic_name="Metformin", event_date=date(2024, 1, 1))
    row.is_deleted = True
    await db.flush()

    assert await _timeline(db, patient) == []


# --- over the wire ------------------------------------------------------------------------------


async def test_the_endpoint_returns_the_grouped_history(auth_client) -> None:
    patient = await create_patient(auth_client)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/medications/timeline")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["patient_id"] == patient["id"]
    assert body["medications"] == []
    assert body["total_drugs"] == 0 and body["total_events"] == 0


async def test_another_accounts_timeline_is_a_404(auth_client, second_auth_client) -> None:
    patient = await create_patient(auth_client)

    resp = await second_auth_client.get(f"/api/v1/patients/{patient['id']}/medications/timeline")

    assert resp.status_code == 404


async def test_the_timeline_read_is_on_the_audit_trail(auth_client) -> None:
    """A patient's whole prescribing history is a disclosure."""
    patient = await create_patient(auth_client)
    await auth_client.get(f"/api/v1/patients/{patient['id']}/medications/timeline")

    trail = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")).json()

    entries = [e for e in trail["items"] if e["action"] == "prescription_timeline_viewed"]
    assert len(entries) == 1
    # The chart's totals plus what this particular request actually returned. Both, because the
    # trail answers "how much of this patient's history was assembled for someone" — a page size
    # is a fact about the client — and because a truncated assembly is a different disclosure
    # from a complete one.
    assert set(entries[0]["payload"]) == {
        "drugs",
        "events",
        "returned_drugs",
        "events_truncated",
    }
