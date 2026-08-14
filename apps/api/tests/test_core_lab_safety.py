"""Unit tests for the deterministic critical/panic lab-value guard (no DB, no LLM)."""

from __future__ import annotations

import pytest

from app.core.lab_safety import evaluate_critical_value


def test_normal_potassium_no_flag():
    assert evaluate_critical_value("Potassium", 4.2, "mmol/L") is None


def test_panic_low_potassium():
    flag = evaluate_critical_value("Potassium", 2.3, "mmol/L")
    assert flag is not None
    assert flag.severity == "panic_low"
    assert "Panic" in flag.summary


def test_critical_high_potassium_not_panic():
    flag = evaluate_critical_value("K+", 6.2, "mmol/L")
    assert flag is not None
    assert flag.severity == "critical_high"


def test_panic_high_potassium():
    flag = evaluate_critical_value("Serum Potassium", 7.0, "mmol/L")
    assert flag is not None
    assert flag.severity == "panic_high"


def test_unrecognised_marker_returns_none():
    assert evaluate_critical_value("Some Obscure Marker", 999, "unit") is None


def test_missing_value_returns_none():
    assert evaluate_critical_value("Potassium", None, "mmol/L") is None


def test_glucose_mgdl_hypoglycemia_panic():
    flag = evaluate_critical_value("FBS", 35, "mg/dL")
    assert flag is not None
    assert flag.severity == "panic_low"


def test_glucose_mmol_l_conversion_hypoglycemia():
    # 35 mg/dL ≈ 1.94 mmol/L
    flag = evaluate_critical_value("Glucose", 1.9, "mmol/L")
    assert flag is not None
    assert flag.severity == "panic_low"


def test_glucose_unrecognised_unit_is_skipped_not_guessed():
    flag = evaluate_critical_value("Glucose", 35, "furlongs")
    assert flag is None


def test_creatinine_panic_high():
    flag = evaluate_critical_value("Serum Creatinine", 11.0, "mg/dL")
    assert flag is not None
    assert flag.severity == "panic_high"


def test_hemoglobin_critical_low():
    flag = evaluate_critical_value("Hb", 6.5, "g/dL")
    assert flag is not None
    assert flag.severity == "critical_low"


def test_inr_critical_high():
    flag = evaluate_critical_value("INR", 6.0, None)
    assert flag is not None
    assert flag.severity == "critical_high"


def test_platelets_panic_low():
    flag = evaluate_critical_value("Platelet Count", 8, "10^3/uL")
    assert flag is not None
    assert flag.severity == "panic_low"


def test_no_certainty_language_in_summary():
    """Rule #4: no imperative/certain clinical language, even in a critical-value summary."""
    flag = evaluate_critical_value("Potassium", 7.0, "mmol/L")
    assert flag is not None
    banned = ("give ", "administer ", "the patient has ", "diagnose with ")
    lowered = flag.summary.lower()
    assert not any(phrase in lowered for phrase in banned)


# --- Unit spellings. Supplying the unit must never be worse than omitting it. ---


@pytest.mark.parametrize(
    "marker,value,unit,expected",
    [
        # mEq/L is mmol/L for a monovalent ion, and is how Indian labs print electrolytes.
        ("Serum Potassium", 7.2, "mEq/L", "panic_high"),
        ("Sodium", 112, "mEq/L", "panic_low"),
        # "gm/dL" and "gm%" are g/dL; "mg%" is mg per 100 mL, i.e. mg/dL by definition.
        ("Hemoglobin", 4.1, "gm/dL", "panic_low"),
        ("Hemoglobin", 4.1, "gm%", "panic_low"),
        ("Random Blood Sugar", 520, "mg%", "panic_high"),
        ("Serum Creatinine", 11.0, "mg%", "panic_high"),
        ("Serum Calcium", 5.2, "mg%", "panic_low"),
        # 1 µL = 1 mm^3 = 1 "cumm": the raw per-cumm count Indian CBC reports print.
        ("Platelet Count", 8000, "/cumm", "panic_low"),
        ("WBC", 800, "cells/cumm", "panic_low"),
        # A real micro sign, which no character class in this module used to survive.
        ("Platelet Count", 8, "10^3/µL", "panic_low"),
        ("Serum Creatinine", 900, "µmol/L", "panic_high"),
    ],
)
def test_a_panic_value_is_caught_however_the_lab_spelled_the_unit(marker, value, unit, expected):
    """The guard used to treat "the same unit written differently" as "a unit I cannot read",
    and skipping is how it handles the latter. So a potassium of 7.2 came back with no flag
    when the report said mEq/L — while the identical value with the unit column left blank was
    caught, because an absent unit is assumed canonical. Supplying the unit disabled the
    can't-miss guard, in the notation Indian labs most commonly use.
    """
    flag = evaluate_critical_value(marker, value, unit)

    assert flag is not None, f"{marker} {value} {unit} produced no flag"
    assert flag.severity == expected


@pytest.mark.parametrize(
    "marker,value,unit",
    [
        # mEq/L is NOT mmol/L for a divalent ion, and calcium's canonical unit is mg/dL anyway.
        # Refusing to guess here is the whole point of scoping synonyms per marker.
        ("Serum Calcium", 5.2, "mEq/L"),
        ("Glucose", 35, "furlongs"),
    ],
)
def test_a_unit_the_guard_cannot_interpret_is_still_skipped_rather_than_guessed(
    marker, value, unit
):
    assert evaluate_critical_value(marker, value, unit) is None


@pytest.mark.parametrize(
    "marker,value,unit",
    [
        ("Potassium", 4.2, "mEq/L"),
        ("Hemoglobin", 13.0, "gm/dL"),
        ("Platelet Count", 250000, "/cumm"),
        ("WBC", 7500, "/cumm"),
    ],
)
def test_a_normal_value_in_those_same_spellings_does_not_flag(marker, value, unit):
    """The other half: the conversions have to land on the right number, not merely produce
    one. A scale error here would flag every normal CBC in the country."""
    assert evaluate_critical_value(marker, value, unit) is None
