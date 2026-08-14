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

Allergy cross-reactivity (check_allergies) additionally consults a curated map of clinically
recognised cross-reactive drug-class families (e.g. penicillins <-> cephalosporins) so a
documented allergy to one class also flags a structurally related class, not only an exact
drug-class match. Cross-reactivity is a well-established but incomplete risk (typically a
minority of patients react), so it is a dismissible "critical" flag rather than a hard block —
unlike a direct or same-class match, which stays a hard block.

Guideline-adherence checking (check_guideline_adherence) is a separate, informational-only,
offline check: for a small curated set of conditions with an unambiguous ICMR STW first-line
class, it notes when a proposed drug is in the same therapeutic domain as an active condition
but outside the guideline-preferred first-line classes for it. It never blocks and never claims
certainty — clinicians routinely have good reasons (prior failure, contraindication, allergy)
to prescribe outside first-line, so this is a nudge, not a rule.
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
    "guideline_deviation",
    "unevaluated_medication",
]

# Symmetric clinically-recognised cross-reactivity between drug-CLASS families. Keys/values are
# lower-cased class names; membership is bidirectional (see _cross_reactive_classes). This is
# deliberately conservative — only well-documented cross-reactivity families are included.
_CROSS_REACTIVITY_CLASSES: dict[str, frozenset[str]] = {
    "penicillin": frozenset({"cephalosporin", "carbapenem"}),
    "cephalosporin": frozenset({"penicillin", "carbapenem"}),
    "carbapenem": frozenset({"penicillin", "cephalosporin"}),
    "sulfonamide antibiotic": frozenset({"sulfonylurea", "thiazide diuretic", "loop diuretic"}),
    "sulfonylurea": frozenset({"sulfonamide antibiotic"}),
    "thiazide diuretic": frozenset({"sulfonamide antibiotic"}),
    "loop diuretic": frozenset({"sulfonamide antibiotic"}),
    "nsaid": frozenset({"salicylate", "antiplatelet"}),
    "salicylate": frozenset({"nsaid"}),
    "antiplatelet": frozenset({"nsaid"}),
}


def _cross_reactive_classes(drug_class: str) -> frozenset[str]:
    return _CROSS_REACTIVITY_CLASSES.get(_norm(drug_class), frozenset())


# condition_name (lower-cased) -> guideline-preferred first-line therapy for that condition.
# `domain_classes` are ALL drug classes plausibly prescribed to treat the condition (so the
# check never fires for an unrelated drug, e.g. an analgesic in a hypertensive patient);
# `first_line_classes` (subset of domain_classes) are the ICMR STW first-line preference.
_GUIDELINE_FIRST_LINE: dict[str, dict] = {
    "hypertension": {
        "domain_classes": frozenset(
            {
                "ace inhibitor",
                "arb",
                "calcium channel blocker",
                "thiazide diuretic",
                "beta blocker",
                "alpha blocker",
                "loop diuretic",
                "potassium-sparing diuretic",
            }
        ),
        "first_line_classes": frozenset(
            {"ace inhibitor", "arb", "calcium channel blocker", "thiazide diuretic"}
        ),
        "guideline_reference": "ICMR STW — Hypertension: first-line pharmacotherapy",
    },
    "type 2 diabetes mellitus": {
        "domain_classes": frozenset(
            {
                "biguanide",
                "sglt2 inhibitor",
                "sulfonylurea",
                "dpp-4 inhibitor",
                "glp-1 agonist",
                "insulin",
                "thiazolidinedione",
                "alpha-glucosidase inhibitor",
            }
        ),
        "first_line_classes": frozenset({"biguanide"}),
        "guideline_reference": (
            "ICMR STW — Type 2 Diabetes Mellitus: first-line pharmacotherapy (metformin)"
        ),
    },
    "dyslipidemia": {
        "domain_classes": frozenset(
            {"statin", "fibrate", "bile acid sequestrant", "cholesterol absorption inhibitor"}
        ),
        "first_line_classes": frozenset({"statin"}),
        "guideline_reference": "ICMR STW — Dyslipidemia: first-line pharmacotherapy (statin)",
    },
}


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
    # Names of current medications that could not be matched to the vocabulary at all, and so
    # are absent from ``current_meds`` and from every rule evaluated against it. See
    # ``check_unevaluated_medications``.
    unresolved_current_meds: list[str] = field(default_factory=list)


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
            continue

        if allergy.drug_class and proposed.drug_class:
            related = _cross_reactive_classes(allergy.drug_class)
            if _norm(proposed.drug_class) in related:
                flags.append(
                    SafetyFlag(
                        check_type="allergy_conflict",
                        severity="critical",
                        is_hard_block=False,
                        summary=(
                            f"Documented allergy to {allergy.allergen_name} "
                            f"({allergy.drug_class}) has recognised cross-reactivity with "
                            f"{proposed.generic_name} ({proposed.drug_class}). Evidence "
                            "suggests considering an alternative or confirming tolerance "
                            "before prescribing."
                        ),
                        details={
                            "allergen": allergy.allergen_name,
                            "allergen_class": allergy.drug_class,
                            "proposed_drug": proposed.generic_name,
                            "proposed_drug_class": proposed.drug_class,
                            "match_type": "cross_reactivity",
                        },
                        allergy_id=allergy.allergy_id,
                    )
                )
    return flags


def check_guideline_adherence(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    """Informational-only nudge when a prescribed drug is off guideline first-line for an
    active condition it plausibly treats. Never a hard block; see module docstring."""
    flags: list[SafetyFlag] = []
    if not proposed.drug_class:
        return flags
    proposed_class = _norm(proposed.drug_class)
    for condition in ctx.conditions:
        entry = _GUIDELINE_FIRST_LINE.get(_norm(condition.condition_name))
        if entry is None or proposed_class not in entry["domain_classes"]:
            continue
        if proposed_class in entry["first_line_classes"]:
            continue
        flags.append(
            SafetyFlag(
                check_type="guideline_deviation",
                severity="info",
                is_hard_block=False,
                summary=(
                    f"{proposed.generic_name} ({proposed.drug_class}) is not among the "
                    f"guideline-preferred first-line classes for {condition.condition_name}. "
                    f"{entry['guideline_reference']}. Consider whether first-line therapy has "
                    "already been tried or is contraindicated for this patient."
                ),
                details={
                    "proposed_drug": proposed.generic_name,
                    "proposed_drug_class": proposed.drug_class,
                    "condition": condition.condition_name,
                    "first_line_classes": sorted(entry["first_line_classes"]),
                    "guideline_reference": entry["guideline_reference"],
                },
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
    if not threshold:
        return None
    below = threshold.get("egfr_below")
    above = threshold.get("egfr_above")
    if below is None:
        return None

    action = threshold.get("action", "review")

    if ctx.egfr is None:
        # A renal rule exists for this drug and there is no eGFR to apply it to. Returning None
        # here -- which is what this did -- makes "could not be evaluated" indistinguishable
        # from "evaluated and fine": the response says `is_blocked: false` with no flags, and
        # the Safety screen renders the missing eGFR as the *absence* of its "eGFR available."
        # footnote, i.e. as nothing at all. So a patient with no creatinine on file was shown a
        # clean metformin check, when the rule that would have hard-blocked it below 30 mL/min
        # simply never ran.
        #
        # This is the same judgement `check_medication` already makes one level up, where a
        # drug name that resolves to nothing is a 422 rather than an unchecked pass: reporting
        # no problems for a check that did not happen is the dangerous answer. Stated as a
        # warning and not a hard block, because blocking every renally-dosed drug for every
        # chart without a creatinine would be both clinically wrong and the kind of unclearable
        # alert that teaches clinicians to click through the real ones.
        #
        # Only the base band (`egfr_above is None`) speaks. The bands of one rule family are
        # half-open slices of the same threshold, so letting each one report its own
        # unevaluated state would put two near-identical warnings on the card for metformin and
        # say nothing the strictest band has not already said.
        if above is not None:
            return None
        return SafetyFlag(
            check_type="renal_dose",
            severity="warning",
            is_hard_block=False,
            summary=(
                f"Renal check not performed for {proposed.generic_name} "
                f"({rule.condition_name}): no eGFR on this chart. Guidelines set a threshold of "
                f"{below} mL/min (action: {action}); a current creatinine is needed to apply it."
            ),
            details={
                "proposed_drug": proposed.generic_name,
                "egfr": None,
                "egfr_threshold": below,
                "action": action,
                "condition": rule.condition_name,
                "evaluated": False,
            },
            contraindication_id=rule.contraindication_id,
        )

    if ctx.egfr >= below:
        return None
    if above is not None and ctx.egfr < above:
        # A more severe (lower) band rule should handle this; skip the dose-reduction band.
        return None

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


def check_unevaluated_medications(ctx: SafetyContext) -> list[SafetyFlag]:
    """Say so when part of the chart could not be evaluated, instead of reporting it as clean.

    A medication row whose name resolves to nothing — an OCR'd brand the vocabulary has not
    been seeded with, a handwritten scrawl, a combination product spelled in a way no candidate
    matches — is dropped from ``current_meds`` by the service that assembles this context.
    Every rule keyed on that list then silently has nothing to say about it: no interaction is
    looked up against it, no same-ingredient or same-class duplicate is detected, and the
    response comes back with no flags at all.

    Which is the answer that reads as "checked, and fine". A chart carrying an unreadable
    "Warf 5mg" beside a proposal of aspirin produces exactly the same empty flag list as a chart
    carrying nothing at all, for the pair that is the textbook major interaction.

    This module already refuses that shape of answer twice — an unresolved *proposed* drug is a
    422 rather than an unchecked pass, and a renal rule with no eGFR to apply reports itself as
    unevaluated rather than as passed. This is the same judgement for the third place the check
    can be incomplete without saying so.

    A warning and not a hard block, for the same reason the renal one is: blocking every
    prescription on every chart with one unreadable medication line would be both clinically
    wrong and the kind of unclearable alert that teaches clinicians to click through the real
    ones. What the clinician needs is to know which line was not read, so they can read it.
    """
    if not ctx.unresolved_current_meds:
        return []
    names = sorted({name.strip() for name in ctx.unresolved_current_meds if name.strip()})
    if not names:
        return []
    listed = ", ".join(f"“{name}”" for name in names)
    return [
        SafetyFlag(
            check_type="unevaluated_medication",
            severity="warning",
            is_hard_block=False,
            summary=(
                f"{len(names)} current medication{'s' if len(names) != 1 else ''} on this chart "
                f"could not be matched to a known drug ({listed}), so no interaction or "
                "duplicate-therapy rule was evaluated against "
                f"{'them' if len(names) != 1 else 'it'} — this is not the same as “no "
                "interactions found”. Confirm what the entry is, or ask for the brand to be "
                "added to the drug vocabulary."
            ),
            details={
                "unresolved_medications": names,
                "evaluated": False,
            },
        )
    ]


def evaluate_drug_safety(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    """Run all deterministic checks for a single proposed medication."""
    flags: list[SafetyFlag] = []
    flags.extend(check_allergies(proposed, ctx))
    flags.extend(check_interactions(proposed, ctx))
    flags.extend(check_contraindications(proposed, ctx))
    flags.extend(check_duplicate_therapy(proposed, ctx))
    flags.extend(check_guideline_adherence(proposed, ctx))
    return flags


def has_hard_block(flags: list[SafetyFlag]) -> bool:
    return any(f.is_hard_block for f in flags)
