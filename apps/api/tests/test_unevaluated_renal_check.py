"""A renal rule that could not be evaluated must not read as a renal rule that passed.

``_evaluate_renal`` returned ``None`` on two entirely different situations: the patient's eGFR
is above the threshold (checked, fine) and there is no eGFR on the chart at all (not checked).
Both reached the clinician as the same thing — ``is_blocked: false``, no flags — and the Safety
screen renders a missing eGFR as the *absence* of its "eGFR available." footnote, which is to
say as nothing. A patient with no creatinine on file was therefore shown a clean metformin
check, with the rule that hard-blocks it below 30 mL/min never having run.

This is the judgement ``check_medication`` already makes one level up, where a drug name that
resolves to nothing is a 422 rather than an unchecked pass, on the stated grounds that reporting
no problems for a check that never happened is the dangerous answer. These tests hold the same
line one level down.

Deliberately a warning and not a hard block: every renally-dosed drug on every chart without a
creatinine would otherwise be blocked, which is clinically wrong and is how a hard block becomes
something clinicians learn to click past (CLAUDE.md rule #3 is about blocks that mean something).
"""

from __future__ import annotations

from app.core.safety import (
    ContraindicationRule,
    DrugRef,
    SafetyContext,
    evaluate_drug_safety,
    has_hard_block,
)

METFORMIN = DrugRef(reference_id="MET-500", generic_name="Metformin", drug_class="biguanide")


def _contraindicated_below_30() -> ContraindicationRule:
    return ContraindicationRule(
        "MET-500",
        "Chronic Kidney Disease",
        "dose_adjustment_required",
        "lactic acidosis",
        False,
        renal_threshold={"egfr_below": 30, "action": "contraindicated"},
    )


def _reduce_dose_30_to_45() -> ContraindicationRule:
    return ContraindicationRule(
        "MET-500",
        "Moderate Renal Impairment",
        "dose_adjustment_required",
        "reduce dose",
        False,
        renal_threshold={"egfr_below": 45, "egfr_above": 30, "action": "reduce_dose_50pct"},
    )


def test_a_renal_rule_with_no_egfr_is_flagged_as_unevaluated() -> None:
    ctx = SafetyContext(egfr=None, contraindication_rules=[_contraindicated_below_30()])

    flags = evaluate_drug_safety(METFORMIN, ctx)

    assert len(flags) == 1
    flag = flags[0]
    assert flag.check_type == "renal_dose"
    assert flag.severity == "warning"
    assert flag.is_hard_block is False
    assert flag.details["evaluated"] is False
    assert flag.details["egfr"] is None
    assert flag.details["egfr_threshold"] == 30
    # The clinician has to be able to tell this from a check that ran, and to know what to do.
    assert "not performed" in flag.summary
    assert "no eGFR" in flag.summary
    assert "creatinine" in flag.summary


def test_an_unevaluated_renal_rule_does_not_block() -> None:
    """It is a prompt to order a test, not a barrier — see the module docstring."""
    ctx = SafetyContext(egfr=None, contraindication_rules=[_contraindicated_below_30()])

    assert not has_hard_block(evaluate_drug_safety(METFORMIN, ctx))


def test_only_the_strictest_band_reports_itself_unevaluated() -> None:
    """The bands of one rule family are half-open slices of the same threshold.

    Metformin carries both a <30 contraindication and a 30-45 dose reduction. Letting each
    report its own unevaluated state would put two near-identical warnings on one card and say
    nothing the stricter band has not already said, so the base band (no ``egfr_above``) speaks
    and the rest stay quiet.
    """
    ctx = SafetyContext(
        egfr=None,
        contraindication_rules=[_reduce_dose_30_to_45(), _contraindicated_below_30()],
    )

    flags = evaluate_drug_safety(METFORMIN, ctx)

    assert len(flags) == 1
    assert flags[0].details["egfr_threshold"] == 30
    assert flags[0].details["action"] == "contraindicated"


def test_a_drug_with_no_renal_rule_is_unaffected_by_a_missing_egfr() -> None:
    """Absent eGFR only matters where a rule wanted one; it does not flag the whole formulary."""
    ctx = SafetyContext(egfr=None, contraindication_rules=[])

    assert evaluate_drug_safety(METFORMIN, ctx) == []


def test_a_malformed_renal_rule_is_still_silent_without_an_egfr() -> None:
    """A threshold with no ``egfr_below`` has nothing to compare, so there is nothing unrun.

    Guards the ordering inside ``_evaluate_renal``: the "is this rule usable at all" check has
    to come before the "do we have an eGFR" one, or a rule that could never fire would announce
    itself as a check the missing creatinine prevented.
    """
    ctx = SafetyContext(
        egfr=None,
        contraindication_rules=[
            ContraindicationRule(
                "MET-500",
                "CKD",
                "dose_adjustment_required",
                "x",
                False,
                renal_threshold={"action": "review"},  # no egfr_below key
            )
        ],
    )

    assert evaluate_drug_safety(METFORMIN, ctx) == []


def test_a_present_egfr_above_the_threshold_still_produces_no_flag() -> None:
    """The evaluated-and-fine case keeps its silence; only the unevaluated case gained a voice."""
    ctx = SafetyContext(egfr=95.0, contraindication_rules=[_contraindicated_below_30()])

    assert evaluate_drug_safety(METFORMIN, ctx) == []
