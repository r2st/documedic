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

import re
from dataclasses import dataclass, field
from functools import lru_cache
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
    "unevaluated_allergy",
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
    icd10_code: str | None = None


@dataclass(frozen=True)
class HepaticPanel:
    """The measured liver function a hepatic dose-adjustment rule can be applied to.

    Two markers, in the canonical units ``app.core.lab_safety`` normalises to: total bilirubin
    in mg/dL and ALT in U/L. Deliberately not a Child-Pugh class, which is what the hepatic
    dosing literature is actually written against — Child-Pugh needs ascites and encephalopathy
    graded by a clinician, neither of which is in this record as structured data, and inferring
    a class from labs alone would be exactly the confident-wrong number this engine refuses
    elsewhere. What is here is what the chart measures.
    """

    bilirubin_mg_dl: float | None = None
    alt_u_l: float | None = None

    def __bool__(self) -> bool:
        return self.bilirubin_mg_dl is not None or self.alt_u_l is not None


@dataclass(frozen=True)
class SafetyContext:
    """Everything the engine needs about a patient + reference data."""

    current_meds: list[DrugRef] = field(default_factory=list)
    allergies: list[PatientAllergy] = field(default_factory=list)
    conditions: list[PatientCondition] = field(default_factory=list)
    egfr: float | None = None
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
    # Pregnancy — the seeded ACE-inhibitor, statin, warfarin and methotrexate blocks.
    "pregnant": "pregnancy",
    "gravid": "pregnancy",
    "primigravida": "pregnancy",
    "multigravida": "pregnancy",
    "intrauterine pregnancy": "pregnancy",
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
    "melena": "active bleeding",
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
    proposed: DrugRef, rule: ContraindicationRule, match: _ConditionMatch
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
            f"{proposed.generic_name} is contraindicated in ({rule.description}), but the record "
            "does not establish it"
            + (
                ", so the hard block was not applied. Confirm the diagnosis if it applies."
                if would_block
                else ". Confirm the diagnosis if it applies."
            )
        ),
        details={
            "proposed_drug": proposed.generic_name,
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
    flags: list[SafetyFlag] = []
    for rule in ctx.contraindication_rules:
        if rule.drug_reference_id != proposed.reference_id:
            continue

        # --- Measured-threshold evaluation (works even if the named condition is absent,
        #     because eGFR and the liver panel are measured values). ---
        renal_flag = _evaluate_renal(proposed, rule, ctx)
        if renal_flag is not None:
            flags.append(renal_flag)
            continue

        hepatic_flag = _evaluate_hepatic(proposed, rule, ctx)
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
            flags.append(_near_miss_flag(proposed, rule, match))
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
                        # What the chart actually says, and why it was taken to be the rule's
                        # condition. The two differ whenever the block fired on anything but a
                        # verbatim wording, and a block is exactly the record that has to be
                        # answerable months later.
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
                        f"Caution: {proposed.generic_name} with {rule.condition_name}: "
                        f"{rule.description}"
                    ),
                    details={
                        "proposed_drug": proposed.generic_name,
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


# The markers a hepatic threshold may be written against: the key a curated rule uses, the
# field it reads on ``HepaticPanel``, and how to say it. Two, because two are what this record
# reliably carries — see ``HepaticPanel`` for why this is not a Child-Pugh class.
_HEPATIC_MARKERS: tuple[tuple[str, str, str, str], ...] = (
    ("bilirubin_above", "bilirubin_mg_dl", "total bilirubin", "mg/dL"),
    ("alt_above", "alt_u_l", "ALT", "U/L"),
)


def _evaluate_hepatic(
    proposed: DrugRef, rule: ContraindicationRule, ctx: SafetyContext
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

    if not ctx.hepatic:
        # No liver panel at all. Same judgement as the renal branch: unevaluated is not the
        # same as fine, and a warning is the honest answer where a block would be an
        # unclearable alert on every chart without an LFT.
        wanted = ", ".join(
            f"{label} above {threshold[key]} {unit}" for key, _f, label, unit in stated
        )
        return SafetyFlag(
            check_type="hepatic_dose",
            severity="warning",
            is_hard_block=False,
            summary=(
                f"Hepatic check not performed for {proposed.generic_name} "
                f"({rule.condition_name}): no liver function tests on this chart. Guidelines "
                f"set a threshold of {wanted} (action: {action}); a current bilirubin or ALT is "
                "needed to apply it."
            ),
            details={
                "proposed_drug": proposed.generic_name,
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
        for key, field_name, label, unit in stated
        if getattr(ctx.hepatic, field_name) is not None
        and getattr(ctx.hepatic, field_name) > threshold[key]
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
            f"Hepatic alert for {proposed.generic_name} ({rule.condition_name}): {measured}. "
            f"Recommended action: {action}."
        ),
        details={
            "proposed_drug": proposed.generic_name,
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
