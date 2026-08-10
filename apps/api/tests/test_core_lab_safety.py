"""Unit tests for the deterministic critical/panic lab-value guard (no DB, no LLM)."""

from __future__ import annotations

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
