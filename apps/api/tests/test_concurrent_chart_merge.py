"""Two prescriptions merging into one chart at the same moment.

``DocumentService.approve`` has locked the *document* row since the double-click race was closed:
two approvals of the same report both read a chart without its rows on it and both merge. That
lock does nothing for two **different** documents, and those are not independent work — they
merge into the same record, and a merge decides what to write by reading what is already there.

The pair that costs something is a prescription that starts a drug and a prescription that stops
it. ``GraphService._retire_current`` can only clear rows that are already committed, so a stop
merging alongside an unrelated start reads a chart without the start on it, retires nothing, and
leaves the drug ``is_current`` on a patient who has just been taken off it — with a ``stop`` event
sitting on the same chart saying the opposite. That is the disagreement
``is_current = event_type != "stop"`` was written to end, reintroduced by timing: the FHIR export
reads ``event_type`` and says stopped; the deterministic safety engine and ``GET /records`` read
``is_current`` and say the patient is still anticoagulated, still interacting with everything
prescribed since, still counting toward the cumulative bleeding burden.

It is not a contrived overlap. The arrangement this product is built for is one practice login
used from two rooms, and approving the two prescriptions a patient walked in with is the ordinary
way to work.

So ``approve`` locks the chart as well as the document, and the two approvals serialise: the
loser waits, reads the winner's rows, and merges on top — which is the sequential path the
deduplication and the retirement are already written for, and which this file pins in both
orders.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.document_service import DocumentService
from app.services.graph_service import GraphService
from app.services.patient_service import PatientService


def _med(name: str, event_type: str, dose: str | None = None) -> dict:
    return {
        "entity_type": "medication",
        "fields": {"generic_name": name, "event_type": event_type, "dose": dose},
        "confidence": {},
    }


async def _patient(db) -> Patient:
    account = Account(email=f"merge-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Two Prescriptions",
        sex="male",
        date_of_birth=datetime(1957, 9, 4).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


async def _rows(db, patient: Patient) -> list[MedicationEvent]:
    return list(
        (await db.execute(select(MedicationEvent).where(MedicationEvent.patient_id == patient.id)))
        .scalars()
        .all()
    )


def _contradictions(rows: list[MedicationEvent]) -> list[str]:
    """Drugs the chart both discontinues and lists as current — the state the lock prevents."""
    stopped = {(row.generic_name or "").lower() for row in rows if row.event_type == "stop"}
    current = {(row.generic_name or "").lower() for row in rows if row.is_current}
    return sorted(stopped & current)


# --- The lock ------------------------------------------------------------------------------


def test_approve_locks_the_chart_not_only_the_document() -> None:
    """Pinned by reading ``approve`` and compiling the statement, not by racing two requests.

    SQLAlchemy's SQLite dialect drops ``FOR UPDATE`` silently, so a test that raced two approvals
    on the file-backed engine the other concurrency tests use would fail against correct code.
    Compiling against the PostgreSQL dialect asserts what is actually true — that production takes
    the lock — rather than asserting nothing. Same argument, and the same shape, as
    ``test_approve_locks_the_document_row_before_reading_the_chart``.
    """
    statement = (
        select(Patient)
        .where(Patient.id == uuid.uuid4(), Patient.is_deleted.is_(False))
        .with_for_update()
    )
    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))

    source = inspect.getsource(DocumentService.approve)
    assert "_get_patient_for_processing(account_id, patient_id, for_update=True)" in source, (
        "DocumentService.approve must load the patient with a row lock; without it two "
        "approvals of different documents merge into one chart from the same stale read, and a "
        "discontinuation merged alongside a prescription retires nothing"
    )


def test_the_lock_is_opt_in_so_ordinary_reads_do_not_take_it() -> None:
    """Every other caller of ``PatientService.get`` reads without locking the chart.

    The lock is for the merge and nothing else. If it leaked into the default, every chart read
    in the product would queue behind an approval in flight.
    """
    signature = inspect.signature(PatientService.get)
    assert signature.parameters["for_update"].default is False
    assert signature.parameters["for_update"].kind is inspect.Parameter.KEYWORD_ONLY

    # And no caller other than the document service asks for it.
    for method in (PatientService.get_for_audit, PatientService.get_for_display):
        assert "for_update=True" not in inspect.getsource(method)


# --- What serialising them has to produce ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_start_then_a_stop_leaves_the_drug_discontinued(db):
    """The order the lock produces when the start commits first."""
    patient = await _patient(db)
    service = GraphService(db)

    await service.merge_entities(
        patient=patient, document=None, entities=[_med("Warfarin", "continue", "5mg")]
    )
    await db.flush()
    await service.merge_entities(
        patient=patient, document=None, entities=[_med("Warfarin", "stop")]
    )
    await db.flush()

    rows = await _rows(db, patient)
    assert not _contradictions(rows), (
        f"the chart both stops and continues {_contradictions(rows)} — the FHIR export would say "
        "stopped while the safety engine and the record list say current"
    )
    assert not [row for row in rows if row.is_current]


@pytest.mark.asyncio
async def test_a_stop_then_a_start_leaves_the_drug_on_the_chart(db):
    """The other order, which is a legitimate restart and must not be swallowed.

    Serialising the two approvals means one of these two orders happens. Both have to be right:
    the lock is worth taking only if the sequential answer is correct whichever way round the two
    documents land.
    """
    patient = await _patient(db)
    service = GraphService(db)

    await service.merge_entities(
        patient=patient, document=None, entities=[_med("Warfarin", "stop")]
    )
    await db.flush()
    await service.merge_entities(
        patient=patient, document=None, entities=[_med("Warfarin", "continue", "5mg")]
    )
    await db.flush()

    rows = await _rows(db, patient)
    assert {row.generic_name for row in rows if row.is_current} == {"Warfarin"}
    # The stop is still on the record — it happened — it is simply no longer the last word.
    assert any(row.event_type == "stop" for row in rows)


@pytest.mark.asyncio
async def test_a_second_prescription_of_the_same_drug_does_not_double_the_chart(db):
    """The other pair the lock serialises: two documents listing the same current medication.

    Sequentially the second merge reads the first's row and skips it. That dedup is the reason a
    duplicated current medication is normally impossible, and it is a read of committed rows —
    so it is the same window the discontinuation loses in.
    """
    patient = await _patient(db)
    service = GraphService(db)

    for _ in range(2):
        await service.merge_entities(
            patient=patient, document=None, entities=[_med("Metformin", "continue", "500mg")]
        )
        await db.flush()

    current = [row for row in await _rows(db, patient) if row.is_current]
    assert len(current) == 1, (
        f"one drug charted as {len(current)} current medications; every interaction and "
        "duplicate-therapy rule downstream counts it twice"
    )


@pytest.mark.asyncio
async def test_a_stop_and_a_start_in_one_document_still_land_as_a_switch(db):
    """The lock changes nothing within a single document, which must still merge as before.

    Rows added earlier in one merge are pending rather than committed, so a stop line cannot
    retire a start line from the document it arrived in — a prescription switching a dose lands
    as the switch it is, whichever order the two lines were extracted in.
    """
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[_med("Metformin", "stop", "500mg"), _med("Metformin", "continue", "1000mg")],
    )
    await db.flush()

    current = [row for row in await _rows(db, patient) if row.is_current]
    assert [row.dose for row in current] == ["1000mg"]
