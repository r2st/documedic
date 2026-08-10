"""Patient-graph merge: deduplication, drug resolution, and derived-marker computation.

The merge runs on clinician-approved extraction output, so it must be forgiving of ragged
LLM/OCR field shapes (missing keys, string numbers, unparseable dates) without either
crashing or silently writing junk into the longitudinal record.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.graph_service import GraphService, _parse_date, _parse_datetime, _to_decimal


async def _patient(db, **overrides) -> Patient:
    account = Account(email=f"graph-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    fields = {
        "full_name": "Graph Patient",
        "sex": "male",
        "date_of_birth": datetime(1970, 1, 1).date(),
        "consent_given": True,
        "consent_given_at": datetime.now(UTC),
    }
    fields.update(overrides)
    patient = Patient(account_id=account.id, **fields)
    db.add(patient)
    await db.flush()
    return patient


def _entity(etype: str, **fields) -> dict:
    return {"entity_type": etype, "fields": fields, "confidence": {}}


async def _rows(db, model, patient):
    result = await db.execute(select(model).where(model.patient_id == patient.id))
    return list(result.scalars().all())


# --- Medications -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_indian_brand_name_resolves_to_its_generic(db):
    """Never string-match drugs — a brand must resolve through DrugVocabulary."""
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("medication", brand_name_raw="Crocin", dose="650")],
    )
    assert counts["medications"] == 1
    med = (await _rows(db, MedicationEvent, patient))[0]
    assert med.brand_name_raw == "Crocin"
    assert med.generic_name and med.generic_name.lower().startswith("paracetamol")
    assert med.drug_vocabulary_id is not None


@pytest.mark.asyncio
async def test_the_same_current_medication_is_not_duplicated(db):
    patient = await _patient(db)
    service = GraphService(db)
    entity = [_entity("medication", brand_name_raw="Crocin", dose="650")]

    assert (await service.merge_entities(patient=patient, document=None, entities=entity))[
        "medications"
    ] == 1
    assert (await service.merge_entities(patient=patient, document=None, entities=entity))[
        "medications"
    ] == 0
    assert len(await _rows(db, MedicationEvent, patient)) == 1


@pytest.mark.asyncio
async def test_a_different_dose_of_the_same_drug_is_a_new_event(db):
    """A dose change is clinically meaningful — it must not be swallowed as a duplicate."""
    patient = await _patient(db)
    service = GraphService(db)
    await service.merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("medication", brand_name_raw="Crocin", dose="650")],
    )
    counts = await service.merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("medication", brand_name_raw="Crocin", dose="500")],
    )
    assert counts["medications"] == 1
    assert len(await _rows(db, MedicationEvent, patient)) == 2


@pytest.mark.asyncio
async def test_an_unresolvable_drug_is_still_recorded(db):
    """An unknown brand must not be dropped — the clinician approved it, so it belongs in
    the record even without a vocabulary mapping (the safety engine just can't check it)."""
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("medication", brand_name_raw="Zzyxtroban", generic_name="Zzyxtroban")],
    )
    assert counts["medications"] == 1
    med = (await _rows(db, MedicationEvent, patient))[0]
    assert med.drug_vocabulary_id is None


@pytest.mark.asyncio
async def test_merged_entities_are_marked_clinician_confirmed(db):
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient, document=None, entities=[_entity("medication", brand_name_raw="Crocin")]
    )
    med = (await _rows(db, MedicationEvent, patient))[0]
    assert med.clinician_confirmed is True
    assert med.clinician_confirmed_at is not None


@pytest.mark.asyncio
async def test_event_type_defaults_to_continue(db):
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient, document=None, entities=[_entity("medication", brand_name_raw="Crocin")]
    )
    assert (await _rows(db, MedicationEvent, patient))[0].event_type == "continue"


# --- Lab results -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_value_above_the_reference_range_is_flagged_high(db):
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity(
                "lab_result",
                marker_name="HbA1c",
                value_numeric=9.2,
                unit="%",
                reference_range_low=4.0,
                reference_range_high=5.6,
            )
        ],
    )
    lab = (await _rows(db, LabResult, patient))[0]
    assert lab.is_abnormal is True
    assert lab.abnormality_direction == "high"


@pytest.mark.asyncio
async def test_a_value_below_the_reference_range_is_flagged_low(db):
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity(
                "lab_result",
                marker_name="Haemoglobin",
                value_numeric=7.1,
                reference_range_low=13.0,
                reference_range_high=17.0,
            )
        ],
    )
    lab = (await _rows(db, LabResult, patient))[0]
    assert lab.is_abnormal is True
    assert lab.abnormality_direction == "low"


@pytest.mark.asyncio
async def test_a_value_inside_the_range_is_explicitly_normal(db):
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity(
                "lab_result",
                marker_name="Sodium",
                value_numeric=140,
                reference_range_low=135,
                reference_range_high=145,
            )
        ],
    )
    lab = (await _rows(db, LabResult, patient))[0]
    assert lab.is_abnormal is False
    assert lab.abnormality_direction is None


@pytest.mark.asyncio
async def test_abnormality_is_unknown_without_a_reference_range(db):
    """No range means no verdict — inventing 'normal' would be a false reassurance."""
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("lab_result", marker_name="Ferritin", value_numeric=12)],
    )
    assert (await _rows(db, LabResult, patient))[0].is_abnormal is None


@pytest.mark.asyncio
async def test_a_lab_without_a_marker_name_is_skipped(db):
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient, document=None, entities=[_entity("lab_result", value_numeric=5)]
    )
    assert counts["lab_results"] == 0
    assert await _rows(db, LabResult, patient) == []


@pytest.mark.asyncio
async def test_a_non_numeric_lab_value_is_kept_as_text(db):
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("lab_result", marker_name="Dengue NS1", value_text="Reactive")],
    )
    lab = (await _rows(db, LabResult, patient))[0]
    assert lab.value_numeric is None
    assert lab.value_text == "Reactive"


# --- Conditions and allergies ----------------------------------------------------------


@pytest.mark.asyncio
async def test_conditions_deduplicate_case_insensitively(db):
    patient = await _patient(db)
    service = GraphService(db)
    await service.merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("condition", condition_name="Hypertension")],
    )
    counts = await service.merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("condition", condition_name="HYPERTENSION")],
    )
    assert counts["conditions"] == 0
    assert len(await _rows(db, Condition, patient)) == 1


@pytest.mark.asyncio
async def test_a_condition_without_a_name_is_skipped(db):
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient, document=None, entities=[_entity("condition", status="active")]
    )
    assert counts["conditions"] == 0


@pytest.mark.asyncio
async def test_a_drug_allergy_resolves_to_the_vocabulary(db):
    """Critical: an allergy has to carry a reference id or the hard-block check can't match
    a brand-name prescription against it."""
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("allergy", allergen_name="Crocin", reaction_description="Rash")],
    )
    allergy = (await _rows(db, Allergy, patient))[0]
    assert allergy.drug_vocabulary_id is not None
    assert allergy.status == "active"


@pytest.mark.asyncio
async def test_a_non_drug_allergy_skips_drug_resolution(db):
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("allergy", allergen_name="Peanuts", allergen_type="food")],
    )
    allergy = (await _rows(db, Allergy, patient))[0]
    assert allergy.allergen_type == "food"
    assert allergy.drug_vocabulary_id is None


@pytest.mark.asyncio
async def test_allergies_deduplicate_case_insensitively(db):
    patient = await _patient(db)
    service = GraphService(db)
    await service.merge_entities(
        patient=patient, document=None, entities=[_entity("allergy", allergen_name="Penicillin")]
    )
    counts = await service.merge_entities(
        patient=patient, document=None, entities=[_entity("allergy", allergen_name="penicillin")]
    )
    assert counts["allergies"] == 0


@pytest.mark.asyncio
async def test_an_allergy_without_a_name_is_skipped(db):
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient, document=None, entities=[_entity("allergy", severity="severe")]
    )
    assert counts["allergies"] == 0


# --- Derived markers -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creatinine_computes_an_egfr_marker(db):
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("lab_result", marker_name="Serum creatinine", value_numeric=1.4, unit="mg/dL")
        ],
    )
    markers = await _rows(db, DerivedMarker, patient)
    assert len(markers) == 1
    assert markers[0].marker_name == "eGFR"
    assert markers[0].formula_name  # provenance is required for a computed clinical value
    assert markers[0].input_values


@pytest.mark.asyncio
async def test_no_egfr_without_a_date_of_birth(db):
    """CKD-EPI needs age; guessing one would produce a wrong renal-dosing input."""
    patient = await _patient(db, date_of_birth=None)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("lab_result", marker_name="Serum creatinine", value_numeric=1.4)],
    )
    assert await _rows(db, DerivedMarker, patient) == []


@pytest.mark.asyncio
async def test_no_egfr_when_sex_is_unrecorded(db):
    patient = await _patient(db, sex="other")
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("lab_result", marker_name="Serum creatinine", value_numeric=1.4)],
    )
    assert await _rows(db, DerivedMarker, patient) == []


@pytest.mark.asyncio
async def test_non_creatinine_labs_produce_no_derived_marker(db):
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("lab_result", marker_name="HbA1c", value_numeric=7.0)],
    )
    assert await _rows(db, DerivedMarker, patient) == []


# --- Mixed batches and coercion helpers ------------------------------------------------


@pytest.mark.asyncio
async def test_a_mixed_batch_counts_each_type(db):
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("medication", brand_name_raw="Crocin"),
            _entity("lab_result", marker_name="HbA1c", value_numeric=7.4),
            _entity("condition", condition_name="Type 2 diabetes mellitus"),
            _entity("allergy", allergen_name="Sulfa"),
            _entity("unknown_type", whatever="ignored"),
        ],
    )
    assert counts == {"medications": 1, "lab_results": 1, "conditions": 1, "allergies": 1}


@pytest.mark.asyncio
async def test_an_empty_batch_is_a_no_op(db):
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(patient=patient, document=None, entities=[])
    assert counts == {"medications": 0, "lab_results": 0, "conditions": 0, "allergies": 0}


def test_to_decimal_accepts_numeric_strings():
    assert _to_decimal("7.4") == Decimal("7.4")
    assert _to_decimal(7) == Decimal("7")


def test_to_decimal_returns_none_for_junk():
    assert _to_decimal(None) is None
    assert _to_decimal("not a number") is None
    assert _to_decimal("") is None


def test_parse_date_prefers_day_first_for_indian_documents():
    """05/06/2026 on an Indian prescription is 5 June, not 6 May."""
    parsed = _parse_date("05/06/2026")
    assert (parsed.day, parsed.month) == (5, 6)


def test_parse_date_returns_none_for_unparseable_input():
    assert _parse_date("illegible") is None
    assert _parse_date(None) is None


def test_parse_datetime_assumes_utc_when_no_offset_is_given():
    parsed = _parse_datetime("2026-03-01 09:30")
    assert parsed is not None and parsed.tzinfo is not None


def test_parse_datetime_returns_none_for_unparseable_input():
    assert _parse_datetime("smudged") is None
    assert _parse_datetime(None) is None
