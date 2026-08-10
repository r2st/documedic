"""Within-document deduplication and query-count behaviour of the graph merge.

The merge used to re-query the patient's medications/conditions/allergies once per entity,
which was both an N+1 (a 12-line prescription issued 12 full scans) and a correctness hole:
because the session runs with autoflush off, a row added earlier in the same batch was not
visible to the next entity's SELECT, so a document listing the same drug twice produced two
medication rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select

from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.graph_service import GraphService


async def _patient(db) -> Patient:
    account = Account(email=f"dedup-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Dedup Patient",
        sex="male",
        date_of_birth=datetime(1970, 1, 1).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


def _entity(etype: str, **fields) -> dict:
    return {"entity_type": etype, "fields": fields, "confidence": {}}


async def _rows(db, model, patient) -> list:
    result = await db.execute(select(model).where(model.patient_id == patient.id))
    return list(result.scalars().all())


class _SelectCounter:
    """Counts SELECT statements issued against a given table while active."""

    def __init__(self, engine, table: str) -> None:
        self.sync_engine = engine.sync_engine
        self.table = table
        self.count = 0

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        normalised = " ".join(statement.split()).lower()
        if normalised.startswith("select") and f" {self.table}" in normalised:
            self.count += 1

    def __enter__(self):
        event.listen(self.sync_engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        event.remove(self.sync_engine, "before_cursor_execute", self._on_execute)
        return False


@pytest.mark.asyncio
async def test_a_drug_listed_twice_in_one_document_is_merged_once(db):
    """OCR frequently repeats a line; the record must not gain a phantom second course."""
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("medication", brand_name_raw="Crocin", dose="650"),
            _entity("medication", brand_name_raw="Crocin", dose="650"),
        ],
    )
    assert counts["medications"] == 1
    assert len(await _rows(db, MedicationEvent, patient)) == 1


@pytest.mark.asyncio
async def test_two_doses_of_the_same_drug_in_one_document_are_both_kept(db):
    """A titration written on one prescription is two distinct clinical facts."""
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("medication", brand_name_raw="Crocin", dose="650"),
            _entity("medication", brand_name_raw="Crocin", dose="500"),
        ],
    )
    assert counts["medications"] == 2
    assert len(await _rows(db, MedicationEvent, patient)) == 2


@pytest.mark.asyncio
async def test_a_brand_and_its_generic_in_one_document_collapse_to_one_medication(db):
    """Dedup keys off the resolved generic, so "Crocin" and "Paracetamol" are one drug."""
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("medication", brand_name_raw="Crocin", dose="650"),
            _entity("medication", brand_name_raw="Paracetamol", dose="650"),
        ],
    )
    assert counts["medications"] == 1


@pytest.mark.asyncio
async def test_a_condition_listed_twice_in_one_document_is_merged_once(db):
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("condition", condition_name="Hypertension"),
            _entity("condition", condition_name="HYPERTENSION"),
        ],
    )
    assert counts["conditions"] == 1
    assert len(await _rows(db, Condition, patient)) == 1


@pytest.mark.asyncio
async def test_an_allergy_listed_twice_in_one_document_is_merged_once(db):
    """A duplicated allergy line must not inflate the hard-block surface."""
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("allergy", allergen_name="Penicillin", severity="severe"),
            _entity("allergy", allergen_name="penicillin", severity="mild"),
        ],
    )
    assert counts["allergies"] == 1
    assert len(await _rows(db, Allergy, patient)) == 1


@pytest.mark.asyncio
async def test_dedup_still_applies_across_separate_documents(db):
    """The in-batch key set must not weaken the existing cross-document dedup."""
    patient = await _patient(db)
    service = GraphService(db)
    first = [_entity("medication", brand_name_raw="Crocin", dose="650")]
    assert (await service.merge_entities(patient=patient, document=None, entities=first))[
        "medications"
    ] == 1
    assert (await service.merge_entities(patient=patient, document=None, entities=first))[
        "medications"
    ] == 0


@pytest.mark.asyncio
async def test_medication_lookups_do_not_scale_with_the_number_of_entities(db, engine):
    """Regression guard for the N+1: the dedup set is loaded once per merge, not per line."""
    patient = await _patient(db)
    entities = [
        _entity("medication", brand_name_raw=brand, dose="500")
        for brand in ("Crocin", "Glycomet", "Brufen", "Augmentin", "Amlodipine", "Atorvastatin")
    ]

    with _SelectCounter(engine, "medication_events") as counter:
        await GraphService(db).merge_entities(patient=patient, document=None, entities=entities)

    assert counter.count == 1, (
        f"{len(entities)} medications triggered {counter.count} medication_events SELECTs; "
        "the dedup key set must be loaded once per merge"
    )


@pytest.mark.asyncio
async def test_condition_and_allergy_lookups_are_also_loaded_once(db, engine):
    patient = await _patient(db)
    entities = [
        _entity("condition", condition_name=name)
        for name in ("Hypertension", "Type 2 Diabetes", "CKD stage 3", "Asthma")
    ] + [_entity("allergy", allergen_name=name) for name in ("Penicillin", "Sulfa", "Aspirin")]

    with _SelectCounter(engine, "conditions") as conditions:
        with _SelectCounter(engine, "allergies") as allergies:
            await GraphService(db).merge_entities(patient=patient, document=None, entities=entities)

    assert conditions.count == 1
    assert allergies.count == 1
