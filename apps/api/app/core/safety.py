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

Every check runs against the *molecules* a proposed product contains, not against the product
row alone. A fixed-dose combination — Glycomet GP, Telma H, Augmentin, which between them are
among the most prescribed products in this market — is one vocabulary row carrying several
active ingredients, and every curated rule here is keyed on a single molecule. Matching a
combination by its own reference id, generic name and drug class matched it against nothing at
all, so it came back clean. See ``_ingredients``.

Duplicate-therapy detection (check_duplicate_therapy) is a separate, deterministic check: it
flags re-ordering an active medication, prescribing a second product with the same active
ingredient (e.g. two paracetamol brands -> unintentional overdose risk), or prescribing a
second drug in the same therapeutic class the patient is already on (e.g. two ACE inhibitors).
None of these are covered by the interaction-rule table (which only fires on specific curated
drug pairs), so they are a distinct, always-on offline check.

Cumulative-burden detection (check_hepatotoxic_burden, check_bleeding_burden) is the same
argument applied to risk that accrues across a whole medication list rather than within a pair.
A table of pairs cannot say "each of these pairs is acceptable and the four together are not",
which is precisely the shape of the two commonest dangerous polypharmacy patterns: stacked
hepatotoxic drugs, and stacked antithrombotics.

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

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from app.core.hepatic import HepaticSeverity, assess_hepatic_severity

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
    "unevaluated_allergy",
    "hepatic_severity",
    "hepatotoxic_burden",
    "bleeding_burden",
    "geriatric_caution",
    "unevaluated_condition",
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
    # Curated liver-injury tier, or None for "not curated" — which is emphatically not "safe for
    # the liver". See ``check_hepatotoxic_burden``, which is written so a None contributes
    # nothing rather than counting as a clean drug.
    hepatotoxicity: str | None = None
    # The active ingredients of a fixed-dose combination, empty for a single-ingredient product.
    # See ``_ingredients`` for why every check here is written against these rather than against
    # the product alone.
    components: tuple[DrugRef, ...] = ()


# --- Fixed-dose combinations --------------------------------------------------------------------
#
# Every curated rule in this engine is keyed on a single molecule: an interaction is a pair of
# reference ids, a contraindication is one reference id and a condition, an allergy match is a
# generic name or a drug class. A fixed-dose combination is one product row carrying several
# molecules — "Metformin + Glimepiride" (Glycomet GP), "Telmisartan + HCTZ" (Telma H),
# "Amoxicillin + Clavulanic acid" (Augmentin) — and matching it against those rules by its own
# reference id, its own generic name and its own drug class is matching it against nothing.
#
# Nothing is what it produced. Glycomet GP prescribed at eGFR 20 came back with no flags at all:
# the metformin hard block below 30 is written on MET-500, and MET-GLM-1-500 is not MET-500, so
# ``_evaluate_renal`` never saw the rule. Augmentin for a patient with a documented amoxicillin
# allergy came back clean, because "Amoxicillin + Clavulanic acid" is not "Amoxicillin" and the
# class "Penicillin + BLI" is not "Penicillin" — the single most common drug allergy in this
# product's market, against the combination form of the drug it is an allergy to, failing open.
# Telma 40 alongside Telma H is a doubled telmisartan dose that ``check_duplicate_therapy``
# reported as two unrelated drugs. In India, where these brands are among the most prescribed
# products on the market, the combination is not the edge case — it is the prescription.
#
# The fix is to stop treating a combination as a drug and start treating it as the set of drugs
# it contains. Each check below evaluates the *identities* of the product: the product itself
# first (so a rule curated against the combination as such still fires), then each ingredient.
# Ingredients are curated data carried on the vocabulary row, not a split of the generic name on
# "+": which molecules a brand contains is a fact about the product, and inferring it from
# punctuation would put an unverified ingredient list underneath a hard block.
#
# An ingredient with no reference id of its own (clavulanic acid, hydrochlorothiazide — real
# molecules with no standalone row in this vocabulary) carries an empty one, and still carries a
# name and a class, so it participates in allergy matching and duplicate-therapy detection and
# simply matches no reference-id-keyed rule. That is a smaller gap than the one it replaces, and
# it fails in the direction of saying less rather than of saying "clean". Every comparison of
# two reference ids below has to check the id is non-empty first, or two unrelated unidentified
# molecules would compare equal.


@dataclass(frozen=True)
class _Ingredient:
    """One identity a rule may be written against, and the product it was reached through.

    ``product`` is what the clinician is prescribing and what every flag must name — a warning
    about "Metformin" on a chart whose prescription says "Glycomet GP" reads as being about some
    other drug. ``drug`` is the identity the rule matched. They are the same object for a
    single-ingredient product, and for the whole-product pass over a combination.
    """

    product: DrugRef
    drug: DrugRef

    @property
    def is_component(self) -> bool:
        # Identity, not reference id: an ingredient with no standalone vocabulary row carries an
        # empty id, and comparing ids would make that ingredient's identity depend on the
        # product's rather than on which object it is.
        return self.drug is not self.product

    @property
    def label(self) -> str:
        """How to name this in clinician-facing text."""
        if not self.is_component:
            return self.product.generic_name
        return f"{self.product.generic_name} (via its {self.drug.generic_name} component)"

    def details(self) -> dict:
        """The identity half of a flag's ``details``, naming the component when there is one."""
        base: dict = {"proposed_drug": self.product.generic_name}
        if self.is_component:
            base["component"] = self.drug.generic_name
            base["component_reference_id"] = self.drug.reference_id
        return base


def _ingredients(drug: DrugRef) -> tuple[_Ingredient, ...]:
    """The product, then each active ingredient it contains.

    Product first so that a rule curated against the combination as a product — which is the
    right place for anything true of the formulation rather than of a molecule — wins the
    "first match" races below and is named without a component qualifier.
    """
    return (_Ingredient(drug, drug), *(_Ingredient(drug, part) for part in drug.components))


def ingredient_reference_ids(drug: DrugRef) -> set[str]:
    """Every reference id a rule for this product could be keyed on.

    The service layer scopes its rule-table queries to the drugs in play; a combination whose
    components were left out of that scope loads none of its components' rules, and the checks
    below then find nothing to apply. Exported so the two stay in step.
    """
    return {i.drug.reference_id for i in _ingredients(drug) if i.drug.reference_id}


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
    icd10_code: str | None = None


@dataclass(frozen=True)
class HepaticPanel:
    """The measured liver function a hepatic dose-adjustment rule can be applied to.

    Markers only, in the canonical units ``app.core.lab_safety`` normalises to — never a class.
    Child-Pugh, which is what the hepatic dosing literature is actually written against, needs
    ascites and encephalopathy graded by a clinician, and neither is in this record as structured
    data; a class asserted from labs alone would be the confident-wrong number this engine
    refuses elsewhere. What ``app.core.hepatic`` does instead is bound the class from these
    values and name the letters the bedside grading could still move it between.

    Bilirubin and ALT drive the curated dose-adjustment thresholds. Albumin, INR and the serum
    creatinine are the further inputs Child-Pugh and MELD need; they are carried here rather than
    fetched separately so the whole hepatic picture comes from one read of the chart, at one
    moment, and cannot disagree with itself.
    """

    bilirubin_mg_dl: float | None = None
    alt_u_l: float | None = None
    albumin_g_dl: float | None = None
    inr: float | None = None
    creatinine_mg_dl: float | None = None

    def __bool__(self) -> bool:
        """True when the chart measures this liver at all.

        The creatinine is deliberately excluded: it is a MELD input, not a liver function test,
        and letting it make the panel truthy would turn "no LFTs on this chart" into "LFTs
        available" for every patient who has ever had a renal panel — which is the exact
        false-reassurance this whole axis exists to prevent.
        """
        return any(
            value is not None
            for value in (self.bilirubin_mg_dl, self.alt_u_l, self.albumin_g_dl, self.inr)
        )


@dataclass(frozen=True)
class SafetyContext:
    """Everything the engine needs about a patient + reference data."""

    current_meds: list[DrugRef] = field(default_factory=list)
    allergies: list[PatientAllergy] = field(default_factory=list)
    conditions: list[PatientCondition] = field(default_factory=list)
    egfr: float | None = None
    # Whole years at the time of the check, or None when the record carries no usable date of
    # birth. None is "not known", never "not elderly" — see ``check_geriatric_cautions``, which
    # says so on the screen rather than falling silent.
    age_years: int | None = None
    hepatic: HepaticPanel = field(default_factory=HepaticPanel)
    interaction_rules: list[InteractionRule] = field(default_factory=list)
    contraindication_rules: list[ContraindicationRule] = field(default_factory=list)
    # Names of current medications that could not be matched to the vocabulary at all, and so
    # are absent from ``current_meds`` and from every rule evaluated against it. See
    # ``check_unevaluated_medications``.
    unresolved_current_meds: list[str] = field(default_factory=list)
    # Names of documented *drug* allergies the vocabulary could not identify, so the entry
    # carries no reference id and no drug class. See ``check_unevaluated_allergies``.
    unresolved_allergies: list[str] = field(default_factory=list)


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


def _allergy_conflict(allergy: PatientAllergy, ing: _Ingredient) -> str | None:
    """``"direct"``/``"cross_class"`` if this allergy hard-blocks this identity, else None."""
    drug = ing.drug
    direct = bool(allergy.drug_reference_id) and allergy.drug_reference_id == drug.reference_id
    name_match = _norm(allergy.allergen_name) == _norm(drug.generic_name)
    if direct or name_match:
        return "direct"
    if (
        allergy.drug_class is not None
        and drug.drug_class is not None
        and _norm(allergy.drug_class) == _norm(drug.drug_class)
    ):
        return "cross_class"
    return None


def check_allergies(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    """Hard-block a documented allergy against the product or any ingredient it contains.

    One flag per allergy at most, and the hard block wins: an allergen that both matches an
    ingredient outright and cross-reacts with another is one conflict, not two, and stacking the
    dismissible flag on top of the undismissible one only adds noise to a card that already
    cannot be cleared.
    """
    flags: list[SafetyFlag] = []
    for allergy in ctx.allergies:
        blocked = next(
            (
                (ing, match_type)
                for ing in _ingredients(proposed)
                if (match_type := _allergy_conflict(allergy, ing)) is not None
            ),
            None,
        )
        if blocked is not None:
            ing, match_type = blocked
            reason = (
                "direct match"
                if match_type == "direct"
                else f"cross-class match (class: {ing.drug.drug_class})"
            )
            flags.append(
                SafetyFlag(
                    check_type="allergy_conflict",
                    severity="hard_block",
                    is_hard_block=True,
                    summary=(
                        f"Documented allergy to {allergy.allergen_name} conflicts with "
                        f"{ing.label} ({reason}). This is a hard block."
                    ),
                    details={
                        "allergen": allergy.allergen_name,
                        **ing.details(),
                        "match_type": match_type,
                    },
                    allergy_id=allergy.allergy_id,
                )
            )
            continue

        if not allergy.drug_class:
            continue
        related = _cross_reactive_classes(allergy.drug_class)
        cross = next(
            (
                ing
                for ing in _ingredients(proposed)
                if ing.drug.drug_class and _norm(ing.drug.drug_class) in related
            ),
            None,
        )
        if cross is not None:
            flags.append(
                SafetyFlag(
                    check_type="allergy_conflict",
                    severity="critical",
                    is_hard_block=False,
                    summary=(
                        f"Documented allergy to {allergy.allergen_name} "
                        f"({allergy.drug_class}) has recognised cross-reactivity with "
                        f"{cross.label} ({cross.drug.drug_class}). Evidence "
                        "suggests considering an alternative or confirming tolerance "
                        "before prescribing."
                    ),
                    details={
                        "allergen": allergy.allergen_name,
                        "allergen_class": allergy.drug_class,
                        **cross.details(),
                        "proposed_drug_class": cross.drug.drug_class,
                        "match_type": "cross_reactivity",
                    },
                    allergy_id=allergy.allergy_id,
                )
            )
    return flags


def check_guideline_adherence(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    """Informational-only nudge when a prescribed drug is off guideline first-line for an
    active condition it plausibly treats. Never a hard block; see module docstring.

    A combination product is adherent if *any* ingredient it carries is first-line: metformin
    plus glimepiride is the guideline's own step-up from metformin, and calling it a deviation
    because one of its two molecules is a sulfonylurea would be the check contradicting the
    guideline it cites.
    """
    flags: list[SafetyFlag] = []
    classes = [(ing, _norm(ing.drug.drug_class)) for ing in _ingredients(proposed)]
    classes = [(ing, name) for ing, name in classes if name]
    if not classes:
        return flags
    for condition in ctx.conditions:
        entry = _GUIDELINE_FIRST_LINE.get(_norm(condition.condition_name))
        if entry is None:
            continue
        if any(name in entry["first_line_classes"] for _ing, name in classes):
            continue
        in_domain = next(
            ((ing, name) for ing, name in classes if name in entry["domain_classes"]), None
        )
        if in_domain is None:
            continue
        ing, _name = in_domain
        flags.append(
            SafetyFlag(
                check_type="guideline_deviation",
                severity="info",
                is_hard_block=False,
                summary=(
                    f"{ing.label} ({ing.drug.drug_class}) is not among the "
                    f"guideline-preferred first-line classes for {condition.condition_name}. "
                    f"{entry['guideline_reference']}. Consider whether first-line therapy has "
                    "already been tried or is contraindicated for this patient."
                ),
                details={
                    **ing.details(),
                    "proposed_drug_class": ing.drug.drug_class,
                    "condition": condition.condition_name,
                    "first_line_classes": sorted(entry["first_line_classes"]),
                    "guideline_reference": entry["guideline_reference"],
                },
            )
        )
    return flags


def check_interactions(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    """Every curated pair between an identity of the proposal and one of a current medication.

    Combination products make this a cross-product rather than a single lookup. Telma H beside
    enalapril is the curated ARB/ACE-inhibitor pair, reachable only through Telma H's telmisartan
    component; Glycomet GP before a contrast study is the metformin/contrast lactic-acidosis
    pair, reachable only through its metformin. Both products matched no rule at all until each
    was read as the molecules it contains.

    Two products can meet in more than one curated rule — a two-molecule product against a
    two-molecule product has four candidate pairs — and each such rule is a separate clinical
    fact, so each is flagged. What is *not* evaluated is a pair inside one product: a licensed
    fixed-dose combination is a formulation decision already made, and flagging it as an
    interaction would put an alert on the card that the prescriber cannot act on.
    """
    flags: list[SafetyFlag] = []
    rules_by_pair = {
        _interaction_key(r.drug_a_reference_id, r.drug_b_reference_id): r
        for r in ctx.interaction_rules
    }
    proposed_ingredients = _ingredients(proposed)
    for med in ctx.current_meds:
        if med.reference_id == proposed.reference_id:
            continue
        seen: set[tuple[str, str]] = set()
        for ing in proposed_ingredients:
            for med_ing in _ingredients(med):
                # An empty id belongs to an ingredient with no standalone vocabulary row; no
                # curated rule can be keyed on one, and two of them are not the same molecule.
                if not ing.drug.reference_id or ing.drug.reference_id == med_ing.drug.reference_id:
                    continue
                key = _interaction_key(ing.drug.reference_id, med_ing.drug.reference_id)
                if key in seen:
                    continue
                rule = rules_by_pair.get(key)
                if rule is None:
                    continue
                seen.add(key)
                severity, hard = _INTERACTION_SEVERITY_MAP.get(rule.severity, ("warning", False))
                flags.append(
                    SafetyFlag(
                        check_type="drug_interaction",
                        severity=severity,
                        is_hard_block=hard,
                        summary=(
                            f"{rule.severity.capitalize()} interaction between "
                            f"{ing.label} and {med_ing.label}: {rule.description}"
                        ),
                        details={
                            **ing.details(),
                            "interacting_drug": med.generic_name,
                            **(
                                {"interacting_component": med_ing.drug.generic_name}
                                if med_ing.is_component
                                else {}
                            ),
                            "severity": rule.severity,
                            "management": rule.management,
                        },
                        drug_interaction_id=rule.interaction_id,
                    )
                )
    return flags


# --- Condition matching -----------------------------------------------------------------------
#
# A contraindication rule is keyed on a condition *name*, and the chart's name for the same
# condition is whatever a clinician wrote or an OCR pass lifted off a referral letter. Comparing
# the two with string equality — which is what this did — meant an absolute contraindication was
# enforced only when the chart happened to spell it exactly as the curated rule does. A chart
# saying "Asthma", the ordinary way it is written, did not hard-block atenolol; "Bronchial
# Asthma" did. That is CLAUDE.md rule 3 defeated by wording, and it fails open: the response was
# `is_blocked: false` with no flag saying the comparison had been attempted and missed.
#
# So the comparison is widened, deterministically and offline (rule 8), on three axes, and
# whatever the widening still cannot resolve is *reported* rather than dropped — the same
# judgement this module already makes for an unresolvable drug name and a missing eGFR.
#
# The widening is deliberately asymmetric. A chart MORE specific than the rule ("Ectopic
# Pregnancy" against a rule for "Pregnancy") is the rule's condition plus detail, so the block
# stands. A chart LESS specific than the rule ("Renal Impairment" against a rule for *Severe*
# renal impairment) is not evidence the patient meets it, so it becomes a warning instead. The
# safe direction to be wrong is the one a clinician can clear with a documented override; the
# unsafe direction is the one that says nothing at all.

# Chart wordings that mean the condition is NOT this patient's problem. Phrases are matched as
# substrings of the normalised name; single words are matched as whole tokens, so "no" catches
# "no h/o asthma" without "nocturnal" catching itself.
_CONDITION_ABSENT_PHRASES = ("family history", "fh of", "negative for", "not present", "ruled out")
_CONDITION_ABSENT_TOKENS = frozenset(
    {"no", "not", "nil", "denies", "denied", "negative", "excluded", "absent", "ruled"}
)

# Wordings that mean the condition is hedged, historical, or still a question. Not grounds for a
# hard block — the chart does not assert the patient currently has it — but very much grounds for
# telling the clinician the rule exists. "h/o peptic ulcer disease" against an NSAID is the
# textbook case: not a block, never nothing.
_CONDITION_UNCERTAIN = (
    "suspected",
    "possible",
    "probable",
    "query ",
    "?",
    "r/o ",
    "rule out",
    "h/o",
    "history of",
    "past ",
    "previous",
    "resolved",
    # The ways a chart says a condition has ended, beyond the generic "resolved". Pregnancy is
    # where this bites: "Pregnancy — delivered" or "Ectopic pregnancy, terminated" left on a
    # problem list would otherwise hard-block ramipril on a postpartum woman forever, and
    # postpartum hypertension is exactly when an ACE inhibitor is correctly prescribed. Hedged
    # rather than absent, so the rule is still named — a chart is not always right about what
    # has ended.
    "terminated",
    "aborted",
    "miscarried",
    "delivered",
    "s/p ",
    "status post",
    # Deliberately NOT here: "postpartum". "Postpartum haemorrhage" is active bleeding and an
    # emergency, and hedging it would downgrade warfarin's block to an advisory line.
)

# British spellings folded to the American forms the curated rules use. Applied per token rather
# than as substring rewrites, because the obvious substring rules ("oe" -> "e") also rewrite
# ordinary words like "toe".
_SPELLING_VARIANTS: dict[str, str] = {
    "haemorrhage": "hemorrhage",
    "haemorrhagic": "hemorrhagic",
    "haematemesis": "hematemesis",
    "anaemia": "anemia",
    "oedema": "edema",
    "diarrhoea": "diarrhea",
    "ischaemia": "ischemia",
    "ischaemic": "ischemic",
    "oesophageal": "esophageal",
    "oesophagitis": "esophagitis",
    "paediatric": "pediatric",
}

# Tokens that carry no identifying weight, dropped before the specificity comparison below.
# Severity and course words (mild/moderate/severe/acute/chronic) are deliberately NOT here:
# "Moderate Renal Impairment" and "Severe Renal Impairment" are two different curated rules with
# two different eGFR bands, and collapsing them would answer with the wrong one.
_CONDITION_STOPWORDS = frozenset(
    {"disease", "disorder", "syndrome", "condition", "of", "the", "and", "with", "due", "to"}
)

# Chart wordings that ARE the curated rule's condition under another name. Each entry is a
# clinical synonym, not a fuzzy guess: this table is the reason a hard block survives being
# written the way clinicians actually write it. Keys and values are already normalised.
_CONDITION_ALIASES: dict[str, str] = {
    # Airways — the seeded atenolol block is keyed on "Bronchial Asthma".
    "asthma": "bronchial asthma",
    "reactive airway": "bronchial asthma",
    "bronchospasm": "bronchial asthma",
    # Peptic ulcer — the seeded NSAID blocks.
    "pud": "peptic ulcer",
    "gastric ulcer": "peptic ulcer",
    "duodenal ulcer": "peptic ulcer",
    "gastroduodenal ulcer": "peptic ulcer",
    # Peptic disease that is not stated to be an ulcer. Deliberately rewritten to a *subset* of
    # the rule's tokens rather than to the rule itself: "acid peptic disease" is the Indian
    # umbrella term and covers gastritis and reflux as well as ulceration, so it is the chart
    # being less specific than the rule — which _match_condition already knows how to answer,
    # with a warning naming the rule instead of a block asserting a diagnosis the chart does not
    # make. Before this it matched nothing at all and the NSAID rule was silent.
    #
    # "APD" is also automated peritoneal dialysis in a nephrology note, and this cannot tell the
    # two apart. Kept because the collision is cheap in exactly one direction: the rewrite lands
    # on the less-specific branch, so the worst a dialysis patient gets is one advisory line
    # naming a rule that does not apply to them — never a block, and never a diagnosis asserted.
    "acid peptic disease": "peptic",
    "apd": "peptic",
    # Pregnancy — the seeded ACE-inhibitor, statin, warfarin and methotrexate blocks.
    "pregnant": "pregnancy",
    "gravid": "pregnancy",
    "primigravida": "pregnancy",
    "primi": "pregnancy",
    "multigravida": "pregnancy",
    "intrauterine pregnancy": "pregnancy",
    "antenatal": "pregnancy",
    # Chronic kidney disease — metformin's dose-adjustment rule.
    "ckd": "chronic kidney",
    "crf": "chronic kidney",
    "chronic renal failure": "chronic kidney",
    "chronic renal insufficiency": "chronic kidney",
    "chronic kidney failure": "chronic kidney",
    # Active bleeding — warfarin's absolute block.
    "active bleed": "active bleeding",
    "active hemorrhage": "active bleeding",
    "gi bleed": "active bleeding",
    "gi bleeding": "active bleeding",
    "gastrointestinal bleed": "active bleeding",
    "gastrointestinal bleeding": "active bleeding",
    "malena": "active bleeding",
    "malaena": "active bleeding",
    "melena": "active bleeding",
    # Bleeding named by where it came out rather than by the word "bleeding". Each of these IS
    # active bleeding by definition — haematemesis is vomited blood — so the block stands rather
    # than softening to a warning. "gi bleed" above only covers a chart that spells "GI" as its
    # own word; "UGI bleed" is one token and matched nothing.
    "hematemesis": "active bleeding",
    "hematochezia": "active bleeding",
    # A haemorrhage named by its site rather than by the word "active": postpartum,
    # intracranial, subarachnoid, variceal. Every one of those was silent, because the existing
    # entry is "active hemorrhage" and that key is not a subset of "postpartum hemorrhage".
    #
    # This is the one entry here with a real false positive in it — a diabetic carrying "retinal
    # haemorrhage" on the problem list now hard-blocks warfarin. Kept, on this file's own stated
    # asymmetry: the safe direction to be wrong is the one a clinician clears with a documented
    # override, and the unsafe one is the direction that says nothing at all about an
    # intracranial bleed.
    "hemorrhage": "active bleeding",
    "ugi bleed": "active bleeding",
    "ugi bleeding": "active bleeding",
    "pr bleed": "active bleeding",
    "pr bleeding": "active bleeding",
    "per rectal bleeding": "active bleeding",
    # "Bleeding P/R", which the punctuation stripper turns into three tokens.
    "bleeding p r": "active bleeding",
    # G6PD deficiency — ciprofloxacin's relative rule.
    "g6pd": "g6pd deficiency",
    "glucose 6 phosphate dehydrogenase deficiency": "g6pd deficiency",
    # Unstable angina — sildenafil's absolute block.
    "unstable angina pectoris": "unstable angina",
    "crescendo angina": "unstable angina",
}

_PARENTHETICAL = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")

# The synonym table as token sets, longest key first so a specific synonym is applied before a
# general one. Rewriting token sets rather than whole strings is what lets one entry cover every
# wording built around it: "asthma" -> "bronchial asthma" also resolves "severe persistent
# asthma", which a whole-string table would have missed.
_ALIAS_REWRITES: tuple[tuple[frozenset[str], frozenset[str]], ...] = tuple(
    sorted(
        (
            (frozenset(key.split()) - _CONDITION_STOPWORDS, frozenset(value.split()))
            for key, value in _CONDITION_ALIASES.items()
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


@dataclass(frozen=True)
class _ConditionMatch:
    """A charted condition taken to be a contraindication rule's condition.

    ``is_present`` false means "related, but the chart does not assert the patient currently has
    it" — reported as a warning rather than enforced as a block.
    """

    charted_name: str
    basis: str
    is_present: bool


@lru_cache(maxsize=2048)
def _condition_words(name: str | None) -> tuple[str, ...]:
    """A condition name as plain lower-case words, with British spellings folded.

    Parenthetical qualifiers go first — "Asthma (moderate persistent)" is asthma — then
    punctuation, so "h/o" and "type-2" become word sequences rather than opaque blobs.

    Memoised, with the module's other pure text rewrites: ``active_flags`` re-evaluates every
    current medication against one chart, so the same handful of condition names is normalised
    once per (medication x rule) pair — the same strings, through the same regexes, every time.
    Keyed on the raw name, and the whole module is free of patient state beyond its arguments,
    so the cache holds condition *names* and never anything tying one to a patient.
    """
    text = _PARENTHETICAL.sub(" ", _norm(name))
    text = _NON_ALPHANUMERIC.sub(" ", text).strip()
    return tuple(_SPELLING_VARIANTS.get(word, word) for word in text.split())


@lru_cache(maxsize=2048)
def _condition_concept(name: str | None) -> frozenset[str]:
    """The set of words that identify a condition, after synonym rewriting.

    Comparing sets rather than strings is what makes one synonym entry cover every wording built
    around it, and what makes word order irrelevant. Everything here is a deterministic rewrite —
    no scoring, no similarity threshold, nothing that can match two different conditions to each
    other merely by resembling one another.
    """
    tokens = frozenset(_condition_words(name)) - _CONDITION_STOPWORDS
    for key, value in _ALIAS_REWRITES:
        if key and key <= tokens:
            tokens = (tokens - key) | value
    return tokens


def _condition_is_unreadable(condition: PatientCondition) -> bool:
    """True when nothing about this condition row can be compared to a contraindication rule.

    Matching happens on two axes and this asks whether *both* are empty. ``_condition_words``
    keeps only ``[a-z0-9]``, so a condition written in Devanagari, Bengali or any other non-Latin
    script tokenises to the empty tuple — as does an OCR blob ("‡‡‡"), a bare "???" and a row
    that is only punctuation. An empty token set satisfies none of the three comparisons in
    ``_match_condition``: it is not equal to a rule's tokens, it is not a superset, and the
    ``tokens and`` guard on the subset branch stops it being read as "less specific than
    everything". So the row falls through ``continue`` and every contraindication rule on the
    chart quietly has nothing to say about it.

    An ICD-10 code rescues the row completely — ``_icd10_matches`` never looks at the name — so a
    condition carrying a usable code is evaluated whatever script its label is in, and is not
    reported here.
    """
    if _condition_concept(condition.condition_name):
        return False
    return len(_NON_ALPHANUMERIC.sub("", (condition.icd10_code or "").lower())) < 3


def _icd10_matches(a: str | None, b: str | None) -> bool:
    """True when two ICD-10 codes name the same condition, at whatever depth each was coded.

    Charts code to whatever precision the source used, so a rule written against the J45 asthma
    category has to match a chart carrying J45.909. Prefix containment on the dotless form is
    that rollup; the three-character floor keeps a whole chapter from matching a category.
    """
    x = _NON_ALPHANUMERIC.sub("", (a or "").lower())
    y = _NON_ALPHANUMERIC.sub("", (b or "").lower())
    if len(x) < 3 or len(y) < 3:
        return False
    return x.startswith(y) or y.startswith(x)


def _match_condition(
    rule: ContraindicationRule, conditions: list[PatientCondition]
) -> _ConditionMatch | None:
    """The charted condition this rule fires against, or None.

    Returns the strongest match on the chart: an established one is preferred over a hedged or
    less-specific one, so a chart carrying both "h/o asthma" and "Asthma" blocks rather than
    warns.
    """
    rule_tokens = _condition_concept(rule.condition_name)
    best: _ConditionMatch | None = None

    for condition in conditions:
        normalised = _norm(condition.condition_name)
        words = set(_condition_words(condition.condition_name))
        if any(phrase in normalised for phrase in _CONDITION_ABSENT_PHRASES) or (
            words & _CONDITION_ABSENT_TOKENS
        ):
            continue

        tokens = _condition_concept(condition.condition_name)

        if _icd10_matches(rule.icd10_code, condition.icd10_code):
            basis = "icd10"
        elif tokens and tokens == rule_tokens:
            # The same condition, verbatim or after the rewrites in _condition_concept.
            basis = "name"
        elif rule_tokens and tokens > rule_tokens:
            # The chart is the rule's condition plus detail ("Ectopic Pregnancy" for a rule on
            # "Pregnancy"), so the patient has the rule's condition.
            basis = "more_specific"
        elif tokens and rule_tokens > tokens:
            # The chart is less specific than the rule. Not evidence the patient meets it.
            basis = "less_specific"
        else:
            continue

        hedged = any(marker in normalised for marker in _CONDITION_UNCERTAIN)
        is_present = basis != "less_specific" and not hedged
        if hedged and basis != "less_specific":
            basis = f"{basis}_hedged"

        match = _ConditionMatch(
            charted_name=condition.condition_name, basis=basis, is_present=is_present
        )
        if is_present:
            return match
        if best is None:
            best = match
    return best


def _near_miss_flag(
    ing: _Ingredient, rule: ContraindicationRule, match: _ConditionMatch
) -> SafetyFlag:
    """Say that a contraindication rule was reached and not applied, and why.

    This is the residue: a condition on the chart that is related to a curated rule for this
    drug but does not establish that the patient meets it. Enforcing the block here would assert
    something the record does not say. Dropping it silently is the failure this whole section
    exists to remove — it is the difference between "checked, and fine" and "there is something
    here I could not decide", and only the clinician can close that gap by reading the chart.

    Never a hard block, whatever the underlying rule's severity. The rule's own severity is
    carried in the details so the clinician can see what it would have been.
    """
    would_block = rule.is_absolute or rule.severity == "absolute"
    return SafetyFlag(
        check_type="contraindication",
        severity="warning" if would_block else "info",
        is_hard_block=False,
        summary=(
            f"“{match.charted_name}” on this chart may be the {rule.condition_name} that "
            f"{ing.label} is contraindicated in ({rule.description}), but the record "
            "does not establish it"
            + (
                ", so the hard block was not applied. Confirm the diagnosis if it applies."
                if would_block
                else ". Confirm the diagnosis if it applies."
            )
        ),
        details={
            **ing.details(),
            "condition": rule.condition_name,
            "charted_condition": match.charted_name,
            "match_basis": match.basis,
            "would_hard_block_if_confirmed": would_block,
            "rule_severity": rule.severity,
            "evaluated": False,
        },
        contraindication_id=rule.contraindication_id,
    )


def check_contraindications(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    """Curated contraindications for the product and for every molecule it contains.

    The ingredient loop is what makes a combination product reachable at all: the metformin
    renal rules are keyed on metformin's reference id, and a chart prescribing Glycomet GP at an
    eGFR of 20 matched none of them and came back with ``is_blocked: false``.
    """
    flags: list[SafetyFlag] = []
    for ing in _ingredients(proposed):
        for rule in ctx.contraindication_rules:
            if rule.drug_reference_id != ing.drug.reference_id:
                continue

            # --- Measured-threshold evaluation (works even if the named condition is absent,
            #     because eGFR and the liver panel are measured values). ---
            renal_flag = _evaluate_renal(ing, rule, ctx)
            if renal_flag is not None:
                flags.append(renal_flag)
                continue

            hepatic_flag = _evaluate_hepatic(ing, rule, ctx)
            if hepatic_flag is not None:
                flags.append(hepatic_flag)
                continue

            match = _match_condition(rule, ctx.conditions)
            if match is None:
                continue

            # Related, but not established as this patient's current problem — the chart is less
            # specific than the rule ("Renal Impairment" against a rule for *Severe* renal
            # impairment), or it is hedged or historical ("suspected", "h/o"). Applying the block
            # would assert something the chart does not say; staying silent would hide the one
            # condition on the chart that bears on this drug. See ``_near_miss_flag``.
            if not match.is_present:
                flags.append(_near_miss_flag(ing, rule, match))
                continue

            if rule.is_absolute or rule.severity == "absolute":
                flags.append(
                    SafetyFlag(
                        check_type="contraindication",
                        severity="hard_block",
                        is_hard_block=True,
                        summary=(
                            f"{ing.label} is contraindicated in "
                            f"{rule.condition_name}: {rule.description}. This is a hard block."
                        ),
                        details={
                            **ing.details(),
                            "condition": rule.condition_name,
                            "is_absolute": True,
                            # What the chart actually says, and why it was taken to be the
                            # rule's condition. The two differ whenever the block fired on
                            # anything but a verbatim wording, and a block is exactly the record
                            # that has to be answerable months later.
                            "charted_condition": match.charted_name,
                            "match_basis": match.basis,
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
                            f"Caution: {ing.label} with {rule.condition_name}: {rule.description}"
                        ),
                        details={
                            **ing.details(),
                            "condition": rule.condition_name,
                            "severity": rule.severity,
                            "charted_condition": match.charted_name,
                            "match_basis": match.basis,
                        },
                        contraindication_id=rule.contraindication_id,
                    )
                )
    return flags


def _evaluate_renal(
    ing: _Ingredient, rule: ContraindicationRule, ctx: SafetyContext
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
                f"Renal check not performed for {ing.label} "
                f"({rule.condition_name}): no eGFR on this chart. Guidelines set a threshold of "
                f"{below} mL/min (action: {action}); a current creatinine is needed to apply it."
            ),
            details={
                **ing.details(),
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
            f"{ing.label} ({rule.condition_name}). Recommended action: {action}."
        ),
        details={
            **ing.details(),
            "egfr": ctx.egfr,
            "egfr_threshold": below,
            "action": action,
            "condition": rule.condition_name,
        },
        contraindication_id=rule.contraindication_id,
    )


# The markers a hepatic threshold may be written against: the key a curated rule uses, the
# field it reads on ``HepaticPanel``, and how to say it. Two, because two are what this record
# reliably carries — see ``HepaticPanel`` for why this is not a Child-Pugh class.
_HEPATIC_MARKERS: tuple[tuple[str, str, str, str], ...] = (
    ("bilirubin_above", "bilirubin_mg_dl", "total bilirubin", "mg/dL"),
    ("alt_above", "alt_u_l", "ALT", "U/L"),
)


def _evaluate_hepatic(
    ing: _Ingredient, rule: ContraindicationRule, ctx: SafetyContext
) -> SafetyFlag | None:
    """Apply a curated hepatic dose-adjustment threshold to the chart's liver panel.

    ``hepatic_threshold`` has been a column on the contraindication table, a documented field in
    the curated-data schema, and a value this engine loads into ``ContraindicationRule`` since
    the table was written — and nothing read it. ``hepatic_dose`` was likewise a permitted
    ``check_type`` in the database constraint, in the shared enums and in this module's own
    ``CheckType``, produced by nothing. A rule keyed on a hepatic threshold therefore loaded,
    matched no condition name, and fell out of the loop reporting nothing: the drug came back
    with a clean check because the only rule that bore on it was written on an axis with no
    evaluator. The same "a check that did not run reporting itself as a check that passed"
    this file has now been corrected for four times over.

    Mirrors ``_evaluate_renal`` deliberately, including the part that matters most: when the
    threshold exists and there is nothing to apply it to, that is stated rather than passed
    over. Prescribing methotrexate to a chart with no LFTs on it is precisely when a clinician
    wants to be told there are no LFTs on it.
    """
    threshold = rule.hepatic_threshold
    if not threshold:
        return None

    stated = [
        (key, field_name, label, unit)
        for key, field_name, label, unit in _HEPATIC_MARKERS
        if threshold.get(key) is not None
    ]
    if not stated:
        return None

    action = threshold.get("action", "review")

    # Not "is the panel empty" but "does the chart measure anything *this rule* is written on".
    # The panel carries five values now (the two dose-adjustment markers plus the Child-Pugh and
    # MELD inputs), so a chart with an albumin and an INR and no bilirubin is a non-empty panel
    # against which a bilirubin-keyed rule still cannot be evaluated — and the loop below would
    # find nothing breached and return silence, which is this file's recurring bug rather than
    # an answer. Asking the narrower question makes the unevaluated branch exact.
    applicable = [
        (key, field_name, label, unit)
        for key, field_name, label, unit in stated
        if getattr(ctx.hepatic, field_name) is not None
    ]

    if not applicable:
        # Same judgement as the renal branch: unevaluated is not the same as fine, and a warning
        # is the honest answer where a block would be an unclearable alert on every chart
        # without an LFT.
        wanted = ", ".join(
            f"{label} above {threshold[key]} {unit}" for key, _f, label, unit in stated
        )
        needed = " or ".join(label for _k, _f, label, _u in stated)
        return SafetyFlag(
            check_type="hepatic_dose",
            severity="warning",
            is_hard_block=False,
            summary=(
                f"Hepatic check not performed for {ing.label} "
                f"({rule.condition_name}): no liver function tests on this chart. Guidelines "
                f"set a threshold of {wanted} (action: {action}); a current {needed} is "
                "needed to apply it."
            ),
            details={
                **ing.details(),
                "hepatic_thresholds": {key: threshold[key] for key, _f, _l, _u in stated},
                "action": action,
                "condition": rule.condition_name,
                "evaluated": False,
            },
            contraindication_id=rule.contraindication_id,
        )

    # Every marker the rule names and the chart measures. A rule naming two is breached by
    # either — the thresholds are alternative pieces of evidence for one impairment, not
    # conditions to be met together — and a marker the chart does not carry cannot breach.
    breached = [
        (label, getattr(ctx.hepatic, field_name), threshold[key], unit)
        for key, field_name, label, unit in applicable
        if getattr(ctx.hepatic, field_name) > threshold[key]
    ]
    if not breached:
        return None

    is_hard = action == "contraindicated"
    measured = "; ".join(
        f"{label} {value} {unit} (threshold {limit})" for label, value, limit, unit in breached
    )
    return SafetyFlag(
        check_type="hepatic_dose",
        severity="hard_block" if is_hard else "warning",
        is_hard_block=is_hard,
        summary=(
            f"Hepatic alert for {ing.label} ({rule.condition_name}): {measured}. "
            f"Recommended action: {action}."
        ),
        details={
            **ing.details(),
            "breached": [
                {"marker": label, "value": value, "threshold": limit, "unit": unit}
                for label, value, limit, unit in breached
            ],
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

    Matched ingredient-by-ingredient, which is where the duplication a combination product
    causes actually lives. Telma 40 alongside Telma H is a doubled telmisartan dose and one of
    the commonest real prescribing errors here; comparing the two products' own names and
    classes — "Telmisartan" against "Telmisartan + HCTZ", "ARB" against "ARB + Thiazide" — found
    nothing in common and reported them as two unrelated drugs.
    """
    flags: list[SafetyFlag] = []
    proposed_ingredients = _ingredients(proposed)

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

        med_ingredients = _ingredients(med)
        shared = next(
            (
                (ing, med_ing)
                for ing in proposed_ingredients
                for med_ing in med_ingredients
                # The reference id must be truthy on both sides: an ingredient with no
                # standalone vocabulary row carries an empty one, and two *different* such
                # molecules (Augmentin's clavulanic acid, Septran's trimethoprim) would
                # otherwise compare equal and be reported as the same active ingredient.
                if (ing.drug.reference_id and ing.drug.reference_id == med_ing.drug.reference_id)
                or (
                    _norm(ing.drug.generic_name)
                    and _norm(ing.drug.generic_name) == _norm(med_ing.drug.generic_name)
                )
            ),
            None,
        )
        if shared is not None:
            ing, med_ing = shared
            flags.append(
                SafetyFlag(
                    check_type="duplicate_therapy",
                    severity="critical",
                    is_hard_block=False,
                    summary=(
                        f"{proposed.generic_name} has the same active ingredient "
                        f"({ing.drug.generic_name}) as a different product the patient is "
                        f"already on ({med.generic_name}). Risk of unintentional double-dosing."
                    ),
                    details={
                        "proposed_drug": proposed.generic_name,
                        "existing_drug": med.generic_name,
                        "shared_ingredient": ing.drug.generic_name,
                        "match_type": "same_ingredient",
                    },
                )
            )
            continue

        shared_class = next(
            (
                (ing, med_ing)
                for ing in proposed_ingredients
                for med_ing in med_ingredients
                if ing.drug.drug_class
                and med_ing.drug.drug_class
                and _norm(ing.drug.drug_class) == _norm(med_ing.drug.drug_class)
            ),
            None,
        )
        if shared_class is not None:
            ing, med_ing = shared_class
            flags.append(
                SafetyFlag(
                    check_type="duplicate_therapy",
                    severity="warning",
                    is_hard_block=False,
                    summary=(
                        f"Therapeutic duplication: {ing.label} is in the same "
                        f"class ({ing.drug.drug_class}) as {med_ing.label}, which the "
                        "patient is already on."
                    ),
                    details={
                        "proposed_drug": proposed.generic_name,
                        "existing_drug": med.generic_name,
                        "drug_class": ing.drug.drug_class,
                        "match_type": "same_class",
                        **({"component": ing.drug.generic_name} if ing.is_component else {}),
                        **(
                            {"existing_component": med_ing.drug.generic_name}
                            if med_ing.is_component
                            else {}
                        ),
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


def check_unevaluated_allergies(ctx: SafetyContext) -> list[SafetyFlag]:
    """Say so when a documented allergy could not be identified, instead of ignoring it.

    ``check_allergies`` fires on three things: the allergy's resolved reference id, its resolved
    drug class, and its raw name compared to the proposed drug's generic name. A documented drug
    allergy the vocabulary cannot identify — an Indian brand the seed data has not been given, a
    combination product, an OCR'd scrawl off a referral letter — has neither of the first two, so
    all that is left is the third, and the third only fires when the chart happens to have
    written the INN.

    Which means a chart carrying "allergic to Calpol 650" and a proposal of paracetamol produces
    exactly the same empty flag list as a chart carrying no allergies at all. That is Critical
    Safety Rule #3, the one rule in this file that admits no override without documented
    reasoning, failing open on the sole ground that nobody has seeded the brand — and it is
    precisely the case CLAUDE.md pitfall #4 names ("a patient's allergy to Crocin must match
    against Paracetamol").

    Nothing here can close that gap by being cleverer: matching an unknown name to a drug is a
    guess, and a guess that manufactures a hard block is its own harm. What can be closed is the
    silence. This is the same judgement ``check_unevaluated_medications`` makes for the other
    half of the chart, applied to the higher-stakes half.

    A warning rather than a hard block, for that function's reason: blocking every prescription
    on every chart with one unrecognised allergen would teach clinicians to click through the
    real blocks. What the clinician needs is to be told which allergy was not cross-checked.
    """
    if not ctx.unresolved_allergies:
        return []
    names = sorted({name.strip() for name in ctx.unresolved_allergies if name.strip()})
    if not names:
        return []
    listed = ", ".join(f"“{name}”" for name in names)
    plural = len(names) != 1
    return [
        SafetyFlag(
            check_type="unevaluated_allergy",
            severity="warning",
            is_hard_block=False,
            summary=(
                f"{len(names)} documented allerg{'ies' if plural else 'y'} on this chart "
                f"could not be matched to a known drug ({listed}), so "
                f"{'they were' if plural else 'it was'} cross-checked only against an exact "
                "generic-name match — no brand, ingredient or drug-class check was performed "
                f"against {'them' if plural else 'it'}. This is not the same as “no allergy "
                "conflict”. Confirm what the allergen is, or ask for it to be added to the drug "
                "vocabulary."
            ),
            details={
                "unresolved_allergies": names,
                "evaluated": False,
            },
        )
    ]


# Curated liver-injury tiers, worst first. 'dose_dependent' is intrinsic toxicity — the injury
# is predictable from exposure, which is why paracetamol and methotrexate are here and why the
# ceiling matters more than the idiosyncrasy. 'established' is a well-documented idiosyncratic
# signal in the published DILI registries.
_HEPATOTOXICITY_TIERS: tuple[str, ...] = ("dose_dependent", "established")


def _is_hepatotoxic(drug: DrugRef) -> bool:
    return (drug.hepatotoxicity or "") in _HEPATOTOXICITY_TIERS


def check_hepatotoxic_burden(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    """Flag adding a hepatotoxic drug to a liver that is already carrying some.

    Two things fall between the checks that exist. The curated ``hepatic_threshold`` rules are
    per-drug and are written against a measured panel, so they say nothing about a drug with a
    documented liver-injury signal and no threshold rule — amoxicillin-clavulanate is the
    commonest cause of drug-induced liver injury in the published registries and this vocabulary
    held no hepatic rule for it at all. And the interaction table is pairwise on curated pairs, so
    three separately unremarkable hepatotoxic drugs on one chart produce three empty checks; the
    burden is a property of the *set*, which is the same reason ``check_duplicate_therapy`` had to
    exist alongside the interaction rules.

    Fires only when the proposed drug is itself curated as hepatotoxic — a chart full of
    hepatotoxic drugs is not a reason to flag an unrelated antihypertensive — and then only when
    the chart adds something: another hepatotoxic drug already active, or a liver the panel shows
    to be impaired.

    An uncurated drug (``hepatotoxicity is None``) contributes nothing and is never counted as
    clean. Fifty seeded drugs is not a pharmacopoeia, and a check that read "no other hepatotoxic
    drugs on this chart" off an incomplete table would be asserting something the table cannot
    support. Silence here means the question was not answered, which is why the summary says so.

    Never a hard block. Co-prescribing hepatotoxic drugs is routine and often correct — a
    diabetic on a statin who needs a course of co-amoxiclav is not a prescribing error — and the
    decision needs the clinician, not a refusal.
    """
    if not _is_hepatotoxic(proposed):
        return []

    concurrent = sorted(
        {
            med.generic_name
            for med in ctx.current_meds
            if _is_hepatotoxic(med) and med.reference_id != proposed.reference_id
        }
    )
    severity_view = hepatic_severity(ctx)
    child_pugh = severity_view.child_pugh
    impaired = child_pugh is not None and child_pugh.min_class != "A"

    if not concurrent and not impaired:
        return []

    reasons: list[str] = []
    if concurrent:
        listed = ", ".join(concurrent)
        reasons.append(
            f"the chart already carries {len(concurrent)} other medication(s) with a documented "
            f"liver-injury signal ({listed})"
        )
    if impaired and child_pugh is not None:
        span = (
            f"Child-Pugh {child_pugh.child_pugh_class}"
            if child_pugh.child_pugh_class
            else f"Child-Pugh {child_pugh.min_class} to {child_pugh.max_class}"
        )
        reasons.append(f"this chart's liver panel scores {span}")

    # Conservative wins, per Critical Safety Rule #2: an impaired liver outranks a count of
    # co-prescriptions, and both together do not exceed the more serious of the two.
    is_critical = impaired and (child_pugh is not None and child_pugh.child_pugh_class == "C")
    return [
        SafetyFlag(
            check_type="hepatotoxic_burden",
            severity="critical" if is_critical else "warning",
            is_hard_block=False,
            summary=(
                f"{proposed.generic_name} has a documented liver-injury signal "
                f"({proposed.hepatotoxicity.replace('_', ' ') if proposed.hepatotoxicity else ''}"
                f") and {' and '.join(reasons)}. Guidelines support checking liver function "
                "before and during the course, and considering an alternative where one exists. "
                "Note that this is scored only against the drugs this vocabulary curates: "
                "medications it does not carry a liver-injury tier for were not counted either "
                "way."
            ),
            details={
                "proposed_drug": proposed.generic_name,
                "proposed_hepatotoxicity": proposed.hepatotoxicity,
                "concurrent_hepatotoxic_drugs": concurrent,
                "hepatic_impairment": impaired,
                **(child_pugh.as_details() if child_pugh is not None else {}),
            },
        )
    ]


# --- Cumulative bleeding risk --------------------------------------------------------------------


@dataclass(frozen=True)
class _BleedingMechanism:
    """How a drug class raises bleeding risk, and whether it does so on its own."""

    label: str
    # True for a class that does not itself impair haemostasis but makes another agent's bleed
    # more likely or more severe — a corticosteroid does not thin the blood, and a corticosteroid
    # on top of an NSAID multiplies the risk of an upper-GI bleed. A set consisting only of
    # potentiators is not a bleeding-risk set, which is what ``potentiator_only`` is read for.
    potentiator_only: bool = False


_ANTICOAGULATION = "anticoagulation"

# Bleeding mechanism by drug CLASS, lower-cased.
#
# Keyed on class rather than on a curated per-drug column, deliberately. Bleeding risk here is a
# property of the mechanism the class names — every antiplatelet inhibits platelets — so a
# per-row column would be a curation surface with nothing to check it against, which is exactly
# how the interaction table came to cover one strength of aspirin and not the other. A class is
# already curated once per row and is already what ``check_duplicate_therapy`` and the
# cross-reactivity table are written against.
#
# Classes with no standalone row in this vocabulary (DOACs, heparins, SSRIs) are listed anyway:
# they are the drugs a real formulary adds next, and an absent key fails silent.
_BLEEDING_MECHANISM_CLASSES: dict[str, _BleedingMechanism] = {
    "vitamin k antagonist": _BleedingMechanism(_ANTICOAGULATION),
    "anticoagulant": _BleedingMechanism(_ANTICOAGULATION),
    "direct oral anticoagulant": _BleedingMechanism(_ANTICOAGULATION),
    "heparin": _BleedingMechanism(_ANTICOAGULATION),
    "low molecular weight heparin": _BleedingMechanism(_ANTICOAGULATION),
    "antiplatelet": _BleedingMechanism("platelet inhibition"),
    "salicylate": _BleedingMechanism("platelet inhibition"),
    "nsaid": _BleedingMechanism("platelet inhibition with GI mucosal injury"),
    "ssri": _BleedingMechanism("platelet inhibition"),
    "snri": _BleedingMechanism("platelet inhibition"),
    "corticosteroid": _BleedingMechanism("GI mucosal injury", potentiator_only=True),
}


def _bleeding_agents(drug: DrugRef) -> dict[str, _BleedingMechanism]:
    """``{generic name: mechanism}`` for every identity of this product that can bleed a patient.

    Ingredient-by-ingredient, because the combination is where this is least visible. An
    aspirin+clopidogrel fixed-dose tablet — a very widely prescribed product in this market — has
    a product class naming neither molecule, so a check written against the product alone reads
    one tablet where the patient is taking two antiplatelets.
    """
    agents: dict[str, _BleedingMechanism] = {}
    for ing in _ingredients(drug):
        mechanism = _BLEEDING_MECHANISM_CLASSES.get(_norm(ing.drug.drug_class))
        if mechanism is not None:
            agents.setdefault(ing.drug.generic_name, mechanism)
    return agents


def check_bleeding_burden(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    """Flag the *set* of bleeding-risk drugs on a chart, which no pairwise rule expresses.

    ``check_interactions`` grades one curated pair at a time, and that is the wrong shape for the
    commonest dangerous polypharmacy there is. A patient on warfarin, aspirin, clopidogrel and
    diclofenac has six pairs; the curated table carries three of them, each graded "major", and
    the screen shows three findings of the same weight a single warfarin+aspirin chart would
    show. Nothing anywhere says the patient is on four agents that each independently impair
    haemostasis — which is the clinical picture, and is worse than any pair in it.

    The gaps are not an oversight in the table so much as a consequence of its shape. Some pairs
    are deliberately absent: dual antiplatelet therapy is prescribed on purpose after a stent, and
    curating aspirin+clopidogrel as an interaction would fire an amber card on correct treatment.
    It is the *third* and fourth agent that changes the picture, and a pairwise table has no way
    to say "this pair is fine, and these four together are not".

    So this is the same argument ``check_duplicate_therapy`` and ``check_hepatotoxic_burden``
    already make: the burden is a property of the set, and a table of pairs cannot carry it.

    Fires only when the proposed drug itself raises bleeding risk — a chart full of
    anticoagulants is not a reason to flag a levothyroxine — and only when the chart adds at
    least one further such drug. A drug whose class this table does not carry contributes
    nothing and is never counted as safe, for the same reason a ``None`` hepatotoxicity is not
    counted as clean: fifty seeded drugs are not a formulary.

    Never a hard block. Triple therapy after a stent in a patient with atrial fibrillation is a
    real, guideline-supported prescription; refusing it would be wrong, and the decision needs
    the clinician.
    """
    proposed_agents = _bleeding_agents(proposed)
    if not proposed_agents:
        return []

    concurrent: dict[str, _BleedingMechanism] = {}
    for med in ctx.current_meds:
        if med.reference_id and med.reference_id == proposed.reference_id:
            continue
        for name, mechanism in _bleeding_agents(med).items():
            # A molecule already counted on the proposed product is the same molecule, not a
            # second agent — re-ordering aspirin for a patient on aspirin is a duplicate, which
            # ``check_duplicate_therapy`` reports, and doubling it here would turn one drug into
            # a two-agent bleeding set.
            if name not in proposed_agents:
                concurrent.setdefault(name, mechanism)
    if not concurrent:
        return []

    combined = {**proposed_agents, **concurrent}
    # Agents that impair haemostasis in their own right. A chart carrying only potentiators —
    # prednisolone beside another steroid — is not a bleeding-risk set at all.
    haemostatic = sorted(name for name, m in combined.items() if not m.potentiator_only)
    if not haemostatic:
        return []

    mechanisms = sorted({m.label for m in combined.values()})
    anticoagulated = any(m.label == _ANTICOAGULATION for m in combined.values())
    # Conservative per Critical Safety Rule #2, and graded on the two things the bleeding
    # literature actually separates: how many agents, and whether one of them is an
    # anticoagulant. An anticoagulant plus any second haemostatic agent is the combination that
    # fills medical wards; three such agents is triple therapy however it was arrived at.
    is_critical = len(haemostatic) >= 3 or (anticoagulated and len(haemostatic) >= 2)

    proposed_label = ", ".join(sorted({m.label for m in proposed_agents.values()}))
    listed = ", ".join(sorted(concurrent))
    return [
        SafetyFlag(
            check_type="bleeding_burden",
            severity="critical" if is_critical else "warning",
            is_hard_block=False,
            summary=(
                f"{proposed.generic_name} raises bleeding risk ({proposed_label}), and this "
                f"chart carries {len(concurrent)} other medication(s) that also do ({listed}) — "
                f"{len(haemostatic)} agents impairing haemostasis across "
                f"{len(mechanisms)} mechanism(s) ({', '.join(mechanisms)}). Interaction rules "
                "grade one pair at a time, so a set like this is reported as several separate "
                "findings each carrying the weight of a single pair, and some pairs in it may "
                "carry no curated rule at all. Guidelines support reviewing whether each agent "
                "is still indicated, and considering gastroprotection where the combination is "
                "intended. Scored only against the drugs whose class this vocabulary curates: "
                "medications it carries no bleeding-risk class for were not counted either way."
            ),
            details={
                "proposed_drug": proposed.generic_name,
                "proposed_bleeding_mechanisms": sorted({m.label for m in proposed_agents.values()}),
                "concurrent_bleeding_drugs": sorted(concurrent),
                "bleeding_mechanisms": mechanisms,
                "haemostatic_agents": haemostatic,
                "haemostatic_agent_count": len(haemostatic),
                "includes_anticoagulant": anticoagulated,
            },
        )
    ]


# --- Age-based prescribing cautions ---------------------------------------------------------------


@dataclass(frozen=True)
class _GeriatricCaution:
    """A curated age-based prescribing caution for a drug class."""

    min_age_years: int
    concern: str
    # Prescriber-framed, per Critical Safety Rule #4 — a consideration, never an instruction.
    consideration: str
    reference: str


# The engine's only age threshold besides CKD-EPI's paediatric floor. 65 is where the published
# geriatric prescribing criteria are written, and each entry restates it so a criterion written
# against a different age can carry its own.
_GERIATRIC_AGE_YEARS = 65

# Bleeding risk, renal clearance and hypoglycaemia awareness all change with age, and none of the
# checks above can see that: an interaction rule is a pair of drugs, a contraindication rule is a
# drug and a charted condition, and being 82 is neither. So a chart carrying glimepiride and
# digoxin — two of the drugs the published criteria name most often — produced nothing whatever
# about the patient's age.
#
# Keyed on drug CLASS, for the reason the bleeding table is: a per-strength key is the curation
# surface that already drifted between the two aspirin strengths, and the digoxin caution below
# is precisely the kind of dose-specific entry that would have been curated against one row.
#
# Every entry is a *caution*, never a block and never critical. Each of these drugs is correctly
# prescribed to older patients every day; what the criteria say is that the decision deserves a
# second look, which is a sentence on the screen, not a refusal.
_GERIATRIC_CAUTIONS: dict[str, _GeriatricCaution] = {
    "sulfonylurea": _GeriatricCaution(
        min_age_years=_GERIATRIC_AGE_YEARS,
        concern=(
            "sulfonylureas cause prolonged hypoglycaemia in older adults, which presents as "
            "confusion or a fall rather than as a recognised hypo"
        ),
        consideration=(
            "Guidelines support considering an agent with a lower hypoglycaemia risk where one "
            "is suitable, and reviewing whether the glycaemic target is still appropriate for "
            "this patient's age and comorbidity."
        ),
        reference="AGS Beers Criteria 2023 — sulfonylureas in older adults",
    ),
    "nsaid": _GeriatricCaution(
        min_age_years=_GERIATRIC_AGE_YEARS,
        concern=(
            "the risk of GI bleeding, acute kidney injury and fluid retention from an NSAID "
            "rises sharply with age, and rises further alongside an anticoagulant, an "
            "antiplatelet or a corticosteroid"
        ),
        consideration=(
            "Guidelines support considering paracetamol or a topical NSAID first, and where an "
            "oral NSAID is used, the shortest course and gastroprotection."
        ),
        reference="AGS Beers Criteria 2023 — NSAIDs in older adults",
    ),
    "cardiac glycoside": _GeriatricCaution(
        min_age_years=_GERIATRIC_AGE_YEARS,
        concern=(
            "digoxin clearance falls with age and with renal function, and the published "
            "criteria caution against a total daily dose above 0.125 mg in older adults. This "
            "record carries the product's tablet strength but no structured daily dose, so the "
            "dose actually taken was not compared against that threshold"
        ),
        consideration=(
            "Guidelines support confirming the total daily dose and considering a digoxin level "
            "alongside renal function and potassium."
        ),
        reference="AGS Beers Criteria 2023 — digoxin dosing in older adults",
    ),
    "ppi": _GeriatricCaution(
        min_age_years=_GERIATRIC_AGE_YEARS,
        concern=(
            "scheduled PPI use beyond about eight weeks in older adults is associated with "
            "C. difficile infection, bone loss and fracture. This record carries no structured "
            "treatment duration, so how long this course has run was not evaluated"
        ),
        consideration=(
            "Guidelines support reviewing whether a continuing indication is documented, and "
            "considering step-down or on-demand use where it is not."
        ),
        reference="AGS Beers Criteria 2023 — proton pump inhibitors in older adults",
    ),
}


def _geriatric_cautions(drug: DrugRef) -> list[tuple[_Ingredient, _GeriatricCaution]]:
    """Every curated age-based caution this product's identities carry, in a stable order.

    Ingredient-by-ingredient, because the combination is where an age-based caution disappears:
    Glycomet GP is one of the most prescribed diabetes products in this market and its product
    class is "Biguanide + Sulfonylurea", which is not "Sulfonylurea" and matches nothing. Its
    glimepiride component is the whole reason the criteria name it.
    """
    seen: set[str] = set()
    out: list[tuple[_Ingredient, _GeriatricCaution]] = []
    for ing in _ingredients(drug):
        caution = _GERIATRIC_CAUTIONS.get(_norm(ing.drug.drug_class))
        if caution is None or caution.reference in seen:
            continue
        seen.add(caution.reference)
        out.append((ing, caution))
    return out


def check_geriatric_cautions(proposed: DrugRef, ctx: SafetyContext) -> list[SafetyFlag]:
    """Surface the published age-based prescribing cautions this drug carries.

    Nothing else in this engine can see the patient's age. An interaction rule is a pair of
    drugs; a contraindication rule is a drug and a charted condition; being 82 is neither. So the
    two drugs the geriatric criteria name most often — a sulfonylurea and digoxin — sat on an
    88-year-old's chart and produced no age-related finding at all.

    An unknown date of birth does not fall silent. It produces an informational flag saying the
    caution exists and was not evaluated, for exactly the reason ``check_unevaluated_medications``
    exists: this engine's recurring failure is reporting a comparison it could not attempt as a
    comparison that passed. The flag is raised only for drugs that actually carry a caution, so a
    record with no date of birth does not grow a notice under every medication on it.

    Never a hard block and never critical. Each of these drugs is correctly prescribed to older
    patients every day; the criteria say the decision deserves a second look, and a second look
    is a sentence on the screen rather than a refusal.
    """
    cautions = _geriatric_cautions(proposed)
    if not cautions:
        return []

    if ctx.age_years is None:
        return [
            SafetyFlag(
                check_type="geriatric_caution",
                severity="info",
                is_hard_block=False,
                summary=(
                    f"{ing.label} carries an age-based prescribing caution "
                    f"({caution.reference}), and this record has no usable date of birth — so "
                    "the caution was not evaluated for this patient. This is a check that did "
                    "not run, not a check that passed."
                ),
                details={
                    **ing.details(),
                    "evaluated": False,
                    "reason": "no_date_of_birth",
                    "min_age_years": caution.min_age_years,
                    "reference": caution.reference,
                },
            )
            for ing, caution in cautions
        ]

    return [
        SafetyFlag(
            check_type="geriatric_caution",
            severity="warning",
            is_hard_block=False,
            summary=(
                f"This patient is {ctx.age_years}, and at {caution.min_age_years} or over "
                f"{caution.concern}. {caution.consideration} ({caution.reference}.)"
            ),
            details={
                **ing.details(),
                "evaluated": True,
                "age_years": ctx.age_years,
                "min_age_years": caution.min_age_years,
                "drug_class": ing.drug.drug_class,
                "reference": caution.reference,
            },
        )
        for ing, caution in cautions
        if ctx.age_years >= caution.min_age_years
    ]


def check_unevaluated_conditions(ctx: SafetyContext) -> list[SafetyFlag]:
    """Say so when a charted condition could not be compared to any rule, rather than dropping it.

    The third row of the same table as ``check_unevaluated_medications`` and
    ``check_unevaluated_allergies``, and the one nobody had noticed. Condition matching is
    deliberately built out of deterministic text rewrites over ``[a-z0-9]`` tokens, which is what
    makes it defensible — no scoring, no similarity threshold, nothing that can match two
    different conditions to each other merely by resembling one another. The cost is that a
    condition name carrying no Latin alphanumerics at all tokenises to nothing, and a token set of
    nothing matches nothing. The row is skipped by every contraindication rule on the chart, and
    the response comes back with no flags.

    Which is the shape of answer this file has now been corrected for five times: a comparison
    that could not be attempted, reported as a comparison that passed. The concrete case is this
    product's own market — a problem list written in Devanagari, or lifted by OCR off a
    handwritten referral into "‡‡‡" — where the drug is proposed against a chart that says
    "गर्भावस्था" and the pregnancy hard block on ramipril never runs.

    Nothing here can close that by being cleverer. Translating a condition name is a guess, and a
    guess that manufactures a hard block is its own harm; the module's refusal to fuzzy-match
    condition names is the same judgement. What can be closed is the silence.

    A row carrying a usable ICD-10 code is not reported, because the code path matches without
    ever reading the name — that is the answer to a non-Latin chart, and it is worth the flag
    saying so.

    A warning rather than a hard block, for the same reason as its two siblings: blocking every
    prescription on every chart with one unreadable problem-list line teaches clinicians to click
    through the real blocks.
    """
    unreadable = sorted(
        {
            (condition.condition_name or "").strip()
            for condition in ctx.conditions
            if _condition_is_unreadable(condition)
        }
    )
    listed_names = [name for name in unreadable if name]
    # A row whose name is blank *and* unreadable is still a row nothing was checked against, so
    # it is counted; it just cannot be quoted back.
    count = len(unreadable)
    if not count:
        return []

    plural = count != 1
    quoted = ", ".join(f"“{name}”" for name in listed_names)
    return [
        SafetyFlag(
            check_type="unevaluated_condition",
            severity="warning",
            is_hard_block=False,
            summary=(
                f"{count} condition{'s' if plural else ''} on this chart could not be read as "
                f"text{f' ({quoted})' if quoted else ''} and carr{'y' if plural else 'ies'} no "
                "ICD-10 code, so no contraindication rule was evaluated against "
                f"{'them' if plural else 'it'} — this is not the same as “no contraindication”. "
                "A condition recorded in a non-Latin script or lifted unreadably by OCR is "
                "checked as soon as it carries an ICD-10 code, or is re-entered in English."
            ),
            details={
                "unevaluated_conditions": listed_names,
                "unevaluated_condition_count": count,
                "evaluated": False,
            },
        )
    ]


def hepatic_severity(ctx: SafetyContext) -> HepaticSeverity:
    """The Child-Pugh window and MELD this chart's liver panel supports. Never raises."""
    return assess_hepatic_severity(
        bilirubin_mg_dl=ctx.hepatic.bilirubin_mg_dl,
        albumin_g_dl=ctx.hepatic.albumin_g_dl,
        inr=ctx.hepatic.inr,
        creatinine_mg_dl=ctx.hepatic.creatinine_mg_dl,
    )


def check_hepatic_severity(ctx: SafetyContext) -> list[SafetyFlag]:
    """State how severe this chart's liver disease is, when the chart establishes it.

    A statement about the patient rather than about any one proposed drug, so it belongs with the
    other chart-level flags rather than repeated under every medication.

    Two things make this worth a flag at all rather than a number on a screen somewhere. The
    first is that the curated ``hepatic_threshold`` rules cover six drugs; a chart showing a
    decompensated liver bears on every drug that liver has to clear, including the forty-odd in
    this vocabulary that carry no hepatic rule and therefore produce no hepatic flag. The second
    is that "Child-Pugh B or C" is the phrase the dosing guidance for those drugs is written in,
    and a clinician who has that phrase can look the drug up; a clinician who has a bilirubin and
    an albumin has to do the arithmetic themselves, on a scoring system whose two remaining
    components they are the only one who can supply.

    Severity is graded by what the labs *establish*, not by the worst case they permit:

    * a determinate Child-Pugh C is a ``critical`` — every point of the ungraded bedside
      components still leaves this patient in the most impaired class;
    * anything else computable is ``info``. It is real information and it is shown, but a
      warning on every chart whose labs merely *permit* class C would fire on a mildly abnormal
      panel, which is how a clinician learns to dismiss the class-C ones.

    Never a hard block, in any case. A liver score is not a contraindication; the contraindication
    rules are, and they run on the same panel.
    """
    severity = hepatic_severity(ctx)
    if not severity:
        return []

    child_pugh = severity.child_pugh
    if child_pugh is not None and child_pugh.child_pugh_class == "C":
        summary = (
            f"This chart's liver panel scores {child_pugh.lab_points} of the 9 available "
            "Child-Pugh laboratory points, which places the patient in class C "
            f"({child_pugh.min_total}–{child_pugh.max_total} of 15) whatever the ascites and "
            "encephalopathy grading turns out to be. Guidelines for most hepatically cleared "
            "drugs treat class C as a reason to reduce the dose or avoid the drug — worth "
            "checking for anything prescribed here, including drugs this engine holds no "
            "hepatic rule for."
        )
        flag_severity: Severity = "critical"
    elif child_pugh is not None:
        summary = (
            f"This chart's liver panel scores {child_pugh.lab_points} of the 9 available "
            f"Child-Pugh laboratory points: Child-Pugh {child_pugh.min_class} to "
            f"{child_pugh.max_class} ({child_pugh.min_total}–{child_pugh.max_total} of 15), "
            "narrowing to one class only once ascites and encephalopathy are graded. This "
            "record holds neither, so the class is not asserted."
        )
        flag_severity = "info"
    else:
        missing = ", ".join(severity.missing_child_pugh)
        summary = (
            f"No Child-Pugh score could be computed for this chart — it carries no {missing}. "
        )
        flag_severity = "info"

    if severity.meld is not None:
        summary += (
            f" MELD is {severity.meld.score}, a severity and referral index rather than a "
            "dosing one."
        )

    return [
        SafetyFlag(
            check_type="hepatic_severity",
            severity=flag_severity,
            is_hard_block=False,
            summary=summary.strip(),
            details={
                **severity.as_details(),
                "measured": {
                    "bilirubin_mg_dl": ctx.hepatic.bilirubin_mg_dl,
                    "albumin_g_dl": ctx.hepatic.albumin_g_dl,
                    "inr": ctx.hepatic.inr,
                    "creatinine_mg_dl": ctx.hepatic.creatinine_mg_dl,
                },
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
    flags.extend(check_hepatotoxic_burden(proposed, ctx))
    flags.extend(check_bleeding_burden(proposed, ctx))
    flags.extend(check_geriatric_cautions(proposed, ctx))
    flags.extend(check_guideline_adherence(proposed, ctx))
    return flags


def has_hard_block(flags: list[SafetyFlag]) -> bool:
    return any(f.is_hard_block for f in flags)
