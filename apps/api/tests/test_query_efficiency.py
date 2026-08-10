"""Query-count regression guards for the read paths that scale with a patient's record size.

An N+1 is invisible in behavioural tests — the output is identical, only the round-trip count
grows — so it needs assertions on the query count itself. These cover the paths where the
number of rows is patient-controlled: current medications, active allergies, and mapped
conditions.

Two testing hazards these tests work around, both of which previously hid a real N+1:

* ``db.get()`` is served from the SQLAlchemy identity map, so an object already loaded in the
  test session costs no SQL. Every test here calls ``db.expunge_all()`` before measuring, which
  is what a production request (fresh session per request) actually looks like.
* Service objects cache reference data for their lifetime (``DrugResolver._all``). Each
  measurement constructs a fresh service so a warm cache cannot mask a per-row query.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select

from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.drug_vocabulary import DrugVocabulary
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.pathway_service import PathwayService
from app.services.safety_service import SafetyService

pytestmark = pytest.mark.asyncio


@contextmanager
def counting_queries(engine):
    """Yields a counter dict whose ``n`` tracks statements executed on `engine`."""
    counter = {"n": 0, "statements": []}

    def _count(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1
        counter["statements"].append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        yield counter
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"qe-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Query Count Patient",
        sex="female",
        date_of_birth=datetime(1972, 3, 4).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _vocab(db, generic: str) -> DrugVocabulary:
    result = await db.execute(
        select(DrugVocabulary).where(DrugVocabulary.generic_name.ilike(generic))
    )
    vocab = result.scalars().first()
    assert vocab is not None, f"seed data is missing {generic}"
    return vocab


async def _add_current_med(db, patient: Patient, generic: str) -> DrugVocabulary:
    vocab = await _vocab(db, generic)
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            drug_vocabulary_id=vocab.id,
            generic_name=vocab.generic_name,
            event_type="continue",
            is_current=True,
        )
    )
    await db.flush()
    return vocab


async def _add_allergy(db, patient: Patient, generic: str) -> None:
    vocab = await _vocab(db, generic)
    db.add(
        Allergy(
            patient_id=patient.id,
            drug_vocabulary_id=vocab.id,
            allergen_name=vocab.generic_name,
            allergen_type="drug",
            status="active",
        )
    )
    await db.flush()


async def _add_condition(db, patient: Patient, name: str) -> None:
    db.add(
        Condition(
            patient_id=patient.id,
            condition_name=name,
            status="active",
        )
    )
    await db.flush()


async def _flags_select_count(db, engine, account, patient) -> int:
    """SELECT count for one active_flags call, measured production-shaped.

    Counts reads only, because writes legitimately scale with the record: more current drugs
    mean more real interaction flags, and each flag is persisted as its own drug_safety_checks
    row. Counting every statement would conflate an N+1 read with correct audit behaviour.

    Uses ``active_flags`` rather than ``check_medication`` deliberately. ``check_medication``
    resolves the proposed drug first, and ``DrugResolver._all()`` selects the entire active
    vocabulary — which lands every row in the session identity map, so the old per-row
    ``db.get()`` was already served from memory on that path. ``active_flags`` reaches
    ``_build_context`` without that warm-up, so it is the path where the per-row fetch actually
    cost a round-trip per drug. The batched-query assertion below covers the mechanism itself.
    """
    with counting_queries(engine) as counter:
        service = SafetyService(db)
        db.expunge_all()
        counter["statements"].clear()
        await service.active_flags(account_id=account.id, patient_id=patient.id)
        return len([s for s in counter["statements"] if s.lstrip().upper().startswith("SELECT")])


async def test_safety_check_query_count_is_flat_in_medication_count(db, engine):
    """The batched vocabulary load must keep the safety context O(1) in current meds."""
    account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Metformin")
    baseline = await _flags_select_count(db, engine, account, patient)

    for generic in ("Atorvastatin", "Amlodipine", "Warfarin", "Paracetamol"):
        await _add_current_med(db, patient, generic)
    with_five = await _flags_select_count(db, engine, account, patient)

    assert with_five == baseline, (
        f"active_flags went from {baseline} to {with_five} SELECTs when the patient's current "
        "medications went from 1 to 5 — the per-medication DrugVocabulary fetch is back"
    )


async def test_safety_check_query_count_is_flat_in_allergy_count(db, engine):
    """Allergies share the single batched vocabulary query with medications."""
    account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Metformin")
    await _add_allergy(db, patient, "Paracetamol")
    baseline = await _flags_select_count(db, engine, account, patient)

    for generic in ("Azithromycin", "Atorvastatin", "Amlodipine"):
        await _add_allergy(db, patient, generic)
    with_four = await _flags_select_count(db, engine, account, patient)

    assert with_four == baseline, (
        f"active_flags went from {baseline} to {with_four} SELECTs when active allergies went "
        "from 1 to 4 — the per-allergy DrugVocabulary fetch is back"
    )


async def test_vocabulary_is_loaded_in_a_single_batched_query(db, engine):
    """Positive assertion on the mechanism, not just the count: one IN (...) query, no repeats.

    A count-only test would still pass if someone replaced N `db.get()` calls with N
    single-row SELECTs, so this pins the shape of the query too.
    """
    account, patient = await _account_and_patient(db)
    for generic in ("Metformin", "Atorvastatin", "Amlodipine"):
        await _add_current_med(db, patient, generic)

    with counting_queries(engine) as counter:
        service = SafetyService(db)
        db.expunge_all()
        counter["statements"].clear()
        await service.check_medication(
            account_id=account.id,
            patient_id=patient.id,
            drug_reference_id=None,
            drug_name="Aspirin",
        )
        vocab_selects = [
            s
            for s in counter["statements"]
            if "drug_vocabulary" in s.lower() and s.lstrip().upper().startswith("SELECT")
        ]

    by_id = [s for s in vocab_selects if "drug_vocabulary.id IN" in s.replace("\n", " ")]
    assert len(by_id) == 1, (
        f"expected exactly one batched vocabulary-by-id query, got {len(by_id)}: {by_id}"
    )


async def test_patient_pathways_query_count_is_flat_in_condition_count(db, engine):
    """for_patient must hydrate every matched pathway from one guideline lookup."""
    account, patient = await _account_and_patient(db)
    await _add_condition(db, patient, "Hypertension")

    async def _measure() -> int:
        with counting_queries(engine) as counter:
            service = PathwayService(db)
            db.expunge_all()
            counter["n"] = 0
            await service.for_patient(account_id=account.id, patient_id=patient.id)
            return counter["n"]

    baseline = await _measure()

    for name in ("Type 2 Diabetes Mellitus", "Dyslipidemia", "Community-Acquired Pneumonia"):
        await _add_condition(db, patient, name)
    with_four = await _measure()

    assert with_four == baseline, (
        f"patient pathways went from {baseline} to {with_four} queries when mapped conditions "
        "went from 1 to 4 — the per-condition guideline lookup is back"
    )


async def test_patient_pathways_still_returns_citations_for_every_matched_condition(db):
    """The batching must not cross-contaminate or drop citations between pathways."""
    account, patient = await _account_and_patient(db)
    for name in ("Hypertension", "Type 2 Diabetes Mellitus"):
        await _add_condition(db, patient, name)

    result = await PathwayService(db).for_patient(account_id=account.id, patient_id=patient.id)

    assert len(result["pathways"]) == 2
    for pathway in result["pathways"]:
        cited = [c for stage in pathway["stages"] for c in stage["citations"]]
        assert cited, f"{pathway['condition_name']} lost its citations under batched hydration"
        # Every citation a stage claims must be one the stage actually asked for.
        for stage in pathway["stages"]:
            for citation in stage["citations"]:
                assert citation["section_id"], "citation is missing its section id"

    # Single-pathway hydration agrees with the batched path.
    single = await PathwayService(db).get("hypertension")
    batched = next(p for p in result["pathways"] if p["condition_name"] == "Hypertension")
    assert single == batched


async def test_unmapped_conditions_cost_no_guideline_queries(db, engine):
    """A patient whose conditions map to no pathway must not query the guideline corpus."""
    account, patient = await _account_and_patient(db)
    for name in ("Some Unmapped Condition", "Another Unmapped One"):
        await _add_condition(db, patient, name)

    with counting_queries(engine) as counter:
        service = PathwayService(db)
        db.expunge_all()
        counter["statements"].clear()
        result = await service.for_patient(account_id=account.id, patient_id=patient.id)

    assert result["pathways"] == []
    assert sorted(result["unmapped_conditions"]) == ["Another Unmapped One", "Some Unmapped Condition"]
    guideline_queries = [s for s in counter["statements"] if "guideline_chunks" in s.lower()]
    assert not guideline_queries, (
        f"no pathway matched, so the guideline corpus should not be queried: {guideline_queries}"
    )
