"""Unit tests for deterministic clinical computations (eGFR, age)."""

from __future__ import annotations

from datetime import date

import pytest

from app.core.clinical import (
    age_from_dob,
    ckd_epi_2021_egfr,
    egfr_reference_abnormal,
    is_serum_creatinine_marker,
    serum_creatinine_mg_dl,
)


def test_age_from_dob():
    assert age_from_dob(date(1980, 6, 15), date(2026, 6, 14)) == 45
    assert age_from_dob(date(1980, 6, 15), date(2026, 6, 15)) == 46


def test_ckd_epi_normal_male():
    # Healthy 45y male, creatinine 0.9 -> eGFR comfortably > 90.
    result = ckd_epi_2021_egfr(creatinine_mg_dl=0.9, age_years=45, sex="male")
    assert result.formula_name == "CKD-EPI_2021"
    assert result.value > 90
    assert result.inputs["sex"] == "male"


def test_ckd_epi_impaired():
    # Elevated creatinine 2.5 in a 70y male -> markedly reduced eGFR.
    result = ckd_epi_2021_egfr(creatinine_mg_dl=2.5, age_years=70, sex="male")
    assert result.value < 30
    assert egfr_reference_abnormal(result.value)


def test_ckd_epi_female_adjustment():
    male = ckd_epi_2021_egfr(creatinine_mg_dl=1.0, age_years=50, sex="male")
    female = ckd_epi_2021_egfr(creatinine_mg_dl=1.0, age_years=50, sex="female")
    # Same creatinine: the female equation yields a lower eGFR (lower k, factor).
    assert female.value < male.value


def test_ckd_epi_rejects_bad_input():
    with pytest.raises(ValueError):
        ckd_epi_2021_egfr(creatinine_mg_dl=0, age_years=40, sex="male")
    with pytest.raises(ValueError):
        ckd_epi_2021_egfr(creatinine_mg_dl=1.0, age_years=0, sex="male")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Creatinine", True),
        ("Serum Creatinine", True),
        ("S. Creatinine", True),
        ("Creat", True),
        ("SCr", True),
        ("HbA1c", False),
        ("Hemoglobin", False),
        ("", False),
        # Different analytes on different scales, every one of which used to match on the
        # substring "creatinine" and be handed to CKD-EPI as if it were a serum creatinine.
        ("Creatinine Clearance", False),
        ("CrCl", False),
        ("Urine Creatinine", False),
        ("Creatinine, Urine", False),
        ("24h Urine Creatinine", False),
        ("Urinary Creatinine Excretion", False),
        ("Spot Urine Creatinine", False),
        ("Albumin/Creatinine Ratio", False),
        ("Creatine Kinase", False),
        ("Creatinine Kinase", False),  # the common misspelling of creatine kinase
    ],
)
def test_only_a_serum_creatinine_is_accepted_for_the_egfr_equation(name, expected):
    """CKD-EPI takes a serum creatinine in mg/dL and nothing else.

    A urine creatinine is two orders of magnitude larger and a creatinine clearance is already
    a clearance, so either one produces a confident eGFR near zero — a fabricated number in the
    chart and an unclearable renal alert on a patient whose kidneys were never measured.
    """
    assert is_serum_creatinine_marker(name) is expected


@pytest.mark.parametrize(
    "unit,expected",
    [
        ("mg/dL", 1.1),
        ("mg/dl", 1.1),
        ("mg%", 1.1),  # mg per 100 mL is mg/dL by definition
        (None, 1.1),  # Indian reports routinely omit it; mg/dL is the near-universal default
        ("", 1.1),
    ],
)
def test_a_creatinine_already_in_mg_per_dl_is_passed_through(unit, expected):
    assert serum_creatinine_mg_dl("Serum Creatinine", 1.1, unit) == pytest.approx(expected)


@pytest.mark.parametrize("micro", ["umol/L", "µmol/L", "μmol/L"])
def test_a_creatinine_in_micromoles_is_converted_not_taken_at_face_value(micro: str) -> None:
    """88 µmol/L is 1.0 mg/dL. Read as 88 mg/dL it yields an eGFR of 0.4.

    The critical-value guard has always converted this; the eGFR derivation read the same row
    at face value, so one half of the safety engine understood the units and the other did not.
    Both Unicode micro signs come off lab reports and both have to fold to the same thing.
    """
    converted = serum_creatinine_mg_dl("Serum Creatinine", 88.0, micro)

    assert converted is not None
    assert converted == pytest.approx(1.0, abs=0.01)


@pytest.mark.parametrize("unit", ["mmol/L", "mL/min", "g/dL", "nonsense"])
def test_a_creatinine_in_units_that_cannot_be_read_yields_nothing(unit: str) -> None:
    """Skipped, never guessed — the renal check then reports that it had no eGFR to apply."""
    assert serum_creatinine_mg_dl("Serum Creatinine", 88.0, unit) is None


def test_a_row_with_no_value_or_the_wrong_analyte_yields_nothing() -> None:
    assert serum_creatinine_mg_dl("Serum Creatinine", None, "mg/dL") is None
    assert serum_creatinine_mg_dl("Urine Creatinine", 120.0, "mg/dL") is None
