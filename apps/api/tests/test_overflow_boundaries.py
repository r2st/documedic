"""Exact-boundary behaviour of the column fitting that R28 put in front of the merge.

``test_column_bounds.py`` proves the fitting works on values that are wildly out of range — a
900-character drug name, a 23-digit lab value. Those are the shapes a smudged scan actually
produces, and they are far enough past the limit that an off-by-one in the fitting would still
pass. This file pins the limit itself:

* the largest value ``Numeric(18, 6)`` can hold, and the first one it cannot;
* a value that only overflows *after* rounding, which fixes the order of quantize-then-check;
* a string of exactly the column's length, and the first one longer;
* every value each ``CHECK ... IN (...)`` permits, round-tripped through the normaliser that
  is supposed to admit them.

It also closes the write path the fitting never covered: ``DerivedMarker``, whose eGFR is the
one clinical number this service computes rather than reads, and which reaches a
``Numeric(18, 6)`` column without passing through ``_to_decimal``.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Numeric, String, select

from app.core.clinical import ckd_epi_2021_egfr
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.graph_service import (
    _DECIMAL_CEILING,
    _DECIMAL_QUANTUM,
    _ENUM_VALUES,
    GraphService,
    _enum,
    _fitted,
    _to_decimal,
)
from tests.column_fit import assert_fits_columns

MERGED_MODELS = (MedicationEvent, LabResult, Condition, Allergy)


async def _patient(db, **overrides) -> Patient:
    account = Account(email=f"{uuid.uuid4()}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    fields = {"full_name": "Boundary Probe", "sex": "male", "consent_given": True}
    fields.update(overrides)
    patient = Patient(account_id=account.id, **fields)
    db.add(patient)
    await db.flush()
    return patient


# --------------------------------------------------------------------------------------
# Numeric(18, 6): the last value in and the first value out
# --------------------------------------------------------------------------------------


def test_the_largest_storable_value_is_kept_exactly():
    """One quantum below the ceiling, with every one of the 18 digits significant."""
    largest = _DECIMAL_CEILING - _DECIMAL_QUANTUM
    assert largest == Decimal("999999999999.999999")
    assert _to_decimal(str(largest)) == largest
    assert_fits_columns(LabResult(marker_name="K", value_numeric=_to_decimal(str(largest))))


def test_the_first_value_past_the_ceiling_is_dropped():
    """The ceiling is exclusive: 10^12 needs a 13th integer digit and there are only 12."""
    assert _to_decimal(str(_DECIMAL_CEILING)) is None
    assert _to_decimal(str(_DECIMAL_CEILING - _DECIMAL_QUANTUM)) is not None


def test_a_value_that_only_overflows_once_rounded_is_dropped():
    """Rounding happens before the range check, and it can push a value over the edge.

    ``999999999999.9999996`` fits the column's *range* as written and stops fitting once
    quantized to scale 6. Checking the range first would admit it and then hand PostgreSQL
    ``1000000000000.000000``, which is the overflow this fitting exists to prevent.
    """
    assert _to_decimal("999999999999.9999996") is None
    assert _to_decimal("-999999999999.9999996") is None
    # One digit lower rounds down and stays inside.
    assert _to_decimal("999999999999.9999994") == Decimal("999999999999.999999")


def test_the_smallest_storable_magnitude_is_kept():
    """One quantum is a real reading; anything smaller cannot be told from zero."""
    assert _to_decimal(str(_DECIMAL_QUANTUM)) == Decimal("0.000001")
    assert _to_decimal(str(-_DECIMAL_QUANTUM)) == Decimal("-0.000001")


@pytest.mark.parametrize("raw", ["0.0000005", "-0.0000005", "0.0000004", "0.00000049999"])
def test_a_magnitude_below_half_a_quantum_is_dropped_not_rounded_to_zero(raw):
    """Banker's rounding takes 5e-7 to zero, and a stored zero is a different reading.

    This is the boundary of the underflow rule in ``_to_decimal``: everything here is nonzero
    in the source and zero after quantizing, so it is dropped rather than recorded as an exact
    ``0.000000`` the report never stated.
    """
    assert _to_decimal(raw) is None


def test_a_true_zero_is_still_stored():
    """Only *nonzero* values that quantize to zero are dropped; zero itself is a reading."""
    assert _to_decimal("0") == Decimal("0.000000")
    assert _to_decimal("-0.0") == Decimal("0.000000")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1E+11", Decimal("100000000000.000000")),  # 12 integer digits: the last that fits
        ("1E+12", None),  # 13: the first that does not
        ("9.99999E+11", Decimal("999999000000.000000")),
        ("1.5E-6", Decimal("0.000002")),  # exponent notation still lands on the quantum
        ("1.5E-7", None),
    ],
)
def test_exponent_notation_is_bounded_by_the_same_edges(raw, expected):
    """Vision models emit ``1E+12`` as readily as ``1000000000000``; both must be judged alike."""
    assert _to_decimal(raw) == expected


# --------------------------------------------------------------------------------------
# String(n): the last string in and the first string out
# --------------------------------------------------------------------------------------


def _bounded_string_columns(model: type) -> list[tuple[str, int]]:
    return [
        (column.key, column.type.length)
        for column in model.__table__.c
        if isinstance(column.type, String) and column.type.length
    ]


@pytest.mark.parametrize("model", MERGED_MODELS, ids=lambda m: m.__name__)
def test_a_string_of_exactly_the_column_length_is_left_alone(model):
    """Trimming at ``>= limit`` instead of ``> limit`` would silently shorten a legal value."""
    for name, limit in _bounded_string_columns(model):
        exact = "a" * limit
        assert _fitted(model, **{name: exact})[name] == exact


@pytest.mark.parametrize("model", MERGED_MODELS, ids=lambda m: m.__name__)
def test_one_character_past_the_column_length_is_trimmed_to_it(model):
    """The off-by-one in the other direction: 501 characters must come back as 500."""
    for name, limit in _bounded_string_columns(model):
        assert len(_fitted(model, **{name: "a" * (limit + 1)})[name]) == limit


def test_the_limit_is_counted_in_characters_not_bytes():
    """PostgreSQL's ``varchar(n)`` counts characters, so a Devanagari name gets the full 500.

    Counting bytes would trim an Indian-language allergen or condition name to a third of the
    column, and — worse — could cut a multi-byte character in half.
    """
    limit = Allergy.__table__.c.allergen_name.type.length
    devanagari = "क" * limit
    fitted = _fitted(Allergy, allergen_name=devanagari)["allergen_name"]
    assert fitted == devanagari
    assert len(fitted) == limit
    assert len(fitted.encode("utf-8")) == limit * 3
    assert_fits_columns(Allergy(allergen_name=fitted, allergen_type="drug"))


def test_trimming_never_splits_a_character():
    """Slicing a ``str`` cuts codepoints, so the trimmed value is still decodable text."""
    limit = Condition.__table__.c.condition_name.type.length
    fitted = _fitted(Condition, condition_name="मधुमेह" * limit)["condition_name"]
    assert len(fitted) == limit
    assert fitted.encode("utf-8").decode("utf-8") == fitted


@pytest.mark.asyncio
async def test_a_name_at_exactly_the_limit_survives_the_merge_unchanged(db):
    """End to end: the fitting must not shorten a legal 500-character name on the way in."""
    patient = await _patient(db)
    exact = "P" * Allergy.__table__.c.allergen_name.type.length
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {"entity_type": "allergy", "fields": {"allergen_name": exact, "severity": "severe"}}
        ],
    )
    await db.commit()

    allergy = (await db.execute(select(Allergy))).scalar_one()
    assert allergy.allergen_name == exact
    assert_fits_columns(allergy)


# --------------------------------------------------------------------------------------
# CHECK ... IN (...): every value the constraint permits, and the near-misses it does not
# --------------------------------------------------------------------------------------


def test_every_check_constrained_column_the_merge_writes_is_normalised():
    """The models the merge writes and the models ``_enum`` knows about must not drift.

    ``_enum`` looks its allowed set up by ``model.__name__``, so a model added to the merge but
    not to ``_ENUM_VALUES`` raises ``KeyError`` at the first extraction that reaches it.
    """
    assert set(_ENUM_VALUES) == {model.__name__ for model in MERGED_MODELS}
    for model in MERGED_MODELS:
        assert _ENUM_VALUES[model.__name__], f"{model.__name__} has no CHECK ... IN (...) read"


@pytest.mark.parametrize(
    ("model", "column", "value"),
    [
        (model, column, value)
        for model in MERGED_MODELS
        for column, values in _ENUM_VALUES[model.__name__].items()
        for value in sorted(values)
    ],
    ids=lambda arg: arg.__name__ if isinstance(arg, type) else str(arg),
)
def test_every_permitted_enum_value_survives_normalisation(model, column, value):
    """Totality: normalisation must admit each value verbatim, not just the ones with tests.

    Underscored members (``life_threatening``, ``one_time``, ``critical_high``) are the ones at
    risk here — the normaliser rewrites spaces and hyphens *into* underscores, and a rule that
    also touched underscores would drop a value the constraint permits.
    """
    assert _enum(model, column, value, None) == value


@pytest.mark.parametrize(
    ("model", "column", "value"),
    [
        (Allergy, "severity", "severes"),  # a trailing character, not a new level
        (Allergy, "allergen_type", "drugs"),
        (Condition, "status", "in_active"),  # an underscore in the wrong place
        (Condition, "severity", "mild_moderate"),
        (MedicationEvent, "event_type", "one_time_only"),
    ],
)
def test_a_near_miss_is_not_admitted_by_normalisation(model, column, value):
    """Normalisation folds case and separators; it must not fold a *different* word."""
    assert _enum(model, column, value, None) is None


@pytest.mark.parametrize(
    "raw", ["life threatening", "life-threatening", "LIFE_THREATENING", " Life Threatening "]
)
def test_the_spellings_a_scan_actually_produces_all_reach_the_constraint(raw):
    """A chart writes the multiword level any of these ways; all of them mean the same thing."""
    assert _enum(Allergy, "severity", raw, None) == "life_threatening"


@pytest.mark.parametrize("raw", ["life  threatening", "life - threatening", "life_ threatening"])
def test_a_doubled_separator_is_not_silently_collapsed(raw):
    """Each separator maps to one underscore, so a doubled one is a value nobody wrote.

    Dropping it is the safe direction: ``severity`` is nullable, and an absent severity is
    honest where a guessed ``life_threatening`` would drive a hard block on a guess.
    """
    assert _enum(Allergy, "severity", raw, None) is None


# --------------------------------------------------------------------------------------
# DerivedMarker: the numeric column the fitting did not cover
# --------------------------------------------------------------------------------------


def test_egfr_fits_the_column_across_the_whole_storable_creatinine_range():
    """The derived value is computed, not extracted, so its range is the formula's to prove.

    Both ends are checked because the exponent is negative: the *smallest* storable creatinine
    produces the largest eGFR, which is the end that could overflow ``Numeric(18, 6)``.
    """
    smallest = float(_DECIMAL_QUANTUM)
    largest = float(_DECIMAL_CEILING - _DECIMAL_QUANTUM)
    for creatinine in (smallest, 0.01, 1.0, 20.0, largest):
        for age in (1, 120):
            for sex in ("male", "female"):
                result = ckd_epi_2021_egfr(creatinine_mg_dl=creatinine, age_years=age, sex=sex)
                assert_fits_columns(
                    DerivedMarker(marker_name="eGFR", value_numeric=Decimal(str(result.value)))
                )


@pytest.mark.asyncio
async def test_an_egfr_that_rounds_to_zero_is_not_recorded(db):
    """eGFR is strictly positive, so ``0.00`` is a rounding artefact, not renal failure.

    It takes a creatinine over ~4000 mg/dL to get there — a value no assay produces and no
    patient survives, so it is a misread decimal point rather than a reading. Storing the zero
    would put a precise number in the chart that the formula never supported, and it would be
    flagged abnormal, which is the same defect ``_to_decimal`` refuses for extracted values.
    """
    patient = await _patient(db, date_of_birth=date(1968, 5, 10))
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "lab_result",
                "fields": {"marker_name": "Creatinine", "value_numeric": "99999999.5"},
            }
        ],
    )
    await db.commit()

    # The creatinine itself is storable, so the lab row is kept ...
    lab = (await db.execute(select(LabResult))).scalar_one()
    assert lab.value_numeric == Decimal("99999999.500000")
    # ... but nothing computable comes out of it.
    assert (await db.execute(select(DerivedMarker))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_plausible_creatinine_still_derives_an_egfr(db):
    """The guard above must not cost the ordinary case its derived marker."""
    patient = await _patient(db, date_of_birth=date(1968, 5, 10))
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "lab_result",
                "fields": {"marker_name": "Creatinine", "value_numeric": "1.4"},
            }
        ],
    )
    await db.commit()

    marker = (await db.execute(select(DerivedMarker))).scalar_one()
    assert marker.marker_name == "eGFR"
    assert marker.value_numeric > 0
    assert_fits_columns(marker)


@pytest.mark.asyncio
async def test_a_dialysis_range_creatinine_still_derives_an_egfr(db):
    """The lowest eGFR a real patient reaches is single digits, well clear of the guard."""
    patient = await _patient(db, date_of_birth=date(1968, 5, 10))
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "lab_result",
                "fields": {"marker_name": "Creatinine", "value_numeric": "18.0"},
            }
        ],
    )
    await db.commit()

    marker = (await db.execute(select(DerivedMarker))).scalar_one()
    assert 0 < marker.value_numeric < 10
    assert marker.is_abnormal is True


@pytest.mark.asyncio
async def test_every_derived_marker_row_fits_its_columns(db):
    """The sweep ``test_column_bounds`` runs over merged rows, extended to computed ones."""
    patient = await _patient(db, date_of_birth=date(1930, 1, 1))
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            {
                "entity_type": "lab_result",
                "fields": {"marker_name": f"Serum Creatinine {i}", "value_numeric": value},
            }
            for i, value in enumerate(["0.000001", "0.4", "1.2", "9.9", "600"])
        ],
    )
    await db.commit()

    markers = (await db.execute(select(DerivedMarker))).scalars().all()
    assert len(markers) == 5
    assert_fits_columns(*markers)


# --------------------------------------------------------------------------------------
# The structural guard: nothing reaches a column without being fitted first
# --------------------------------------------------------------------------------------

GRAPH_SERVICE = Path(__file__).resolve().parents[1] / "app" / "services" / "graph_service.py"

# Every mapped model this service instantiates. ``DerivedMarker`` was constructed with bare
# keyword arguments while the other four went through ``_fitted``, which is exactly the drift
# this test exists to catch: it is the one row whose values the service computes itself, so it
# looked safe by inspection and had no test holding it to the same rule.
CONSTRUCTED_MODELS = {"MedicationEvent", "LabResult", "Condition", "Allergy", "DerivedMarker"}


def _model_constructions(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in CONSTRUCTED_MODELS
    ]


def test_every_model_the_merge_constructs_is_fitted_to_its_columns():
    """A row built with bare keyword arguments is one OCR artefact away from a 500 at flush.

    Static, because the failure only appears on PostgreSQL: SQLite accepts an over-long string
    and an out-of-range numeric silently, so a new field added straight to a constructor passes
    the whole suite and breaks the deploy.
    """
    tree = ast.parse(GRAPH_SERVICE.read_text())
    constructions = _model_constructions(tree)
    assert len(constructions) == len(CONSTRUCTED_MODELS), "a model construction moved or was added"

    for call in constructions:
        model_name = call.func.id  # type: ignore[attr-defined]
        assert len(call.keywords) == 1 and call.keywords[0].arg is None, (
            f"{model_name}(...) at line {call.lineno} passes values outside _fitted(); those "
            f"reach the column untrimmed"
        )
        fitted_call = call.keywords[0].value
        assert (
            isinstance(fitted_call, ast.Call)
            and isinstance(fitted_call.func, ast.Name)
            and fitted_call.func.id == "_fitted"
        ), (
            f"{model_name}(...) at line {call.lineno} does not route its values through "
            f"_fitted(); an over-long string would reach the column unchanged"
        )
        first_arg = fitted_call.args[0]
        assert isinstance(first_arg, ast.Name) and first_arg.id == model_name, (
            f"{model_name}(...) at line {call.lineno} is fitted against the wrong model, so the "
            f"limits applied are another table's"
        )


def _names_bound_to(tree: ast.AST, function_name: str) -> set[str]:
    """Local names assigned the result of ``function_name(...)`` anywhere in the module."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == function_name
        ):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return bound


def test_the_numeric_columns_are_written_from_to_decimal_only():
    """Every ``Numeric`` value assigned in the service comes out of the range check.

    ``_fitted`` trims strings; it does not range-check numbers. So the guard above is necessary
    for the ``VARCHAR`` columns and not sufficient for the ``NUMERIC`` ones — a magnitude past
    the column's range still reaches PostgreSQL and still fails the whole approval at flush.
    """
    numeric_columns = {
        column.key
        for model in (*MERGED_MODELS, DerivedMarker)
        for column in model.__table__.c
        if isinstance(column.type, Numeric)
    }
    tree = ast.parse(GRAPH_SERVICE.read_text())
    range_checked = _names_bound_to(tree, "_to_decimal")
    assert range_checked, "no name is bound from _to_decimal(); this test is checking nothing"

    for call in _model_constructions(tree):
        fitted_call = call.keywords[0].value
        assert isinstance(fitted_call, ast.Call)
        for kw in fitted_call.keywords:
            if kw.arg not in numeric_columns:
                continue
            value = kw.value
            direct = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "_to_decimal"
            )
            # A literal reference bound (``Decimal("90")`` for the eGFR floor) is a constant
            # this file already knows fits; anything else must be a range-checked name.
            constant = isinstance(value, ast.Call) and ast.unparse(value).startswith("Decimal(")
            assert (
                direct or constant or (isinstance(value, ast.Name) and value.id in range_checked)
            ), (
                f"{kw.arg} at line {value.lineno} is written to a NUMERIC column without "
                f"passing through _to_decimal()"
            )


@pytest.mark.asyncio
async def test_the_boundary_sweep_would_notice_an_unfitted_write(db):
    """Proof the checker above has teeth: an unfitted row is caught by the same sweep."""
    from tests.column_fit import column_fit_violations

    unfitted = DerivedMarker(
        patient_id=uuid.uuid4(),
        marker_name="eGFR",
        value_numeric=Decimal("1000000000000.0"),
        unit="x" * 200,
        computed_at=datetime.now(UTC),
    )
    violations = column_fit_violations(unfitted)
    assert any("value_numeric" in v for v in violations)
    assert any("unit" in v for v in violations)
