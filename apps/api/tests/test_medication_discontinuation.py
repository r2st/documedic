"""A prescription that discontinues a drug has to take the drug off the chart.

``event_type`` has carried ``'stop'`` since the model was written, the extraction prompt asks
Claude for it by name (``"event_type": "continue|start|stop"``), and ``export_service`` reads it
— "a stop event is a stopped medication whatever ``is_current`` says". Nothing else did.
``_merge_medication`` wrote ``is_current=True`` for every event type, and the row never even got
that far: the dedup key is ``(generic, dose)`` over the patient's *current* medications, so a
stop event for a drug the patient was on matched the very row it was meant to retire, returned
``False``, and was dropped before insert. A document that read "STOP Warfarin" left no trace at
all.

Two consequences, both in the direction this project cares about:

* the deterministic safety engine reads ``is_current`` and nothing else, so a discontinued drug
  kept interacting with everything started after it and kept counting toward the cumulative
  bleeding burden — a major-interaction flag and a bleeding-risk flag, on the screen whose worth
  depends on clinicians reading its flags rather than clearing them, for a combination the
  patient was not taking;
* ``GET /records`` listed the drug as current. In a product whose stated job is to assemble a
  longitudinal record out of fragmented paper, a chart saying a patient is anticoagulated when
  the record itself says they were taken off warfarin is the error the record exists to prevent,
  and it is what the next decision gets made on.

The FHIR export was the one component reading the chart correctly, which is how the two halves
came to disagree in writing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.graph_service import GraphService
from app.services.safety_service import SafetyService


def _med(name: str, event_type: str, dose: str | None = None) -> dict:
    """One extracted medication line, in the shape ``merge_entities`` takes."""
    return {
        "entity_type": "medication",
        "fields": {"generic_name": name, "event_type": event_type, "dose": dose},
        "confidence": {},
    }


async def _patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"stop-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Discontinuation Patient",
        sex="male",
        date_of_birth=datetime(1957, 9, 4).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _visit(db, patient: Patient, *lines: dict) -> None:
    """One document's worth of extracted medication lines, approved into the graph."""
    await GraphService(db).merge_entities(patient=patient, document=None, entities=list(lines))
    await db.flush()


async def _rows(db, patient: Patient) -> list[MedicationEvent]:
    result = await db.execute(
        select(MedicationEvent)
        .where(MedicationEvent.patient_id == patient.id)
        .order_by(MedicationEvent.generic_name, MedicationEvent.event_type)
    )
    return list(result.scalars().all())


async def _current(db, patient: Patient) -> set[str]:
    return {row.generic_name for row in await _rows(db, patient) if row.is_current}


# --- The record ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stopping_a_drug_takes_it_off_the_current_medication_list(db):
    """Two visits: on warfarin, then off it. The chart has to be able to say the second."""
    _account, patient = await _patient(db)
    await _visit(db, patient, _med("Warfarin", "start"))
    assert await _current(db, patient) == {"Warfarin"}

    await _visit(db, patient, _med("Warfarin", "stop"))

    assert await _current(db, patient) == set()


@pytest.mark.asyncio
async def test_the_discontinuation_is_kept_rather_than_swallowed(db):
    """The stop event was being dropped by the dedup, not merely mis-flagged.

    It has to survive as a row: "warfarin was stopped" is a fact about this patient's history,
    it is what the FHIR export renders as ``status: stopped``, and a record that simply loses
    the line cannot be told apart from one where the drug was never mentioned again.
    """
    _account, patient = await _patient(db)
    await _visit(db, patient, _med("Warfarin", "start"))
    await _visit(db, patient, _med("Warfarin", "stop"))

    events = [(row.event_type, row.is_current) for row in await _rows(db, patient)]
    assert sorted(events) == [("start", False), ("stop", False)]


@pytest.mark.asyncio
async def test_stopping_one_drug_leaves_the_rest_of_the_chart_alone(db):
    _account, patient = await _patient(db)
    await _visit(
        db, patient, _med("Warfarin", "start"), _med("Metformin", "start"), _med("Aspirin", "start")
    )

    await _visit(db, patient, _med("Warfarin", "stop"))

    assert await _current(db, patient) == {"Metformin", "Aspirin"}


@pytest.mark.asyncio
async def test_a_stop_matches_the_drug_through_the_vocabulary_not_the_spelling(db):
    """CLAUDE.md pitfall #4, on the discontinuation path.

    The chart carries the INN; the discontinuing prescription names the Indian brand. Matching
    on text would leave the patient on a drug their clinician stopped because two documents
    spelled it differently, which is the whole reason the vocabulary is in front of every other
    comparison in this system.
    """
    _account, patient = await _patient(db)
    await _visit(db, patient, _med("Metformin", "start", "500mg"))

    await _visit(db, patient, _med("Glycomet", "stop"))

    assert await _current(db, patient) == set()


@pytest.mark.asyncio
async def test_a_stop_line_nothing_can_identify_retires_nothing(db):
    """The failure direction that would be worse than the one being fixed.

    OCR of a handwritten prescription produces unreadable lines routinely. A stop line whose
    drug resolves to nothing must not be allowed to clear a medication list — the record would
    then be missing drugs the patient is actually taking, and the safety engine would go quiet
    about them.
    """
    _account, patient = await _patient(db)
    await _visit(db, patient, _med("Warfarin", "start"), _med("Metformin", "start"))

    await _visit(db, patient, _med("Wrfrn 5mg (illegible)", "stop"))

    assert await _current(db, patient) == {"Warfarin", "Metformin"}


# --- The same document -----------------------------------------------------------------------


@pytest.mark.parametrize("stop_first", [True, False], ids=["stop_then_start", "start_then_stop"])
@pytest.mark.asyncio
async def test_a_dose_change_written_as_stop_and_start_lands_as_the_switch_it_is(db, stop_first):
    """A titration is one prescription with two lines, and their order is the extractor's.

    The stop must retire what the chart held before this document and nothing this document
    adds. Parametrised because the whole property is that the outcome does not depend on which
    line the extractor emitted first — which holds only because rows added during a merge are
    still pending and invisible to the stop's own query.
    """
    _account, patient = await _patient(db)
    await _visit(db, patient, _med("Metformin", "start", "500mg"))

    lines = [_med("Metformin", "stop", "500mg"), _med("Metformin", "start", "1000mg")]
    await _visit(db, patient, *(lines if stop_first else list(reversed(lines))))

    current = [(row.dose, row.event_type) for row in await _rows(db, patient) if row.is_current]
    assert current == [("1000mg", "start")]


@pytest.mark.asyncio
async def test_a_brand_switch_at_the_same_dose_leaves_the_patient_on_the_drug(db):
    """Stop and restart at one dose, which the dedup key cannot tell apart on its own.

    ``seen`` is loaded from the patient's current medications so a document cannot re-insert one
    it already has. Once the stop retires those rows they are not current any more, so the keys
    have to go with them — otherwise the restart line reads as a duplicate of the row that
    stopped it and the patient ends up on neither.
    """
    _account, patient = await _patient(db)
    await _visit(db, patient, _med("Glycomet", "start", "500mg"))

    await _visit(
        db, patient, _med("Glycomet", "stop", "500mg"), _med("Metformin", "start", "500mg")
    )

    current = [
        (row.generic_name, row.event_type) for row in await _rows(db, patient) if row.is_current
    ]
    assert current == [("Metformin", "start")]


@pytest.mark.asyncio
async def test_one_stop_line_repeated_in_a_document_inserts_once(db):
    """The dedup the stop path still needs, in its own namespace.

    Giving stops a separate key is what stopped them colliding with the row they retire; it must
    not also stop them colliding with each other, or a prescription listing a discontinuation
    twice writes it twice.
    """
    _account, patient = await _patient(db)
    await _visit(db, patient, _med("Warfarin", "start"))

    await _visit(db, patient, _med("Warfarin", "stop"), _med("Warfarin", "stop"))

    assert [row.event_type for row in await _rows(db, patient)] == ["start", "stop"]


# --- What the safety engine then sees --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_safety_engine_stops_pairing_a_drug_the_patient_was_taken_off(db):
    """The cost, measured where a clinician would have met it.

    Warfarin with aspirin is a major interaction and two bleeding mechanisms at once. Stop the
    warfarin and both findings have to go — a patient on aspirin alone is not the patient those
    flags describe, and a screen that keeps insisting otherwise is training its readers to
    dismiss it. Asserted as "the pair-dependent findings are present, then none of them are"
    rather than against an exact set, so warfarin's own single-drug flags do not make this test
    about them.
    """
    account, patient = await _patient(db)
    await _visit(db, patient, _med("Warfarin", "start"), _med("Aspirin", "start"))
    before = {
        f.check_type
        for _d, flags in await SafetyService(db).active_flags(
            account_id=account.id, patient_id=patient.id
        )
        for f in flags
    }
    assert {"drug_interaction", "bleeding_burden"} <= before, (
        "fixture check — warfarin with aspirin must flag before the stop"
    )

    await _visit(db, patient, _med("Warfarin", "stop"))

    after = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    assert [f.check_type for _d, flags in after for f in flags] == [], (
        "aspirin alone raises nothing, so anything left here is a finding about a drug the "
        "patient was taken off"
    )


@pytest.mark.asyncio
async def test_a_discontinued_drug_is_not_reported_as_unevaluated_either(db):
    """It left the chart's current medications; it did not become a gap in them.

    ``check_unevaluated_medications`` reports drugs that could not be read. A drug that was read
    perfectly well and then stopped is neither evaluated nor missing — it is simply no longer a
    current medication, and reporting it as an unread line would be its own false alarm.
    """
    _account, patient = await _patient(db)
    await _visit(db, patient, _med("Warfarin", "start"))
    await _visit(db, patient, _med("Warfarin", "stop"))

    assert await SafetyService(db).chart_completeness_flags(patient.id) == []
