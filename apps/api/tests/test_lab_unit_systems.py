"""Every curated marker, read correctly in both unit systems it is reported in.

``app.core.lab_safety`` skips a row whose unit it cannot place, which is the right answer for a
unit it genuinely cannot interpret and the wrong one for a unit it simply had not been told
about. The difference is invisible from outside: both come back as "no flag", the same answer a
normal result gives. So a haemoglobin of 55 g/L — a panic-low value, in the SI spelling a
machine-generated CBC prints — was indistinguishable from a normal haemoglobin, and so were a
calcium of 1.6 mmol/L and a platelet count of 8 x10^9/L.

The module's own rule is that supplying the unit must never be worse than omitting it. These
tests hold it to that across the whole table rather than at the handful of spellings someone
happened to write a case for:

  * every conversion factor lands on the right number, checked against arithmetic done here
    rather than against the table's own constant;
  * every synonym carries no arithmetic at all;
  * the same physiological value produces the same verdict in either system;
  * a unit that really is uninterpretable is still skipped, and still says so.

The last one is not a formality. Widening unit handling is how a scale error gets in, and a
scale error here flags every normal result in the country.
"""

from __future__ import annotations

import pytest

from app.core.lab_safety import (
    _RANGES,
    _UNIT_CONVERSIONS,
    _UNIT_SYNONYMS,
    _normalize_marker,
    canonical_lab_value,
    evaluate_critical_value,
    unreadable_lab,
)

# (marker, SI value, SI unit, conventional value, conventional unit, shared verdict).
#
# The two values are the same patient measured twice, converted by hand. A marker appears once
# per clinically distinct verdict so that both a firing and a non-firing case are covered: a
# conversion that is off by a factor tends to break exactly one of the two.
_BOTH_SYSTEMS: tuple[tuple[str, float, str, float, str, str | None], ...] = (
    # Glucose: mmol/L vs mg/dL, x18.0182.
    ("Glucose", 2.0, "mmol/L", 36.0, "mg/dL", "panic_low"),
    ("Fasting Blood Sugar", 5.5, "mmol/L", 99.1, "mg/dL", None),
    ("Glucose", 28.0, "mmol/L", 504.5, "mg/dL", "panic_high"),
    # Creatinine: µmol/L vs mg/dL, x88.42.
    ("Serum Creatinine", 900.0, "umol/L", 10.18, "mg/dL", "panic_high"),
    ("S. Creatinine", 88.4, "umol/L", 1.0, "mg/dL", None),
    ("Cr", 400.0, "umol/L", 4.52, "mg/dL", "critical_high"),
    # Bilirubin: µmol/L vs mg/dL, x17.104.
    ("Total Bilirubin", 290.0, "umol/L", 16.96, "mg/dL", "critical_high"),
    ("S. Bilirubin", 17.0, "umol/L", 0.99, "mg/dL", None),
    # Haemoglobin: g/L vs g/dL, x10.
    ("Hemoglobin", 48.0, "g/L", 4.8, "g/dL", "panic_low"),
    ("Hgb", 120.0, "g/L", 12.0, "g/dL", None),
    ("Hb", 65.0, "g/L", 6.5, "g/dL", "critical_low"),
    # Albumin: g/L vs g/dL, x10. Registered for its unit, not for a threshold — the Child-Pugh
    # score needs the value, and no verdict is expected either way.
    ("S. Albumin", 42.0, "g/L", 4.2, "g/dL", None),
    # Calcium: mmol/L vs mg/dL, x4.008.
    ("Calcium", 1.45, "mmol/L", 5.81, "mg/dL", "panic_low"),
    ("Total Calcium", 2.35, "mmol/L", 9.42, "mg/dL", None),
    ("Serum Calcium", 3.4, "mmol/L", 13.63, "mg/dL", "critical_high"),
    # Cell counts: 10^9/L vs 10^3/µL — the same number, not a conversion.
    ("Platelet Count", 8.0, "10^9/L", 8.0, "10^3/uL", "panic_low"),
    ("Platelet Count", 250.0, "10^9/L", 250.0, "10^3/uL", None),
    ("WBC", 0.8, "10^9/L", 0.8, "10^3/uL", "panic_low"),
    ("Total Leucocyte Count", 7.5, "10^9/L", 7.5, "10^3/uL", None),
    # Electrolytes: mmol/L and mEq/L coincide for a monovalent ion.
    ("Potassium", 7.2, "mmol/L", 7.2, "mEq/L", "panic_high"),
    ("S.Potassium", 4.1, "mmol/L", 4.1, "mEq/L", None),
    ("Sodium", 112.0, "mmol/L", 112.0, "mEq/L", "panic_low"),
    # Transaminases: U/L and IU/L are the same unit of enzyme activity. No threshold curated.
    ("ALT/SGPT", 240.0, "IU/L", 240.0, "U/L", None),
    ("AST (SGOT)", 180.0, "units/L", 180.0, "U/L", None),
    # Urea, on its own scale: mmol/L vs mg/dL, x6.006.
    ("Blood Urea", 40.0, "mmol/L", 240.2, "mg/dL", "critical_high"),
    ("Serum Urea", 5.0, "mmol/L", 30.0, "mg/dL", None),
    # BUN has no SI form here on purpose — see the module, which refuses mmol/L under a BUN
    # label because SI reports the urea molecule and the two readings are 2.14x apart. Its two
    # spellings are both mg/dL, so this row is what "both systems" amounts to for it.
    ("BUN", 130.0, "mg%", 130.0, "mg/dL", "critical_high"),
    ("BUN", 15.0, "mg%", 15.0, "mg/dL", None),
)


@pytest.mark.parametrize(
    "marker,si_value,si_unit,conv_value,conv_unit,expected",
    _BOTH_SYSTEMS,
    ids=[f"{row[0]}-{row[2]}-vs-{row[4]}" for row in _BOTH_SYSTEMS],
)
def test_the_same_result_reads_the_same_in_either_unit_system(
    marker, si_value, si_unit, conv_value, conv_unit, expected
):
    """One patient, one measurement, two ways of printing it, one verdict.

    This is the property that matters clinically: which notation the analyser used must not
    decide whether a can't-miss guard fires.
    """
    si_flag = evaluate_critical_value(marker, si_value, si_unit)
    conv_flag = evaluate_critical_value(marker, conv_value, conv_unit)

    assert (si_flag.severity if si_flag else None) == expected, f"{marker} in {si_unit}"
    assert (conv_flag.severity if conv_flag else None) == expected, f"{marker} in {conv_unit}"


@pytest.mark.parametrize(
    "marker,si_value,si_unit,conv_value,conv_unit",
    [row[:5] for row in _BOTH_SYSTEMS],
    ids=[f"{row[0]}-{row[2]}-vs-{row[4]}" for row in _BOTH_SYSTEMS],
)
def test_both_spellings_land_on_the_same_canonical_number(
    marker, si_value, si_unit, conv_value, conv_unit
):
    """The verdict agreeing is necessary but not sufficient: two values on opposite sides of a
    threshold agree about the verdict while disagreeing about the patient. The converted numbers
    themselves have to coincide, which is what the derived markers downstream consume —
    ``creatinine_to_mg_dl`` feeds the CKD-EPI eGFR that the metformin hard block hangs off.
    """
    si = canonical_lab_value(marker, si_value, si_unit)
    conv = canonical_lab_value(marker, conv_value, conv_unit)

    assert si is not None, f"{marker} {si_value} {si_unit} was not readable"
    assert conv is not None, f"{marker} {conv_value} {conv_unit} was not readable"
    assert si[0] == conv[0]
    assert si[1] == pytest.approx(conv[1], rel=0.005)


def test_every_marker_with_a_second_unit_system_is_exercised_above():
    """A conversion or synonym added to the module without a case here would be untested, and
    the failure mode of an untested conversion is a scale error that reads as a normal result.
    """
    covered = {_normalize_marker(marker) for marker, *_ in _BOTH_SYSTEMS}
    has_second_system = set(_UNIT_CONVERSIONS) | set(_UNIT_SYNONYMS)

    assert has_second_system - covered == set()


# --- The conversions themselves ------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker,value,unit,expected_canonical",
    [
        # Each expected number is arithmetic done here, not the module's constant read back.
        ("Glucose", 10.0, "mmol/L", 10.0 * 18.0182),
        ("Serum Creatinine", 100.0, "umol/L", 100.0 / 88.42),
        ("Total Bilirubin", 100.0, "umol/L", 100.0 / 17.104),
        ("S. Albumin", 35.0, "g/L", 3.5),
        ("Hemoglobin", 130.0, "g/L", 13.0),
        ("Calcium", 2.5, "mmol/L", 2.5 * 4.008),
        ("Blood Urea", 10.0, "mmol/L", 60.06),
        ("Platelet Count", 200_000.0, "/cumm", 200.0),
        ("WBC", 7_500.0, "cells/cumm", 7.5),
    ],
)
def test_a_registered_conversion_lands_on_the_right_number(marker, value, unit, expected_canonical):
    result = canonical_lab_value(marker, value, unit)
    assert result is not None
    assert result[1] == pytest.approx(expected_canonical, rel=1e-4)


@pytest.mark.parametrize(
    "marker,value,unit",
    [
        ("Potassium", 4.2, "mEq/L"),
        ("Sodium", 138.0, "mEq/L"),
        ("Glucose", 95.0, "mg%"),
        ("Serum Creatinine", 1.1, "mg%"),
        ("Serum Calcium", 9.4, "mg%"),
        ("Blood Urea", 32.0, "mg%"),
        ("BUN", 15.0, "mg%"),
        ("Hemoglobin", 13.2, "gm/dL"),
        ("Hemoglobin", 13.2, "gm%"),
        ("S. Albumin", 4.1, "gm/dL"),
        ("ALT", 30.0, "IU/L"),
        ("AST", 28.0, "units/L"),
        ("Platelet Count", 240.0, "10^9/L"),
        ("WBC", 6.8, "x10^9/L"),
    ],
)
def test_a_synonym_is_the_same_unit_and_carries_no_arithmetic(marker, value, unit):
    """A synonym is the canonical unit written the way a report writes it. If any of these
    started scaling the value, a normal result would become a flag."""
    result = canonical_lab_value(marker, value, unit)
    assert result is not None, f"{marker} in {unit} was not readable"
    assert result[1] == value


# --- What must still be refused ------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker,value,unit",
    [
        # mEq/L depends on valence and is wrong by two for calcium — which is exactly why the
        # mmol/L conversion added for calcium is registered separately from this.
        ("Serum Calcium", 5.2, "mEq/L"),
        # U/mL is a thousandfold different from U/L, not a spelling of it.
        ("ALT", 240.0, "U/mL"),
        # SI reports the urea molecule, so mmol/L under a BUN label is 2.14x ambiguous.
        ("BUN", 40.0, "mmol/L"),
        ("Glucose", 35.0, "furlongs"),
        ("Hemoglobin", 8.0, "mmol/L"),
    ],
)
def test_a_unit_the_module_cannot_place_is_skipped_and_said_to_be_skipped(marker, value, unit):
    """Skipping quietly is the failure this module exists to avoid: it renders identically to a
    normal result. Every skip has to be reportable."""
    assert evaluate_critical_value(marker, value, unit) is None
    assert canonical_lab_value(marker, value, unit) is None

    unread = unreadable_lab(marker, value, unit)
    assert unread is not None
    assert unread.reason == "unit_unrecognised"
    assert unit in unread.summary


def test_adding_the_calcium_conversion_did_not_admit_the_valence_dependent_one():
    """The pair that has to stay apart. Both are "a molar-ish unit on a calcium row", and only
    one of them converts: 2.5 mmol/L is 10.0 mg/dL, while 2.5 mEq/L is 5.0 mg/dL, and guessing
    between them is a panic-low flag on a normal calcium."""
    assert canonical_lab_value("Calcium", 2.5, "mmol/L") == pytest.approx(
        ("calcium", 10.02), rel=1e-3
    )
    assert canonical_lab_value("Calcium", 2.5, "mEq/L") is None


# --- Table invariants ----------------------------------------------------------------------------


def test_no_conversion_is_registered_for_a_marker_with_no_curated_range():
    """``_RANGES[canonical]`` is what supplies the expected unit, and is indexed unguarded."""
    assert set(_UNIT_CONVERSIONS) - set(_RANGES) == set()
    assert set(_UNIT_SYNONYMS) - set(_RANGES) == set()


def test_no_synonym_is_also_a_conversion_for_the_same_marker():
    """A unit that is both would take whichever path is checked first, which is a silent
    disagreement about whether it carries arithmetic."""
    for marker, synonyms in _UNIT_SYNONYMS.items():
        conversions = set(_UNIT_CONVERSIONS.get(marker, {}))
        assert synonyms & conversions == set(), marker


def test_no_synonym_restates_the_canonical_unit():
    """The canonical unit is matched before the synonym table is consulted, so an entry naming
    it is dead weight that reads as coverage."""
    for marker, synonyms in _UNIT_SYNONYMS.items():
        assert _RANGES[marker].unit.lower() not in synonyms, marker


def test_no_conversion_factor_is_one():
    """A factor of 1 is a synonym wearing a conversion's clothes, and hides that the two units
    are the same thing — which is the fact a reader needs to check it."""
    for marker, factors in _UNIT_CONVERSIONS.items():
        for unit, factor in factors.items():
            assert factor != 1.0, f"{marker}/{unit}"
