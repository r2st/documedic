"""The dose parser, exercised on the prose it actually has to read.

``app.core.dose_text`` decides two things about a sentence a language model wrote: which of its
numbers are doses, and which word each dose belongs to. Everything downstream — whether a
management option is escalated to flag-for-review, and whether a clinician is told the drug in
front of them does not exist — rests on those two judgements, so they are pinned here on the
shapes that occur rather than on invented ones.

The bias of the whole module is to under-report, and most of this file is about that: a
laboratory value is not a dose, a dose with no name in front of it raises nothing, and a
denominator the module cannot read makes the number meaningless rather than suspicious. A false
positive here escalates a correct guideline recommendation, and a tier that fills up with
phrasing artefacts is a tier clinicians stop reading.
"""

from __future__ import annotations

import pytest

from app.core.dose_text import (
    ABSOLUTE_MAX_SINGLE_DOSE_MG,
    IMPLAUSIBLE_STRENGTH_MULTIPLE,
    DoseMention,
    find_dose_mentions,
    implausible_dose_reason,
    max_milligrams_in_strength,
)


def _only(text: str) -> DoseMention:
    mentions = find_dose_mentions(text)
    assert len(mentions) == 1, f"expected exactly one dose in {text!r}, got {mentions}"
    return mentions[0]


# --------------------------------------------------------------------------- what is a dose


@pytest.mark.parametrize(
    ("text", "milligrams"),
    [
        ("Paracetamol 500 mg orally every 6 hours.", 500.0),
        ("Paracetamol 500mg orally.", 500.0),
        ("Ceftriaxone 1 g IV once daily.", 1000.0),
        ("Salbutamol 100 mcg inhaled.", 0.1),
        ("Levothyroxine 50 µg once daily.", 0.05),
        ("Digoxin 0.25 mg daily.", 0.25),
        ("Metformin 1,000 mg twice daily.", 1000.0),
        ("Prednisolone 10 milligrams daily.", 10.0),
    ],
)
def test_a_dose_is_read_in_the_unit_it_was_written_in(text: str, milligrams: float) -> None:
    assert _only(text).milligrams == pytest.approx(milligrams)


def test_a_range_is_judged_at_its_upper_end():
    """ "500-1000 mg" is one dose expression, and the number that could be wrong is the larger."""
    assert _only("Amoxicillin 500-1000 mg three times daily.").amount == 1000.0
    assert _only("Amoxicillin 1 to 2 g three times daily.").milligrams == 2000.0


def test_a_laboratory_value_is_not_a_dose():
    """The case that decides whether this module can be run over clinical prose at all.

    A creatinine in mg/dL is a dose expression by shape and a laboratory result by meaning, and
    guideline text reports one in the same paragraph that recommends the other.
    """
    assert find_dose_mentions("Serum creatinine 1.5 mg/dL was recorded.") == []
    assert find_dose_mentions("eGFR 22 mL/min/1.73m2, potassium 5.1 mmol/L.") == []
    assert find_dose_mentions("Haemoglobin 9.4 g/dL on admission.") == []


def test_a_denominator_the_module_cannot_read_is_not_a_dose():
    """Unrecognised means "meaning unknown", not "assume the worst".

    Excluded by falling through rather than by being listed, so a unit nobody anticipated does
    not arrive as a dose and produce a flag about a number this module never understood.
    """
    assert find_dose_mentions("Infusion at 5 mg/furlong.") == []


def test_a_weight_based_dose_is_a_dose_but_not_an_amount():
    mention = _only("Paediatric dosing is 15 mg/kg per dose.")
    assert mention.per_body_weight is True
    # No ceiling can apply: a mg/kg figure and a tablet's strength are not the same kind of
    # number, and comparing them would flag every correct paediatric dose in the corpus.
    assert implausible_dose_reason(mention, 500.0) is None


def test_a_daily_total_is_still_a_dose():
    assert _only("Paracetamol 4 g/day maximum.").milligrams == 4000.0


def test_a_number_that_is_not_followed_by_a_unit_is_not_a_dose():
    assert find_dose_mentions("Review the patient in 3 days.") == []
    assert find_dose_mentions("A 54-year-old with 2 hours of chest pain.") == []


def test_a_unit_spelling_that_is_only_the_start_of_a_word_is_not_a_unit():
    """ "5 gastric" is not five grams."""
    assert find_dose_mentions("There were 5 gastric erosions.") == []


# --------------------------------------------------------------------------- whose dose is it


def test_the_word_immediately_before_the_amount_is_the_candidate():
    assert _only("Consider Cardizemol 40 mg twice daily.").name_candidate == "Cardizemol"


def test_each_dose_gets_its_own_clause_not_the_whole_sentence():
    """The case that makes a second, invented drug detectable beside a real first one.

    Bounding each dose's window at the previous dose is what stops "Paracetamol" vouching for
    "Cardizemol" three words later.
    """
    first, second = find_dose_mentions("Paracetamol 500 mg and Cardizemol 40 mg twice daily.")
    assert first.name_candidate == "Paracetamol"
    assert second.name_candidate == "Cardizemol"
    assert "Paracetamol" not in second.context


def test_a_drug_named_in_the_previous_sentence_does_not_reach_this_one():
    _first, second = find_dose_mentions("Paracetamol 500 mg is preferred. Give Zaltrexil 20 mg.")
    assert "Paracetamol" not in second.context


def test_a_dose_the_sentence_does_not_put_a_name_in_front_of_raises_no_candidate():
    """The precision guard. Only the canonical prescription shape is judged.

    Every other arrangement leaves the candidate empty, and an empty candidate can never produce
    an unverified-drug-name flag — a sentence not in prescription form is not evidence that a
    drug name was invented.
    """
    assert _only("Titrate the dose to 500 mg as tolerated.").name_candidate == ""
    assert _only("Paracetamol at a dose of 500 mg may be considered.").name_candidate == ""
    assert _only("Reduce salt intake to less than 5 g per day.").name_candidate == ""
    assert _only("The maximum is 4 g daily.").name_candidate == ""


def test_a_word_too_short_to_be_a_drug_name_is_not_one():
    """ "Vitamin D 60,000 IU" — the letter is not the candidate, and IU has no mass."""
    mention = _only("Vitamin D 60,000 IU weekly.")
    assert mention.name_candidate == ""
    assert mention.milligrams is None


def test_a_trailing_name_is_kept_in_the_context_so_the_vocabulary_can_see_it():
    """ "500 mg of paracetamol" names its drug after the dose, and still names it."""
    assert "paracetamol" in _only("Give 500 mg of paracetamol orally.").context


# --------------------------------------------------------------------------- product strengths


@pytest.mark.parametrize(
    ("strength", "milligrams"),
    [
        ("500mg", 500.0),
        ("500mg+1mg", 500.0),  # the heaviest ingredient of a combination
        ("160mg+800mg", 800.0),
        ("6/200mcg", 0.2),
        ("0.25mg", 0.25),
        ("1g", 1000.0),
        ("100mcg", 0.1),
    ],
)
def test_the_largest_mass_in_a_product_label_is_what_a_dose_is_measured_against(
    strength: str, milligrams: float
) -> None:
    assert max_milligrams_in_strength(strength) == pytest.approx(milligrams)


def test_a_product_with_no_mass_strength_offers_no_ceiling():
    """An insulin in U/mL, a contrast medium with nothing recorded. None, not zero.

    Zero would make every dose of that drug a hundred times its strength.
    """
    assert max_milligrams_in_strength("100U/mL") is None
    assert max_milligrams_in_strength(None) is None
    assert max_milligrams_in_strength("") is None


# --------------------------------------------------------------------------- what is impossible


def test_the_microgram_to_milligram_slip_is_caught():
    """Levothyroxine is dispensed and dosed in micrograms; "50 mg" is 500x its largest strength.

    The classic model error, and one of the few where the fluent sentence and the lethal one are
    the same sentence.
    """
    mention = _only("Levothyroxine 50 mg once daily before breakfast.")
    reason = implausible_dose_reason(mention, max_milligrams_in_strength("100mcg"))
    assert reason is not None
    assert "100 times" in reason


def test_a_therapeutic_dose_well_above_one_tablet_is_left_alone():
    """4 g of paracetamol is a day's maximum, not a typing error. 500 mg tablets, 8x, allowed."""
    mention = _only("Paracetamol 4 g daily maximum.")
    assert implausible_dose_reason(mention, max_milligrams_in_strength("650mg")) is None


def test_the_ceiling_is_exactly_the_documented_multiple():
    """Pinned on both sides so the constant cannot drift without a test saying so."""
    at_ceiling = _only(f"Paracetamol {500 * IMPLAUSIBLE_STRENGTH_MULTIPLE:.0f} mg.")
    assert implausible_dose_reason(at_ceiling, 500.0) is None
    over = _only(f"Paracetamol {500 * IMPLAUSIBLE_STRENGTH_MULTIPLE + 1:.0f} mg.")
    assert implausible_dose_reason(over, 500.0) is not None


def test_an_absolute_ceiling_applies_when_there_is_no_product_to_measure_against():
    mention = _only("Zaltrexil 500 g daily.")
    reason = implausible_dose_reason(mention, None)
    assert reason is not None
    assert "no drug is administered in" in reason
    assert implausible_dose_reason(_only("Zaltrexil 40 g daily."), None) is None
    assert ABSOLUTE_MAX_SINGLE_DOSE_MG == 100_000.0


def test_a_dose_in_kilograms_is_not_a_dose_of_anything():
    assert implausible_dose_reason(_only("Paracetamol 2 kg daily."), 500.0) is not None


def test_a_dose_of_zero_is_not_a_dose():
    assert implausible_dose_reason(_only("Paracetamol 0 mg daily."), 500.0) == (
        "the amount is zero"
    )


def test_international_units_are_not_judged_against_any_ceiling():
    """100 IU of insulin and 100 IU of vitamin D are not comparable quantities.

    There is no unit-independent ceiling to apply, so the module reports nothing rather than
    inventing one.
    """
    mention = _only("Insulin 300 units daily.")
    assert mention.milligrams is None
    assert implausible_dose_reason(mention, None) is None


def test_the_reason_is_written_in_the_unit_a_clinician_reads():
    """The number in the flag has to be checkable against the number on the screen."""
    reason = implausible_dose_reason(_only("Levothyroxine 50 mg daily."), 0.1)
    assert reason is not None and "50 mg" in reason and "100 mcg" in reason


def test_empty_and_whitespace_text_find_nothing():
    assert find_dose_mentions("") == []
    assert find_dose_mentions("   \n ") == []
