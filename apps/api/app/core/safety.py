"""Deterministic drug-safety engine (P1-08b).

Pure functions, no LLM, no I/O — so the exact same logic runs server-side (Python) and
offline client-side (the TypeScript port), and is exhaustively unit-testable. The service
layer loads patient data + reference data from the database and hands plain dataclasses here.

Safety rules (per CLAUDE.md NON-NEGOTIABLE rules 3 & 8):
  * Allergy conflicts (direct or same-drug-class cross-match)  -> HARD BLOCK.
  * Absolute contraindications                                 -> HARD BLOCK.
  * Interaction severity 'contraindicated'                     -> HARD BLOCK.
  * Renal threshold with action 'contraindicated'             -> HARD BLOCK.
Hard blocks can never be dismissed; everything else is a warning/info flag.

Duplicate-therapy detection (check_duplicate_therapy) is a separate, deterministic check: it
flags re-ordering an active medication, prescribing a second product with the same active
ingredient (e.g. two paracetamol brands -> unintentional overdose risk), or prescribing a
second drug in the same therapeutic class the patient is already on (e.g. two ACE inhibitors).
None of these are covered by the interaction-rule table (which only fires on specific curated
drug pairs), so they are a distinct, always-on offline check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["info", "warning", "critical", "hard_block"]
CheckType = Literal[
    "drug_interaction",
    "contraindication",
    "allergy_conflict",
    "renal_dose",
    "hepatic_dose",
    "duplicate_therapy",
]


@dataclass(frozen=True)
class DrugRef:
    reference_id: str
    generic_name: str
    drug_class: str | None = None


@dataclass(frozen=True)
class PatientAllergy:
    allergen_name: str
    drug_reference_id: str | None = None
    drug_class: str | None = None
    allergy_id: str | None = None


@dataclass(frozen=True)
class PatientCondition:
    condition_name: str
    icd10_code: str | None = None


@dataclass(frozen=True)
class InteractionRule:
    drug_a_reference_id: str
    drug_b_reference_id: str
    severity: str  # minor | moderate | major | contraindicated
    description: str
    management: str | None = None
    interaction_id: str | None = None


@dataclass(frozen=True)
class ContraindicationRule:
    drug_reference_id: str
    condition_name: str
    severity: str  # relative | absolute | dose_adjustment_required
    description: str
    is_absolute: bool
    renal_threshold: dict | None = None
    hepatic_threshold: dict | None = None
    contraindication_id: str | None = None


@dataclass(frozen=True)
class SafetyContext:
    """Everything the engine needs about a patient + reference data."""

    current_meds: list[DrugRef] = field(default_factory=list)
    allergies: list[PatientAllergy] = field(default_factory=list)
    conditions: list[PatientCondition] = field(default_factory=list)
    egfr: float | None = None
    interaction_rules: list[InteractionRule] = field(default_factory=list)
    contraindication_rules: list[ContraindicationRule] = field(default_factory=list)


@dataclass(frozen=True)
class SafetyFlag:
    check_type: CheckType
    severity: Severity
    is_hard_block: bool
    summary: str
    details: dict = field(default_factory=dict)
    drug_interaction_id: str | None = None
    contraindication_id: str | None = None
    allergy_id: str | None = None


_INTERACTION_SEVERITY_MAP: dict[str, tuple[Severity, bool]] = {
    "contraindicated": ("hard_block", True),
    "major": ("critical", False),
    "moderate": ("warning", False),
    "minor": ("info", False),
}


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _interaction_key(a: str, b: str) -> tuple[str, str]:
    """Pairs are stored alphabetically (drug_a < drug_b)."""
    return (a, b) if a <= b else (b, a)


def check_allergies(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    flags: list[SafetyFlag] = []
    for allergy in ctx.allergies:
        direct = (
            allergy.drug_reference_id is not None
            and allergy.drug_reference_id == proposed.reference_id
        )
        name_match = _norm(allergy.allergen_name) == _norm(proposed.generic_name)
        cross_class = (
            allergy.drug_class is not None
            and proposed.drug_class is not None
            and _norm(allergy.drug_class) == _norm(proposed.drug_class)
        )
        if direct or name_match or cross_class:
            reason = (
                "direct match"
                if (direct or name_match)
                else f"cross-class match (class: {proposed.drug_class})"
            )
            flags.append(
                SafetyFlag(
                    check_type="allergy_conflict",
                    severity="hard_block",
                    is_hard_block=True,
                    summary=(
                        f"Documented allergy to {allergy.allergen_name} conflicts with "
                        f"{proposed.generic_name} ({reason}). This is a hard block."
                    ),
                    details={
                        "allergen": allergy.allergen_name,
                        "proposed_drug": proposed.generic_name,
                        "match_type": "direct" if (direct or name_match) else "cross_class",
                    },
                    allergy_id=allergy.allergy_id,
                )
            )
    return flags


def check_interactions(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    flags: list[SafetyFlag] = []
    rules_by_pair = {
        _interaction_key(r.drug_a_reference_id, r.drug_b_reference_id): r
        for r in ctx.interaction_rules
    }
    for med in ctx.current_meds:
        if med.reference_id == proposed.reference_id:
            continue
        rule = rules_by_pair.get(_interaction_key(proposed.reference_id, med.reference_id))
        if rule is None:
            continue
        severity, hard = _INTERACTION_SEVERITY_MAP.get(rule.severity, ("warning", False))
        flags.append(
            SafetyFlag(
                check_type="drug_interaction",
                severity=severity,
                is_hard_block=hard,
                summary=(
                    f"{rule.severity.capitalize()} interaction between "
                    f"{proposed.generic_name} and {med.generic_name}: {rule.description}"
                ),
                details={
                    "proposed_drug": proposed.generic_name,
                    "interacting_drug": med.generic_name,
                    "severity": rule.severity,
                    "management": rule.management,
                },
                drug_interaction_id=rule.interaction_id,
            )
        )
    return flags


def check_contraindications(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    flags: list[SafetyFlag] = []
    condition_names = {_norm(c.condition_name) for c in ctx.conditions}
    for rule in ctx.contraindication_rules:
        if rule.drug_reference_id != proposed.reference_id:
            continue

        condition_present = _norm(rule.condition_name) in condition_names

        # --- Renal-threshold evaluation (works even if the named condition is absent,
        #     because eGFR is a measured value). ---
        renal_flag = _evaluate_renal(proposed, rule, ctx)
        if renal_flag is not None:
            flags.append(renal_flag)
            continue

        if not condition_present:
            continue

        if rule.is_absolute or rule.severity == "absolute":
            flags.append(
                SafetyFlag(
                    check_type="contraindication",
                    severity="hard_block",
                    is_hard_block=True,
                    summary=(
                        f"{proposed.generic_name} is contraindicated in "
                        f"{rule.condition_name}: {rule.description}. This is a hard block."
                    ),
                    details={
                        "proposed_drug": proposed.generic_name,
                        "condition": rule.condition_name,
                        "is_absolute": True,
                    },
                    contraindication_id=rule.contraindication_id,
                )
            )
        else:
            flags.append(
                SafetyFlag(
                    check_type="contraindication",
                    severity="warning",
                    is_hard_block=False,
                    summary=(
                        f"Caution: {proposed.generic_name} with {rule.condition_name}: "
                        f"{rule.description}"
                    ),
                    details={
                        "proposed_drug": proposed.generic_name,
                        "condition": rule.condition_name,
                        "severity": rule.severity,
                    },
                    contraindication_id=rule.contraindication_id,
                )
            )
    return flags


def _evaluate_renal(
    proposed: DrugRef, rule: ContraindicationRule, ctx: SafetyContext
) -> SafetyFlag | None:
    threshold = rule.renal_threshold
    if not threshold or ctx.egfr is None:
        return None
    below = threshold.get("egfr_below")
    above = threshold.get("egfr_above")
    if below is None:
        return None
    if ctx.egfr >= below:
        return None
    if above is not None and ctx.egfr < above:
        # A more severe (lower) band rule should handle this; skip the dose-reduction band.
        return None

    action = threshold.get("action", "review")
    is_hard = action == "contraindicated"
    return SafetyFlag(
        check_type="renal_dose",
        severity="hard_block" if is_hard else "warning",
        is_hard_block=is_hard,
        summary=(
            f"Renal alert: patient eGFR {ctx.egfr} mL/min is below {below} for "
            f"{proposed.generic_name} ({rule.condition_name}). Recommended action: {action}."
        ),
        details={
            "proposed_drug": proposed.generic_name,
            "egfr": ctx.egfr,
            "egfr_threshold": below,
            "action": action,
            "condition": rule.condition_name,
        },
        contraindication_id=rule.contraindication_id,
    )


def check_duplicate_therapy(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    """Flag re-ordering an active med, a same-ingredient duplicate, or a same-class duplicate.

    None of these are "interactions" in the curated pairwise-rule sense, so they fall outside
    ``check_interactions`` entirely; without this check two brands of the same generic (or two
    drugs in the same class) can be prescribed side by side with zero flags raised.
    """
    flags: list[SafetyFlag] = []
    proposed_generic = _norm(proposed.generic_name)
    proposed_class = _norm(proposed.drug_class) if proposed.drug_class else None

    for med in ctx.current_meds:
        if med.reference_id == proposed.reference_id:
            flags.append(
                SafetyFlag(
                    check_type="duplicate_therapy",
                    severity="warning",
                    is_hard_block=False,
                    summary=(
                        f"Patient already has an active order for {proposed.generic_name}. "
                        "Confirm whether this is an intentional refill/dose change or a "
                        "duplicate order."
                    ),
                    details={
                        "proposed_drug": proposed.generic_name,
                        "match_type": "same_product",
                    },
                )
            )
            continue

        med_generic = _norm(med.generic_name)
        if proposed_generic and med_generic == proposed_generic:
            flags.append(
                SafetyFlag(
                    check_type="duplicate_therapy",
                    severity="critical",
                    is_hard_block=False,
                    summary=(
                        f"{proposed.generic_name} has the same active ingredient as a "
                        f"different product the patient is already on ({med.generic_name}). "
                        "Risk of unintentional double-dosing."
                    ),
                    details={
                        "proposed_drug": proposed.generic_name,
                        "existing_drug": med.generic_name,
                        "match_type": "same_ingredient",
                    },
                )
            )
            continue

        med_class = _norm(med.drug_class) if med.drug_class else None
        if proposed_class and med_class and med_class == proposed_class:
            flags.append(
                SafetyFlag(
                    check_type="duplicate_therapy",
                    severity="warning",
                    is_hard_block=False,
                    summary=(
                        f"Therapeutic duplication: {proposed.generic_name} is in the same "
                        f"class ({proposed.drug_class}) as {med.generic_name}, which the "
                        "patient is already on."
                    ),
                    details={
                        "proposed_drug": proposed.generic_name,
                        "existing_drug": med.generic_name,
                        "drug_class": proposed.drug_class,
                        "match_type": "same_class",
                    },
                )
            )

    return flags


def evaluate_drug_safety(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    """Run all deterministic checks for a single proposed medication."""
    flags: list[SafetyFlag] = []
    flags.extend(check_allergies(proposed, ctx))
    flags.extend(check_interactions(proposed, ctx))
    flags.extend(check_contraindications(proposed, ctx))
    flags.extend(check_duplicate_therapy(proposed, ctx))
    return flags


def has_hard_block(flags: list[SafetyFlag]) -> bool:
    return any(f.is_hard_block for f in flags)
