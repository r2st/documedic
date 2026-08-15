"""Which analyte a lab row is, read off the name the report actually printed.

``app.core.lab_safety`` holds curated critical-value thresholds, and it can only apply one to a
row it has identified. Identification was an exact lookup against a table of alias spellings,
after normalising the printed name by deleting every character outside ``[a-z0-9+ ]`` — which is
right for the punctuation that is noise and wrong for the punctuation that separates. Indian lab
reports separate constantly, so the names that failed to resolve were ordinary ones:

    "Potassium (K+)"   "ALT/SGPT"   "Bilirubin - Total"   "Platelet Count (PLT)"   "S.Creatinine"

A row that does not resolve is not evaluated, and — because ``unreadable_lab`` only speaks for
markers it could identify — it is not reported as unevaluated either. So a potassium of 7.2
printed as "Potassium (K+)" was dropped silently by the guard whose whole purpose is to catch it,
and the screen rendered it exactly as it renders a normal result.

These tests pin both halves, because widening a name match is precisely how a *different* analyte
comes to be read as this one: the spellings that must resolve, and the ones that must keep not
resolving. The second half matters more. A direct bilirubin read as a total one, or a urine
albumin read as a serum one, is a fabricated number in a patient's chart — the R44 failure ("a
urine creatinine was computed into an eGFR") wearing a different name.
"""

from __future__ import annotations

import pytest

from app.core.lab_safety import (
    _ALIASES,
    _RANGES,
    _normalize_marker,
    canonical_lab_value,
    evaluate_critical_value,
    unreadable_lab,
)

# --- The spellings that must resolve ------------------------------------------------------------


@pytest.mark.parametrize(
    "printed,expected",
    [
        # The four motivating cases: punctuation that separates rather than decorates.
        ("Potassium (K+)", "potassium"),
        ("ALT/SGPT", "alt"),
        ("Bilirubin - Total", "bilirubin"),
        ("Platelet Count (PLT)", "platelets"),
        # A parenthetical is a synonym or a qualifier, and either half may be the known name.
        ("AST (SGOT)", "ast"),
        ("ALT (SGPT)", "alt"),
        ("Sodium (Na+)", "sodium"),
        ("Creatinine (Serum)", "creatinine"),
        ("INR (International Normalized Ratio)", "inr"),
        ("TLC (Total Leukocyte Count)", "wbc"),
        # Punctuation that really is noise still has to be deleted, not separated.
        ("T.Bili", "bilirubin"),
        ("S.Creatinine", "creatinine"),
        ("Hb%", "hemoglobin"),
    ],
)
def test_a_name_the_report_printed_resolves_to_the_analyte_it_names(printed, expected):
    assert _normalize_marker(printed) == expected


@pytest.mark.parametrize(
    "printed,expected",
    [
        # Haemoglobin, both spellings and both abbreviations.
        ("Hgb", "hemoglobin"),
        ("HGB", "hemoglobin"),
        ("Hb", "hemoglobin"),
        ("Haemoglobin", "hemoglobin"),
        # Blood urea nitrogen. See the urea section below for why this is not "urea".
        ("BUN", "bun"),
        ("Blood Urea Nitrogen", "bun"),
        ("Urea Nitrogen", "bun"),
        # Creatinine, including the abbreviations a narrow renal-panel column forces.
        ("Cr", "creatinine"),
        ("Creat", "creatinine"),
        ("S. Cr", "creatinine"),
        ("Serum Creatinine", "creatinine"),
        # The transaminases under both naming conventions, in either order.
        ("AST", "ast"),
        ("SGOT", "ast"),
        ("AST/SGOT", "ast"),
        ("SGOT/AST", "ast"),
        ("Aspartate Aminotransferase", "ast"),
        ("ALT", "alt"),
        ("SGPT", "alt"),
        ("SGPT/ALT", "alt"),
        ("Alanine Aminotransferase", "alt"),
    ],
)
def test_the_common_report_abbreviations_are_recognised(printed, expected):
    """The shortlist a clinician would name if asked how their lab prints these.

    Every one of them is a marker with a curated threshold, so a miss here is a can't-miss guard
    that does not see the row at all.
    """
    assert _normalize_marker(printed) == expected


@pytest.mark.parametrize(
    "printed,expected",
    [
        ("S. Creatinine", "creatinine"),
        ("S.Bilirubin", "bilirubin"),
        ("S. Bilirubin", "bilirubin"),
        ("S.Potassium", "potassium"),
        ("S. Calcium", "calcium"),
        ("S. Albumin", "albumin"),
        ("Serum Urea", "urea"),
        ("P. Glucose", "glucose"),
        ("Plasma Glucose", "glucose"),
        ("B. Urea", "urea"),
        ("Total Calcium", "calcium"),
        ("Total Bilirubin", "bilirubin"),
        # Two prefixes on one line, which is an ordinary liver-panel row.
        ("S. Total Bilirubin", "bilirubin"),
    ],
)
def test_a_leading_specimen_word_does_not_hide_the_analyte(printed, expected):
    """ "S." is how an Indian biochemistry panel labels the specimen, on nearly every line.

    The alias table cannot list the cross-product of every prefix with every marker, and the fact
    that it was carrying exactly one such spelling by hand ("s albumin") is the tell that the
    prefix is a rule rather than a synonym.
    """
    assert _normalize_marker(printed) == expected


def test_a_glucose_is_a_glucose_whenever_it_was_drawn():
    """Fasting, random and post-prandial are the same analyte against the same critical bands.

    The timing word lands on either side of the name depending on the analyser, and a glucose of
    32 is a panic low whichever way round the column header put it.
    """
    for printed in (
        "FBS",
        "RBS",
        "PPBS",
        "Fasting Blood Sugar",
        "Fasting Blood Glucose",
        "Fasting Plasma Glucose",
        "Glucose, Fasting",
        "Glucose (Random)",
        "Post Prandial Blood Sugar",
        "Random Blood Glucose",
        "Blood Sugar",
    ):
        assert _normalize_marker(printed) == "glucose", printed


def test_the_white_count_resolves_under_either_spelling_of_leucocyte():
    """Indian reports print the "-cyte" names with a "c" at least as often as with a "k"."""
    for printed in (
        "WBC",
        "WBC Count",
        "White Cell Count",
        "White Blood Cell Count",
        "Total Leukocyte Count",
        "Total Leucocyte Count",
        "Leucocyte Count",
        "TLC",
    ):
        assert _normalize_marker(printed) == "wbc", printed


# --- The spellings that must keep NOT resolving --------------------------------------------------


@pytest.mark.parametrize(
    "printed",
    [
        "Urine Albumin",
        "Albumin (Urine)",
        "Urinary Creatinine",
        "Microalbumin",
        "Albumin/Creatinine Ratio",
        "Protein/Creatinine Ratio",
        "Creatinine Clearance",
        "CrCl",
        "24h Urine Protein",
        "Spot Urine Protein",
        "CSF Glucose",
        "Ascitic Fluid Albumin",
        "Pleural Fluid Glucose",
        "Stool Occult Blood",
    ],
)
def test_another_specimen_is_never_read_as_the_serum_analyte(printed):
    """A urine albumin in mg/L lands squarely in the g/L band a serum albumin is read in, and a
    urine creatinine fed to CKD-EPI produced a confident eGFR of 0.4. Widening the name match is
    exactly how those come back, so the fence is tested at the same time as the widening."""
    assert _normalize_marker(printed) is None


@pytest.mark.parametrize(
    "printed",
    [
        "Direct Bilirubin",
        "Indirect Bilirubin",
        "Conjugated Bilirubin",
        "Unconjugated Bilirubin",
        "Ionised Calcium",
        "Ionized Calcium",
        "Free T4",
    ],
)
def test_a_qualifier_that_makes_it_a_different_measurement_is_not_stripped(printed):
    """Same serum, different analyte, different scale.

    A direct bilirubin is not the total the 15 mg/dL threshold is written for. An ionised calcium
    runs about 1.2 mmol/L where a total calcium runs about 9.5 mg/dL, so reading one as the other
    is a panic-low flag on a perfectly normal result. These qualifiers must survive the prefix
    rule that correctly turns "Total Calcium" into a calcium.
    """
    assert _normalize_marker(printed) is None


@pytest.mark.parametrize("printed", ["S", "S.", "Serum", "Plasma", "Total", "Blood", "", "   "])
def test_a_bare_qualifier_is_not_a_marker_name(printed):
    """Stripping leading specimen words must never strip the last token: "" would then be looked
    up as a name, and an empty alias key is one typo away from existing."""
    assert _normalize_marker(printed) is None


@pytest.mark.parametrize(
    "printed",
    ["CRP", "ESR", "HbA1c", "Total Protein", "Uric Acid", "TSH", "Vitamin D", "Amylase"],
)
def test_a_marker_this_module_does_not_curate_stays_unresolved(printed):
    """No threshold is curated for these, so resolving them would only produce a KeyError or a
    threshold borrowed from an unrelated analyte."""
    assert _normalize_marker(printed) is None


# --- Urea and BUN are two quantities, not two spellings ------------------------------------------


def test_urea_and_bun_resolve_to_different_markers():
    """BUN is the nitrogen in the urea; blood urea is the whole molecule, 2.14x larger.

    Anglo-American labs print BUN and Indian labs mostly print Blood Urea, and both reach this
    product. Folding them together would mean a threshold wrong by that factor for whichever
    spelling lost.
    """
    assert _normalize_marker("BUN") == "bun"
    assert _normalize_marker("Blood Urea Nitrogen") == "bun"
    assert _normalize_marker("Urea") == "urea"
    assert _normalize_marker("Blood Urea") == "urea"
    assert _normalize_marker("Serum Urea") == "urea"


def test_the_nitrogen_spelling_never_collapses_to_the_whole_molecule():
    """The dangerous direction: "Blood Urea Nitrogen" reduced to "urea" by stripping its leading
    specimen word would be a BUN measured against a threshold 2.14x too high."""
    assert _normalize_marker("Blood Urea Nitrogen") != "urea"
    assert _normalize_marker("Serum Urea Nitrogen") == "bun"


def test_an_ordinary_indian_blood_urea_is_not_a_critical_value():
    """A blood urea of 90 mg/dL is high but unremarkable in CKD — a BUN of 42.

    Read against the BUN threshold it is most of the way to a critical flag, which is the whole
    reason the two are curated apart.
    """
    assert evaluate_critical_value("Blood Urea", 90, "mg/dL") is None
    assert evaluate_critical_value("BUN", 90, "mg/dL") is None


def test_a_critical_value_of_each_is_caught_on_its_own_scale():
    bun = evaluate_critical_value("BUN", 130, "mg/dL")
    assert bun is not None and bun.severity == "critical_high"
    assert bun.canonical_marker == "bun"

    urea = evaluate_critical_value("Blood Urea", 260, "mg/dL")
    assert urea is not None and urea.severity == "critical_high"
    assert urea.canonical_marker == "urea"

    # ...and neither threshold is applied to the other quantity.
    assert evaluate_critical_value("Blood Urea", 130, "mg/dL") is None


def test_a_bun_in_si_units_is_skipped_rather_than_read_as_the_other_quantity():
    """SI reports the urea molecule, not its nitrogen. A row labelled BUN carrying mmol/L is
    either mislabelled or using a convention the document does not pin down, and the two readings
    are 2.14x apart — so it is skipped and said to be skipped."""
    assert evaluate_critical_value("BUN", 40, "mmol/L") is None

    unread = unreadable_lab("BUN", 40, "mmol/L")
    assert unread is not None
    assert unread.reason == "unit_unrecognised"

    # The same number under the urea spelling converts, because that conversion is defined.
    assert canonical_lab_value("Blood Urea", 40, "mmol/L") == pytest.approx(
        ("urea", 240.24), rel=1e-3
    )


# --- Identification feeds the checks, not just the flag ------------------------------------------


def test_a_spelling_that_now_resolves_reaches_the_critical_value_guard():
    """The point of identification is the flag it enables. A potassium of 7.2 is a panic high
    however the analyser labelled the row."""
    for printed in ("Potassium (K+)", "S.Potassium", "K+", "Serum Potassium"):
        flag = evaluate_critical_value(printed, 7.2, "mmol/L")
        assert flag is not None, printed
        assert flag.severity == "panic_high"
        # The clinician-facing text names the row as the report printed it, not as the module
        # filed it: a flag headed "potassium" against a chart that says "S.Potassium" reads as
        # being about a different line.
        assert printed in flag.summary


def test_a_newly_resolved_row_is_now_reportable_as_unreadable_too():
    """Before it resolved, an unreadable "S.Creatinine" was indistinguishable from a normal one:
    ``unreadable_lab`` only speaks for markers it could identify, so an unidentified row was
    silent in both directions."""
    unread = unreadable_lab("S.Creatinine", 88, None)
    assert unread is not None
    assert unread.canonical_marker == "creatinine"
    assert unread.reason == "unit_missing"


def test_the_hepatic_thresholds_can_tell_an_alt_from_a_bilirubin_under_either_convention():
    """``canonical_lab_value`` is what the hepatic dose-adjustment thresholds in
    ``app.core.safety`` use to know which liver-panel row they are looking at."""
    assert canonical_lab_value("SGPT", 240, "U/L") == ("alt", 240)
    assert canonical_lab_value("ALT/SGPT", 240, "IU/L") == ("alt", 240)
    assert canonical_lab_value("S. Total Bilirubin", 4.2, "mg/dL") == ("bilirubin", 4.2)


# --- Table invariants ----------------------------------------------------------------------------


def test_every_alias_points_at_a_marker_with_a_curated_range():
    """``_RANGES[canonical]`` is indexed unguarded on every path that resolves a name, so an
    alias pointing at a marker with no range entry is a KeyError on a real lab row."""
    dangling = {
        alias: canonical for alias, canonical in _ALIASES.items() if canonical not in _RANGES
    }
    assert dangling == {}


def test_every_curated_marker_is_reachable_by_at_least_one_name():
    """A range no name resolves to is a threshold that can never fire."""
    reachable = set(_ALIASES.values())
    assert set(_RANGES) - reachable == set()


def test_every_alias_is_already_in_the_normalised_form_it_is_looked_up_by():
    """The table is only ever consulted with lowercase, punctuation-folded, single-spaced keys.
    An alias carrying a capital, a slash or a double space is dead weight that reads as coverage.
    """
    assert all(alias == " ".join(alias.lower().split()) for alias in _ALIASES)
    assert not [alias for alias in _ALIASES if any(c in alias for c in "/().-,%")]


# --- The INR, which a coagulation panel abbreviates ---------------------------------------------
#
# ``prothrombin time inr`` was in the alias table and ``pt inr`` was not, which is the wrong way
# round: the long form is what a textbook writes and the abbreviation is what a report prints.
# The failure was silent and one-sided. An unresolved row is skipped, so no INR reached
# ``HepaticPanel`` — and the INR is an input to *both* Child-Pugh and MELD, so a chart printing
# "PT INR 2.8" lost the entire hepatic severity picture and ``assess_hepatic_severity`` named the
# INR as an input the chart did not carry, on a chart that carried it.


@pytest.mark.parametrize(
    "printed",
    [
        "PT INR",
        "PT/INR",
        "PT-INR",
        "PT_INR",
        "INR (PT)",
        "INR/PT",
        "Prothrombin Time INR",
        "Prothrombin Time International Normalized Ratio",
        "PT INR Ratio",
        "INR",
        "International Normalised Ratio",
    ],
)
def test_the_coagulation_panels_spellings_of_the_inr_all_resolve(printed: str) -> None:
    assert _normalize_marker(printed) == "inr"


@pytest.mark.parametrize(
    "printed",
    [
        # Prothrombin time itself: a different quantity in different units (seconds, ~11-14).
        # Read as an INR it is a coagulopathy that is not there, scoring 3 Child-Pugh points on a
        # normal clotting screen — the R44 failure shape, in the marker that feeds two scores.
        "PT",
        "Prothrombin Time",
        "PT (Prothrombin Time)",
        "Prothrombin Time (seconds)",
        # The other clotting times, which are not the INR either.
        "APTT",
        "aPTT",
        "Activated Partial Thromboplastin Time",
        "Thrombin Time",
        "Bleeding Time",
        "Clotting Time",
    ],
)
def test_the_other_clotting_times_are_not_read_as_an_inr(printed: str) -> None:
    assert _normalize_marker(printed) != "inr"


def test_an_inr_printed_as_pt_inr_carries_a_value_through_to_the_canonical_form() -> None:
    """The whole point of resolving it: the number has to arrive somewhere usable.

    The INR is dimensionless, so there is no conversion to get wrong — which is exactly why the
    row being dropped was invisible. Nothing failed; a score simply stopped being computable.
    """
    assert canonical_lab_value("PT INR", 2.8, None) == ("inr", 2.8)
    assert canonical_lab_value("PT/INR", 2.8, "ratio") == ("inr", 2.8)
