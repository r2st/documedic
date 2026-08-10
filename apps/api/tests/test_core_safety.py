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
