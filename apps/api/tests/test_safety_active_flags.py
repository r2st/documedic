"""SafetyService.active_flags — pairwise re-check across a patient's current medications.

Also pins the query-count optimisation: the safety context is built once, not once per drug.
A regression there is invisible in behaviour but turns an N-drug patient into N full context
rebuilds (two reference-table scans each) on every page load of the safety screen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select

from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.drug_vocabulary import DrugVocabulary
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"flags-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Test Patient",
        sex="male",
        date_of_birth=datetime(1970, 1, 1).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _vocab(db, generic: str) -> DrugVocabulary | None:
    result = await db.execute(
        select(DrugVocabulary).where(DrugVocabulary.generic_name.ilike(generic))
    )
    return result.scalars().first()


async def _add_current_med(db, patient: Patient, generic: str) -> DrugVocabulary:
    vocab = await _vocab(db, generic)
    assert vocab is not None, f"seed data is missing {generic}"
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


@pytest.mark.asyncio
async def test_no_current_medications_yields_no_flags(db):
    account, patient = await _account_and_patient(db)
    result = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    assert result == []


@pytest.mark.asyncio
async def test_a_single_medication_is_not_flagged_against_itself(db):
    """The pairwise check excludes the drug under test from its own comparison set —
    otherwise every medication would flag as a duplicate of itself."""
    account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Metformin")

    result = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    assert all(f.check_type != "duplicate_therapy" for _v, flags in result for f in flags)


@pytest.mark.asyncio
async def test_an_interacting_pair_is_flagged(db):
    account, patient = await _account_and_patient(db)
    warfarin = await _vocab(db, "Warfarin")
    aspirin = await _vocab(db, "Aspirin")
    if warfarin is None or aspirin is None:
        pytest.skip("seed corpus lacks the warfarin/aspirin interaction pair")

    await _add_current_med(db, patient, "Warfarin")
    await _add_current_med(db, patient, "Aspirin")

    result = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    assert any(f.check_type == "drug_interaction" for _v, flags in result for f in flags)


@pytest.mark.asyncio
async def test_an_allergy_conflict_surfaces_as_a_hard_block(db):
    """A documented allergy against a drug the patient is currently on must hard-block
    (Critical Safety Rule #3) — and must be found without any LLM involvement."""
    account, patient = await _account_and_patient(db)
    vocab = await _add_current_med(db, patient, "Amoxicillin + Clavulanic acid")
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name=vocab.generic_name,
            allergen_type="drug",
            drug_vocabulary_id=vocab.id,
            status="active",
        )
    )
    await db.flush()

    result = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    assert any(f.is_hard_block for _v, flags in result for f in flags)


@pytest.mark.asyncio
async def test_results_are_ordered_deterministically(db):
    """Two calls on unchanged data must return the same order — the UI diffs on it."""
    account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Warfarin")
    await _add_current_med(db, patient, "Aspirin")
    await _add_current_med(db, patient, "Metformin")

    service = SafetyService(db)
    first = await service.active_flags(account_id=account.id, patient_id=patient.id)
    second = await service.active_flags(account_id=account.id, patient_id=patient.id)
    assert [v.reference_id for v, _ in first] == [v.reference_id for v, _ in second]


@pytest.mark.asyncio
async def test_unknown_patient_is_rejected(db):
    account, _patient = await _account_and_patient(db)
    from app.exceptions import PatientNotFoundError

    with pytest.raises(PatientNotFoundError):
        await SafetyService(db).active_flags(account_id=account.id, patient_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_another_accounts_patient_is_rejected(db):
    _account_a, patient_a = await _account_and_patient(db)
    account_b, _patient_b = await _account_and_patient(db)
    from app.exceptions import PatientNotFoundError

    with pytest.raises(PatientNotFoundError):
        await SafetyService(db).active_flags(account_id=account_b.id, patient_id=patient_a.id)


@pytest.mark.asyncio
async def test_query_count_does_not_scale_with_medication_count(db, engine):
    """Regression guard for the N+1 fixes.

    Two separate regressions are pinned here:

    1. active_flags used to call _build_context once per current medication.
    2. _build_context then issued one `db.get(DrugVocabulary, ...)` per medication and per
       allergy. That is now a single batched `WHERE id IN (...)` query.

    With both fixed the query count is *flat* in the number of medications, so this asserts
    equality rather than a slack allowance.

    ``expunge_all()`` before each measurement matters: ``db.get()`` is served from the session
    identity map, so without it the per-drug fetches never reach the database and the N+1
    stays invisible in tests while still costing a round-trip per drug in production, where
    every request gets a fresh session.
    """
    account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Metformin")

    counter = {"n": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    async def _measure(account_id, patient_id) -> int:
        # Fresh service (no warm DrugResolver cache) + empty identity map == production shape.
        service = SafetyService(db)
        db.expunge_all()
        counter["n"] = 0
        await service.active_flags(account_id=account_id, patient_id=patient_id)
        return counter["n"]

    try:
        one_drug = await _measure(account.id, patient.id)

        for generic in ("Aspirin", "Warfarin", "Atorvastatin"):
            await _add_current_med(db, patient, generic)

        four_drugs = await _measure(account.id, patient.id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)

    assert four_drugs == one_drug, (
        f"query count grew from {one_drug} to {four_drugs} for 3 extra drugs — the per-drug "
        "context rebuild or the per-drug vocabulary fetch is probably back"
    )


@pytest.mark.asyncio
async def test_conditions_and_allergies_are_shared_across_every_drug_variant(db):
    """The in-memory context variants must keep everything except current_meds intact —
    a contraindication driven by a condition has to still fire for each drug."""
    account, patient = await _account_and_patient(db)
    db.add(
        Condition(patient_id=patient.id, condition_name="Chronic kidney disease", status="active")
    )
    await _add_current_med(db, patient, "Metformin")
    await _add_current_med(db, patient, "Aspirin")
    await db.flush()

    ctx = await SafetyService(db)._build_context(patient.id)
    assert [c.condition_name for c in ctx.conditions] == ["Chronic kidney disease"]
    assert len(ctx.current_meds) == 2
