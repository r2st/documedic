"""Extraction values too large for their columns are fitted, not flushed as-is.

Everything a scan produces reaches the patient graph as a string or a number that nothing has
range-checked: OCR reads a smudged decimal point as twenty digits, a bad line break glues a
whole paragraph into one drug name. SQLite stores both; PostgreSQL rejects both, and the
rejection lands at ``flush`` — so it does not fail one bad line, it 500s the approval and rolls
back every entity that extracted correctly on the same document.

These tests pin the fitting logic and then sweep the merged rows with
``tests.column_fit.assert_fits_columns``, which applies the column limits SQLite ignores. The
matching end-to-end proof against a real PostgreSQL lives in ``test_postgres_column_bounds.py``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.graph_service import (
    _DECIMAL_CEILING,
    GraphService,
    _allowed_enum_values,
    _enum,
    _fitted,
    _to_decimal,
)
from tests.column_fit import assert_fits_columns, column_fit_violations

# Wider than any column the merge writes into, so one constant covers every string case.
OVERLONG = "X" * 900


# --------------------------------------------------------------------------------------
# _to_decimal: what the Numeric(18, 6) columns can hold
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "99999999999999999999999.5",  # the R27 report: 23 integer digits into 12
        1e23,
        "1E+400",  # past the decimal context, not just the column
        Decimal("1E+30"),
        "-99999999999999999999999.5",  # magnitude is what overflows, not sign
        10**12,  # exactly at the ceiling: 13 integer digits do not fit 12
    ],
)
def test_value_past_column_range_is_dropped(raw):
    assert _to_decimal(raw) is None


@pytest.mark.parametrize("raw", ["inf", "-inf", "Infinity", "nan", "NaN", "sNaN"])
def test_non_finite_value_is_dropped(raw):
    """A NaN compares false against every bound, so it would record as *not* abnormal."""
    assert _to_decimal(raw) is None


@pytest.mark.parametrize("raw", ["1e-400", "0.0000001", "-1e-400"])
def test_nonzero_value_that_underflows_scale_is_dropped(raw):
    """Scale 6 cannot tell these from zero, and zero is a different clinical reading.

    Storing ``0.000000`` would assert a precise value the source never gave — and for
    creatinine zero is exactly the value that makes eGFR undefined.
    """
    assert _to_decimal(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", Decimal("0.000000")),  # a real zero still stores; only underflow is dropped
        ("1.2", Decimal("1.200000")),
        ("999999999999.999999", Decimal("999999999999.999999")),  # the largest that fits
        (1.5, Decimal("1.500000")),
        ("-5.0", Decimal("-5.000000")),  # negatives are implausible, not unstorable
        ("0.1234567", Decimal("0.123457")),  # rounded to scale, not rejected
    ],
)
def test_value_within_column_range_is_kept(raw, expected):
    assert _to_decimal(raw) == expected


def test_kept_values_carry_the_column_scale():
    """Quantizing in the merge — not in the database — keeps the row and its dedup_key agreeing."""
    assert _to_decimal("1.2").as_tuple().exponent == -6


def test_ceiling_is_read_from_the_column_not_hardcoded():
    """Numeric(18, 6) leaves 12 integer digits; a schema change must move this with it."""
    assert _DECIMAL_CEILING == Decimal(10) ** 12
    assert _to_decimal(Decimal(10) ** 12 - 1) is not None


def test_bare_junk_is_still_dropped():
    for raw in ("", "  ", "abc", "12.3.4", "1,234"):
        assert _to_decimal(raw) is None


# --------------------------------------------------------------------------------------
# _fitted: what the String(n) columns can hold
# --------------------------------------------------------------------------------------


def test_fitted_trims_each_string_to_its_own_column():
    fitted = _fitted(
        MedicationEvent,
        brand_name_raw=OVERLONG,  # String(500)
        dose=OVERLONG,  # String(100)
        route=OVERLONG,  # String(50)
    )
    assert len(fitted["brand_name_raw"]) == 500
    assert len(fitted["dose"]) == 100
    assert len(fitted["route"]) == 50


def test_fitted_leaves_short_strings_and_non_strings_untouched():
    doc_id = uuid.uuid4()
    fitted = _fitted(
        LabResult,
        marker_name="Creatinine",
        value_numeric=Decimal("1.2"),
        source_document_id=doc_id,
        unit=None,
    )
    assert fitted["marker_name"] == "Creatinine"
    assert fitted["value_numeric"] == Decimal("1.2")
    assert fitted["source_document_id"] == doc_id
    assert fitted["unit"] is None


def test_fitted_does_not_touch_unbounded_text_columns():
    """``reaction_description`` is TEXT — trimming it would lose clinical detail for nothing."""
    fitted = _fitted(Allergy, reaction_description=OVERLONG)
    assert fitted["reaction_description"] == OVERLONG


def test_fitted_reads_limits_from_the_model():
    """A widened or narrowed column must not leave a stale constant behind in the service."""
    limit = Condition.__table__.c.condition_name.type.length
    assert len(_fitted(Condition, condition_name="Y" * (limit + 50))["condition_name"]) == limit


# --------------------------------------------------------------------------------------
# The merge end to end: every row it writes fits every column
# --------------------------------------------------------------------------------------


async def _patient(db) -> Patient:
    account = Account(email=f"{uuid.uuid4()}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id, full_name="Overflow Probe", sex="male", consent_given=True
    )
    db.add(patient)
    await db.flush()
    return patient


PATHOLOGICAL_ENTITIES: list[dict] = [
    {
        "entity_type": "medication",
        "fields": {
            "brand_name_raw": OVERLONG,
            "generic_name": OVERLONG,
            "dose": OVERLONG,
            "dose_unit": OVERLONG,
            "frequency": OVERLONG,
            "route": OVERLONG,
        },
    },
    {
        "entity_type": "lab_result",
        "fields": {
            "marker_name": OVERLONG,
            "value_numeric": "99999999999999999999999.5",
            "unit": OVERLONG,
            "reference_range_low": "1E+400",
            "reference_range_high": "inf",
        },
    },
    {
        "entity_type": "condition",
        "fields": {"condition_name": OVERLONG, "icd10_code": OVERLONG, "severity": OVERLONG},
    },
    {
        "entity_type": "allergy",
        "fields": {
            "allergen_name": OVERLONG,
            "allergen_type": "drug",
            "severity": OVERLONG,
            "reaction_description": OVERLONG,
        },
    },
]


@pytest.mark.asyncio
async def test_merge_of_pathological_extraction_fits_every_column(db):
    """The whole payload merges, and nothing it writes would be rejected by PostgreSQL."""
    patient = await _patient(db)

    counts = await GraphService(db).merge_entities(
        patient=patient, document=None, entities=PATHOLOGICAL_ENTITIES
    )
    # Nothing is refused: an unreadable line must not cost the clinician the rest of the chart.
    assert counts == {
        "medications": 1,
        "lab_results": 1,
        "conditions": 1,
        "allergies": 1,
        "encounters": 0,
    }
    await db.commit()

    rows: list[object] = []
    for model in (MedicationEvent, LabResult, Condition, Allergy):
        rows.extend((await db.execute(select(model))).scalars().all())
    assert len(rows) == 4
    assert_fits_columns(*rows)


@pytest.mark.asyncio
async def test_overflowing_lab_value_stays_qualitative_rather_than_wrong(db):
    """The reading is kept as text; only the unusable number is dropped.

    A row with no ``value_numeric`` is qualitative, and ``LabSafetyService`` already skips
    qualitative rows instead of coercing them — the correct handling for a value that could not
    be read. Recording the truncated number instead would put a wrong result in the chart.
    """
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "lab_result",
                "fields": {
                    "marker_name": "Potassium",
                    "value_numeric": "99999999999999999999999.5",
                    "unit": "mmol/L",
                    "reference_range_low": "3.5",
                    "reference_range_high": "5.0",
                },
            }
        ],
    )
    await db.commit()

    lab = (await db.execute(select(LabResult))).scalar_one()
    assert lab.value_numeric is None
    assert "99999999999999999999999.5" in lab.value_text
    # No number to compare, so no abnormality claim in either direction.
    assert lab.is_abnormal is None
    assert lab.abnormality_direction is None


@pytest.mark.asyncio
async def test_overlong_drug_name_still_resolves_nothing_after_trimming(db):
    """Trimming is safe because the vocabulary is resolved on the *full* name, before the trim.

    A 900-character run of OCR noise is not any real drug, so it resolved to nothing before it
    was shortened and must resolve to nothing after — the trim must not accidentally create a
    prefix that matches a real vocabulary entry.
    """
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[{"entity_type": "medication", "fields": {"brand_name_raw": OVERLONG}}],
    )
    await db.commit()

    med = (await db.execute(select(MedicationEvent))).scalar_one()
    assert med.drug_vocabulary_id is None
    assert len(med.brand_name_raw) == 500


@pytest.mark.asyncio
async def test_overlong_allergen_name_is_fitted(db):
    """Allergies drive the hard blocks, so this is the row that must never fail to flush."""
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "allergy",
                "fields": {"allergen_name": OVERLONG, "allergen_type": "drug"},
            }
        ],
    )
    await db.commit()

    allergy = (await db.execute(select(Allergy))).scalar_one()
    assert len(allergy.allergen_name) == Allergy.__table__.c.allergen_name.type.length
    assert_fits_columns(allergy)


@pytest.mark.asyncio
async def test_creatinine_that_overflows_derives_no_egfr(db):
    """An eGFR computed from an unreadable creatinine would be a fabricated marker."""
    patient = await _patient(db)
    patient.date_of_birth = __import__("datetime").date(1968, 5, 10)
    await db.flush()

    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "lab_result",
                "fields": {"marker_name": "Creatinine", "value_numeric": "1e-400"},
            }
        ],
    )
    await db.commit()

    from app.models.derived_marker import DerivedMarker

    assert (await db.execute(select(DerivedMarker))).scalars().all() == []


# --------------------------------------------------------------------------------------
# _enum: what the CHECK ... IN (...) constraints permit
# --------------------------------------------------------------------------------------


def test_allowed_values_are_read_from_the_check_constraints():
    """One source of truth: adding a severity level to the model must admit it here."""
    assert _allowed_enum_values(Allergy)["allergen_type"] == frozenset(
        {"drug", "food", "environmental", "other"}
    )
    assert _allowed_enum_values(Condition)["status"] == frozenset(
        {"active", "resolved", "inactive", "recurrence", "unknown"}
    )
    # Declared across two concatenated source lines and guarded by an IS NULL disjunct.
    assert _allowed_enum_values(Allergy)["severity"] == frozenset(
        {"mild", "moderate", "severe", "life_threatening", "unknown"}
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("severe", "severe"),
        ("Severe", "severe"),  # a vision model's capitalisation
        ("  severe  ", "severe"),
        ("life-threatening", "life_threatening"),  # hyphen, as it reads on a chart
        ("life threatening", "life_threatening"),
        ("moderate-severe", None),  # genuinely not one of the levels
        ("catastrophic", None),
        ("", None),
        (None, None),
    ],
)
def test_allergy_severity_is_normalised_or_dropped(raw, expected):
    assert _enum(Allergy, "severity", raw, None) == expected


def test_condition_status_distinguishes_absent_from_unreadable():
    """Absent keeps the column's long-standing default; unreadable must not assert 'active'."""
    assert _enum(Condition, "status", None, "active", "unknown") == "active"
    assert _enum(Condition, "status", "well controlled", "active", "unknown") == "unknown"
    assert _enum(Condition, "status", "resolved", "active", "unknown") == "resolved"


def test_unreadable_event_type_keeps_the_drug_safety_checked():
    """'continue' is the reading under which the patient is still on the drug."""
    assert _enum(MedicationEvent, "event_type", "titrating up", "continue") == "continue"
    assert _enum(MedicationEvent, "event_type", "stop", "continue") == "stop"


def test_non_string_enum_value_falls_back():
    """``FieldCorrection.value`` admits int/float/bool, and none of them are enum members."""
    for raw in (5, 1.5, True, [], {}):
        assert _enum(Allergy, "severity", raw, None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_type", "field_name", "bad_value"),
    [
        ("allergy", "severity", "very bad"),
        ("allergy", "allergen_type", "pharmaceutical"),
        ("condition", "status", "well controlled"),
        ("condition", "severity", "quite bad"),
        ("medication", "event_type", "titrating up"),
    ],
)
async def test_unlisted_enum_value_does_not_break_the_approval(
    db, entity_type, field_name, bad_value
):
    """The constraint used to reject these at flush, taking the whole document's merge with it."""
    patient = await _patient(db)
    name_field = {
        "allergy": "allergen_name",
        "condition": "condition_name",
        "medication": "brand_name_raw",
    }[entity_type]

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {"entity_type": entity_type, "fields": {name_field: "Probe", field_name: bad_value}}
        ],
    )
    assert sum(counts.values()) == 1
    await db.commit()  # the CHECK constraint is enforced here, on SQLite as well as PostgreSQL


@pytest.mark.asyncio
async def test_unreadable_allergen_type_still_resolves_against_the_vocabulary(db):
    """Falling back to 'drug' is what keeps an unreadable allergy inside the safety checks."""
    patient = await _patient(db)
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "allergy",
                "fields": {"allergen_name": "Crocin", "allergen_type": "pharmaceutical"},
            }
        ],
    )
    await db.commit()

    allergy = (await db.execute(select(Allergy))).scalar_one()
    assert allergy.allergen_type == "drug"
    # Resolved through the vocabulary despite the unreadable type, so the hard block still fires.
    assert allergy.drug_vocabulary_id is not None


# --------------------------------------------------------------------------------------
# The guard itself: prove these tests would notice a regression
# --------------------------------------------------------------------------------------


def test_column_fit_checker_catches_an_overlong_string():
    """Without this, the sweep above could pass by checking nothing."""
    violations = column_fit_violations(Allergy(allergen_name=OVERLONG, allergen_type="drug"))
    assert any("allergen_name" in v and "VARCHAR(500)" in v for v in violations)


def test_column_fit_checker_catches_an_out_of_range_numeric():
    violations = column_fit_violations(
        LabResult(marker_name="K", value_numeric=Decimal("99999999999999999999999.5"))
    )
    assert any("value_numeric" in v and "NUMERIC(18, 6)" in v for v in violations)


def test_column_fit_checker_catches_excess_scale():
    violations = column_fit_violations(
        LabResult(marker_name="K", value_numeric=Decimal("1.12345678901"))
    )
    assert any("scale" in v for v in violations)


def test_column_fit_checker_passes_a_clean_row():
    assert (
        column_fit_violations(
            LabResult(marker_name="Creatinine", value_numeric=Decimal("1.200000"), unit="mg/dL")
        )
        == []
    )
