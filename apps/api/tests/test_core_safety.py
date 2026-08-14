"""Unit tests for the deterministic drug-safety engine (no DB, no LLM)."""

from __future__ import annotations

import pytest

from app.core.safety import (
    ContraindicationRule,
    DrugRef,
    InteractionRule,
    PatientAllergy,
    PatientCondition,
    SafetyContext,
    check_duplicate_therapy,
    check_guideline_adherence,
    evaluate_drug_safety,
    has_hard_block,
)

ASPIRIN = DrugRef(reference_id="ASP-75", generic_name="Aspirin", drug_class="Antiplatelet")
DICLOFENAC = DrugRef(reference_id="DIC-50", generic_name="Diclofenac", drug_class="NSAID")
IBUPROFEN = DrugRef(reference_id="IBU-400", generic_name="Ibuprofen", drug_class="NSAID")
METFORMIN = DrugRef(reference_id="MET-500", generic_name="Metformin", drug_class="Biguanide")
ENALAPRIL = DrugRef(reference_id="ENA-5", generic_name="Enalapril", drug_class="ACE Inhibitor")
RAMIPRIL = DrugRef(reference_id="RAM-5", generic_name="Ramipril", drug_class="ACE Inhibitor")
PARACETAMOL_500 = DrugRef(
    reference_id="PCM-500", generic_name="Paracetamol", drug_class="Analgesic"
)
PARACETAMOL_650 = DrugRef(
    reference_id="PCM-650", generic_name="Paracetamol", drug_class="Analgesic"
)


def test_direct_allergy_is_hard_block():
    ctx = SafetyContext(
        allergies=[PatientAllergy(allergen_name="Aspirin", drug_reference_id="ASP-75")]
    )
    flags = evaluate_drug_safety(ASPIRIN, ctx)
    assert has_hard_block(flags)
    assert flags[0].check_type == "allergy_conflict"
    assert flags[0].severity == "hard_block"


def test_cross_class_allergy_is_hard_block():
    """Allergy to one NSAID hard-blocks another NSAID via drug-class cross-match."""
    ctx = SafetyContext(
        allergies=[
            PatientAllergy(
                allergen_name="Diclofenac", drug_reference_id="DIC-50", drug_class="NSAID"
            )
        ]
    )
    flags = evaluate_drug_safety(IBUPROFEN, ctx)
    assert has_hard_block(flags)
    assert flags[0].details["match_type"] == "cross_class"


def test_contraindicated_interaction_is_hard_block():
    ctx = SafetyContext(
        current_meds=[ENALAPRIL],
        interaction_rules=[InteractionRule("ENA-5", "SPIRO-25", "contraindicated", "x")],
    )
    spiro = DrugRef(reference_id="SPIRO-25", generic_name="Spironolactone")
    flags = evaluate_drug_safety(spiro, ctx)
    assert has_hard_block(flags)


def test_major_interaction_is_critical_not_hard_block():
    ctx = SafetyContext(
        current_meds=[ASPIRIN],
        interaction_rules=[InteractionRule("ASP-75", "DIC-50", "major", "GI bleeding risk")],
    )
    flags = evaluate_drug_safety(DICLOFENAC, ctx)
    assert len(flags) == 1
    assert flags[0].severity == "critical"
    assert not flags[0].is_hard_block
    assert not has_hard_block(flags)


def test_absolute_contraindication_is_hard_block():
    ctx = SafetyContext(
        conditions=[PatientCondition(condition_name="Peptic Ulcer Disease")],
        contraindication_rules=[
            ContraindicationRule(
                "DIC-50", "Peptic Ulcer Disease", "absolute", "bleeding risk", True
            )
        ],
    )
    flags = evaluate_drug_safety(DICLOFENAC, ctx)
    assert has_hard_block(flags)
    assert flags[0].check_type == "contraindication"


def test_relative_contraindication_is_warning():
    ctx = SafetyContext(
        conditions=[PatientCondition(condition_name="G6PD Deficiency")],
        contraindication_rules=[
            ContraindicationRule("CIP-500", "G6PD Deficiency", "relative", "hemolysis", False)
        ],
    )
    cipro = DrugRef(reference_id="CIP-500", generic_name="Ciprofloxacin")
    flags = evaluate_drug_safety(cipro, ctx)
    assert len(flags) == 1
    assert flags[0].severity == "warning"
    assert not has_hard_block(flags)


def test_renal_threshold_contraindicated_is_hard_block():
    ctx = SafetyContext(
        egfr=22.0,
        contraindication_rules=[
            ContraindicationRule(
                "MET-500",
                "Chronic Kidney Disease",
                "dose_adjustment_required",
                "lactic acidosis",
                False,
                renal_threshold={"egfr_below": 30, "action": "contraindicated"},
            )
        ],
    )
    flags = evaluate_drug_safety(METFORMIN, ctx)
    assert has_hard_block(flags)
    assert flags[0].check_type == "renal_dose"


def test_renal_threshold_dose_reduction_is_warning():
    ctx = SafetyContext(
        egfr=38.0,
        contraindication_rules=[
            ContraindicationRule(
                "MET-500",
                "Moderate Renal Impairment",
                "dose_adjustment_required",
                "reduce dose",
                False,
                renal_threshold={"egfr_below": 45, "egfr_above": 30, "action": "reduce_dose_50pct"},
            )
        ],
    )
    flags = evaluate_drug_safety(METFORMIN, ctx)
    assert len(flags) == 1
    assert flags[0].severity == "warning"
    assert not has_hard_block(flags)


def test_normal_egfr_no_renal_flag():
    ctx = SafetyContext(
        egfr=95.0,
        contraindication_rules=[
            ContraindicationRule(
                "MET-500",
                "CKD",
                "dose_adjustment_required",
                "x",
                False,
                renal_threshold={"egfr_below": 30, "action": "contraindicated"},
            )
        ],
    )
    assert evaluate_drug_safety(METFORMIN, ctx) == []


def test_no_flags_when_clean():
    ctx = SafetyContext(current_meds=[METFORMIN])
    assert evaluate_drug_safety(ASPIRIN, ctx) == []


@pytest.mark.parametrize(
    "rule_severity,expected_severity,expected_hard",
    [
        ("contraindicated", "hard_block", True),
        ("major", "critical", False),
        ("moderate", "warning", False),
        ("minor", "info", False),
    ],
)
def test_interaction_severity_matrix(rule_severity, expected_severity, expected_hard):
    ctx = SafetyContext(
        current_meds=[ASPIRIN],
        interaction_rules=[InteractionRule("ASP-75", "DIC-50", rule_severity, "x")],
    )
    flags = evaluate_drug_safety(DICLOFENAC, ctx)
    assert flags[0].severity == expected_severity
    assert flags[0].is_hard_block is expected_hard


# --- Duplicate therapy (not covered by the pairwise interaction-rule table) ---


def test_reordering_active_medication_is_warning():
    ctx = SafetyContext(current_meds=[METFORMIN])
    flags = check_duplicate_therapy(METFORMIN, ctx)
    assert len(flags) == 1
    assert flags[0].check_type == "duplicate_therapy"
    assert flags[0].severity == "warning"
    assert flags[0].details["match_type"] == "same_product"
    assert not flags[0].is_hard_block


def test_same_ingredient_different_product_is_critical():
    """Two different paracetamol brands/strengths -> unintentional double-dosing risk."""
    ctx = SafetyContext(current_meds=[PARACETAMOL_500])
    flags = check_duplicate_therapy(PARACETAMOL_650, ctx)
    assert len(flags) == 1
    assert flags[0].severity == "critical"
    assert not flags[0].is_hard_block
    assert flags[0].details["match_type"] == "same_ingredient"


def test_same_drug_class_different_ingredient_is_warning():
    """Two ACE inhibitors (Enalapril + Ramipril) -> therapeutic duplication warning."""
    ctx = SafetyContext(current_meds=[ENALAPRIL])
    flags = check_duplicate_therapy(RAMIPRIL, ctx)
    assert len(flags) == 1
    assert flags[0].severity == "warning"
    assert flags[0].details["match_type"] == "same_class"


def test_no_duplicate_therapy_flag_for_unrelated_drug():
    ctx = SafetyContext(current_meds=[METFORMIN])
    assert check_duplicate_therapy(ASPIRIN, ctx) == []


def test_duplicate_therapy_is_not_a_hard_block_and_does_not_stop_other_checks():
    """A duplicate-therapy warning must never suppress an allergy hard block on the same drug."""
    ctx = SafetyContext(
        current_meds=[IBUPROFEN],
        allergies=[PatientAllergy(allergen_name="Ibuprofen", drug_reference_id="IBU-400")],
    )
    flags = evaluate_drug_safety(IBUPROFEN, ctx)
    assert has_hard_block(flags)
    types = {f.check_type for f in flags}
    assert "allergy_conflict" in types
    assert "duplicate_therapy" in types


# --- Allergy cross-reactivity (related, not identical, drug-class families) ---

PENICILLIN_DRUG = DrugRef(
    reference_id="AMOX-500", generic_name="Amoxicillin", drug_class="Penicillin"
)
CEPHALOSPORIN_DRUG = DrugRef(
    reference_id="CFX-500", generic_name="Cefixime", drug_class="Cephalosporin"
)


def test_cross_reactive_class_allergy_is_critical_not_hard_block():
    """Penicillin allergy flags a cephalosporin as a dismissible critical alert, not a block."""
    ctx = SafetyContext(
        allergies=[
            PatientAllergy(
                allergen_name="Amoxicillin", drug_reference_id="AMOX-500", drug_class="Penicillin"
            )
        ]
    )
    flags = evaluate_drug_safety(CEPHALOSPORIN_DRUG, ctx)
    assert not has_hard_block(flags)
    assert len(flags) == 1
    assert flags[0].check_type == "allergy_conflict"
    assert flags[0].severity == "critical"
    assert flags[0].details["match_type"] == "cross_reactivity"


def test_unrelated_class_allergy_has_no_cross_reactivity_flag():
    ctx = SafetyContext(
        allergies=[
            PatientAllergy(
                allergen_name="Amoxicillin", drug_reference_id="AMOX-500", drug_class="Penicillin"
            )
        ]
    )
    assert evaluate_drug_safety(METFORMIN, ctx) == []


def test_exact_same_class_allergy_still_hard_blocks_over_cross_reactivity():
    """Exact drug-class match takes the hard-block path, not the softer cross-reactivity one."""
    ctx = SafetyContext(
        allergies=[
            PatientAllergy(
                allergen_name="Ceftriaxone",
                drug_reference_id="CTX-1000",
                drug_class="Cephalosporin",
            )
        ]
    )
    flags = evaluate_drug_safety(CEPHALOSPORIN_DRUG, ctx)
    assert has_hard_block(flags)
    assert flags[0].details["match_type"] == "cross_class"


# --- Guideline adherence (informational-only nudge, never a hard block) ---

STATIN = DrugRef(reference_id="ATOR-10", generic_name="Atorvastatin", drug_class="Statin")
ACE_INHIBITOR = DrugRef(
    reference_id="ENA-5-GL", generic_name="Enalapril", drug_class="ACE Inhibitor"
)
BETA_BLOCKER = DrugRef(reference_id="ATEN-50", generic_name="Atenolol", drug_class="Beta Blocker")


def test_off_first_line_drug_for_matching_condition_is_informational():
    ctx = SafetyContext(conditions=[PatientCondition(condition_name="Hypertension")])
    flags = check_guideline_adherence(BETA_BLOCKER, ctx)
    assert len(flags) == 1
    assert flags[0].check_type == "guideline_deviation"
    assert flags[0].severity == "info"
    assert not flags[0].is_hard_block
    assert flags[0].details["condition"] == "Hypertension"


def test_first_line_drug_for_matching_condition_has_no_flag():
    ctx = SafetyContext(conditions=[PatientCondition(condition_name="Hypertension")])
    assert check_guideline_adherence(ACE_INHIBITOR, ctx) == []


def test_unrelated_drug_for_condition_has_no_guideline_flag():
    """A statin is out of the hypertension therapeutic domain entirely — no noisy false flag."""
    ctx = SafetyContext(conditions=[PatientCondition(condition_name="Hypertension")])
    assert check_guideline_adherence(STATIN, ctx) == []


def test_no_flag_when_condition_not_in_curated_table():
    ctx = SafetyContext(conditions=[PatientCondition(condition_name="Migraine")])
    assert check_guideline_adherence(BETA_BLOCKER, ctx) == []


def test_guideline_deviation_never_a_hard_block_even_alongside_other_flags():
    ctx = SafetyContext(
        conditions=[PatientCondition(condition_name="Hypertension")],
        allergies=[PatientAllergy(allergen_name="Atenolol", drug_reference_id="ATEN-50")],
    )
    flags = evaluate_drug_safety(BETA_BLOCKER, ctx)
    types = {f.check_type for f in flags}
    assert "guideline_deviation" in types
    assert "allergy_conflict" in types
    deviation = next(f for f in flags if f.check_type == "guideline_deviation")
    assert not deviation.is_hard_block


# --- Decisions mutation testing found nothing pinned. Each block below is a mutant that
#     survived the suite: the engine's behaviour was right, and no test would have noticed if
#     it stopped being. ---


def test_an_allergy_matched_only_through_the_vocabulary_still_hard_blocks():
    """The brand-name path, which is the one that matters most and was the one not tested.

    ``check_allergies`` matches three ways: resolved reference id, allergen text against the
    generic name, or drug class. Every existing test set up an allergy whose *text* also matched
    (allergen "Aspirin" vs generic "Aspirin"), so the reference-id arm carried no test of its
    own — deleting it entirely left the suite green.

    That arm is the whole point of the DrugVocabulary pipeline (CLAUDE.md pitfall #4): a patient
    whose chart says "Crocin" and a prescription written as "Paracetamol" share no text and no
    class, only the reference id the resolver puts on both.
    """
    ctx = SafetyContext(
        allergies=[PatientAllergy(allergen_name="Crocin", drug_reference_id="PCM-500")]
    )

    flags = evaluate_drug_safety(PARACETAMOL_500, ctx)

    assert has_hard_block(flags)
    assert flags[0].details["match_type"] == "direct"


def test_an_allergy_matched_only_by_name_reports_itself_as_a_direct_match():
    """The other arm of the same ``or``, for the same reason: an imported allergy with no
    vocabulary link resolved yet still matches on text, and it is a direct match, not a
    cross-class one. The label is what the clinician reads to judge the block."""
    ctx = SafetyContext(allergies=[PatientAllergy(allergen_name="paracetamol")])

    flags = evaluate_drug_safety(PARACETAMOL_500, ctx)

    assert has_hard_block(flags)
    assert flags[0].details["match_type"] == "direct"
    assert "direct match" in flags[0].summary


@pytest.mark.parametrize(
    "is_absolute,severity",
    [(True, "relative"), (False, "absolute")],
    ids=["flag-only", "severity-only"],
)
def test_a_rule_marked_absolute_in_either_column_hard_blocks(is_absolute, severity):
    """``rule.is_absolute or rule.severity == "absolute"`` reads either column, because the two
    can disagree — they are separate columns in curated reference data, edited by hand.

    Every test until now set them consistently, so narrowing that ``or`` to an ``and`` changed
    nothing the suite could see. What it changes in production is the direction that matters: a
    contraindication marked absolute in one column and not the other would stop being a hard
    block and come back as a dismissible warning (Critical Safety Rule #3).
    """
    ctx = SafetyContext(
        conditions=[PatientCondition(condition_name="Peptic Ulcer Disease")],
        contraindication_rules=[
            ContraindicationRule(
                "DIC-50", "Peptic Ulcer Disease", severity, "bleeding risk", is_absolute
            )
        ],
    )

    flags = evaluate_drug_safety(DICLOFENAC, ctx)

    assert has_hard_block(flags)
    assert flags[0].details["is_absolute"] is True


def test_at_the_exact_egfr_boundary_one_band_fires_and_only_one():
    """The bands are half-open — ``[egfr_above, egfr_below)`` — and the seeded metformin rules
    are a matched pair (contraindicated below 30, reduce dose 30–45) that meet at 30.

    A patient sitting exactly on 30 is the case both comparisons decide, and neither was tested.
    Loosening either one breaks it in a different direction: ``egfr >= below`` to ``>`` fires the
    severe band too and hard-blocks at a value the guideline says to dose-reduce at, while
    ``egfr < above`` to ``<=`` drops the moderate band and leaves eGFR 30 with *no* renal flag
    from either rule.
    """
    both_bands = [
        ContraindicationRule(
            "MET-500",
            "Chronic Kidney Disease",
            "dose_adjustment_required",
            "lactic acidosis",
            False,
            renal_threshold={"egfr_below": 30, "action": "contraindicated"},
        ),
        ContraindicationRule(
            "MET-500",
            "Moderate Renal Impairment",
            "dose_adjustment_required",
            "reduce dose",
            False,
            renal_threshold={"egfr_below": 45, "egfr_above": 30, "action": "reduce_dose_50pct"},
        ),
    ]

    flags = evaluate_drug_safety(
        METFORMIN, SafetyContext(egfr=30.0, contraindication_rules=both_bands)
    )

    assert len(flags) == 1
    assert flags[0].details["action"] == "reduce_dose_50pct"
    assert not has_hard_block(flags)

    # One step below the boundary the pair swaps over, and still exactly one band fires.
    severe = evaluate_drug_safety(
        METFORMIN, SafetyContext(egfr=29.9, contraindication_rules=both_bands)
    )
    assert len(severe) == 1
    assert has_hard_block(severe)


def test_an_interaction_severity_the_engine_does_not_recognise_warns_without_blocking():
    """The severity map's fallback. The column has a CHECK constraint, so this is defensive
    code — but it decides what an unrecognised value *becomes*, and the two wrong answers are
    both bad: silently dropping it hides a real interaction, and hard-blocking on it produces a
    block no clinician can act on and no rule text explains. A dismissible warning is the
    answer that degrades honestly, and nothing pinned it there.
    """
    ctx = SafetyContext(
        current_meds=[ASPIRIN],
        interaction_rules=[InteractionRule("ASP-75", "DIC-50", "catastrophic", "x")],
    )

    flags = evaluate_drug_safety(DICLOFENAC, ctx)

    assert len(flags) == 1
    assert flags[0].severity == "warning"
    assert flags[0].is_hard_block is False


def test_no_duplicate_therapy_finding_is_ever_a_hard_block():
    """Duplicate therapy is a "did you mean to" question, not a contraindication. A hard block
    is undismissable and needs a documented override, which is the wrong ceremony for a
    deliberate refill or a second agent in the same class added on purpose.
    """
    ctx = SafetyContext(current_meds=[PARACETAMOL_650, ENALAPRIL, METFORMIN])

    for proposed in (PARACETAMOL_500, RAMIPRIL, METFORMIN):
        flags = check_duplicate_therapy(proposed, ctx)
        assert flags, f"expected a duplicate-therapy finding for {proposed.generic_name}"
        assert not has_hard_block(flags)
