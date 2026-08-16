"""Deterministic medication reconciliation across a transition of care. No LLM, no I/O.

``POST /patients/{id}/safety/check`` answers one question well: is *this* drug safe for *this*
patient, given what the chart already holds. Reconciliation is a different question, asked at
the moments where medication error actually concentrates — admission, discharge, transfer, a
patient arriving with a discharge summary from another hospital. The clinician has a *list*, and
three of the four things that go wrong with a list are invisible to any number of per-drug
checks:

* **Omission.** A drug on the chart that is absent from the new list. A per-drug check cannot
  see it, because there is no drug to pass in — the failure mode is a check that was never
  called, and no amount of calling it more carefully finds one that was not called at all. This
  is the classic reconciliation error and the reason the discipline exists.
* **Intra-list interaction.** Two drugs that are both new. Checked one at a time against the
  chart, each is clean: neither is charted yet, so the pair never meets. The first time they
  are in the same room is when both have been prescribed.
* **Intra-list duplication.** The same molecule, or the same therapeutic class, arriving twice
  in one list — routinely as a brand on one line and its INN on another, or as a combination
  product overlapping a single-ingredient one.

The fourth, per-drug safety against the patient, is not reimplemented here. ``app.core.safety``
already does it, and the service layer runs it for every proposed line; this module is only
about what is true of the *list*.

Two design rules the rest of this file follows:

**Matching is on identity, never on the string.** A proposed "Glycomet GP" and a charted
"Metformin + Glimepiride" are the same prescription; a proposed "Telma 40" alongside a charted
"Telma H" share a molecule and differ in another. Every comparison here goes through the
reference ids of a product's *ingredients* (``app.core.safety.ingredient_reference_ids``), which
is the same identity the hard-block engine matches on. String equality would report a brand
switch as a stop plus a start — the shape that hides a genuine omission in noise — and would
miss the doubled telmisartan entirely.

**An unreadable line is reported as unreadable, never dropped.** A proposed drug that resolves
to nothing, and a charted row that resolved to nothing, are each carried through to the output
as their own disposition. Silently omitting them would produce a reconciliation that claims to
have compared two lists while having compared subsets of them — the same fail-open shape this
codebase has closed repeatedly, and the more dangerous one here because a reconciliation report
reads as a statement that the lists now agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.core.safety import (
    DrugRef,
    InteractionRule,
    Severity,
    ingredient_reference_ids,
)

# What reconciliation concluded about one medication, from the pair of lists alone.
#
# These are statements about the two lists, not instructions. "stop" does not mean stop the
# drug; it means the new list does not carry a drug the chart calls current, and the clinician
# is the one who decides whether that is intended (Critical Safety Rule #4). The microcopy in
# ``summary`` is written accordingly, and ``tests/test_clinical_language_framing.py`` holds it
# to that.
Disposition = Literal[
    # On both lists, and the chart's dose and the proposed dose agree (or neither is stated).
    "continue",
    # On both lists with different doses. Separated from ``continue`` because it is the line a
    # clinician must actually read: a dose change is the intended act about half the time and a
    # transcription error the other half, and the two are indistinguishable from the lists.
    "dose_change",
    # Proposed, not currently charted.
    "start",
    # Charted as current, not proposed. The omission case.
    "stop",
    # A proposed line whose drug name matched nothing in the vocabulary. It was compared against
    # nothing and nothing was compared against it.
    "unresolved_proposed",
    # A charted current medication that matched nothing in the vocabulary, for the same reason.
    "unresolved_charted",
]

# Dispositions that mean "this line was never actually reconciled". Exported because the service
# and the response schema both need to say how much of the comparison did not happen, and a
# reconciliation summary that counts these among its matched lines overstates itself.
UNRESOLVED_DISPOSITIONS: frozenset[str] = frozenset({"unresolved_proposed", "unresolved_charted"})

ReconciliationFinding = Literal[
    # Two proposed lines that a curated interaction rule pairs. Neither is charted, so no
    # per-drug check against the chart can produce this.
    "intra_list_interaction",
    # Two proposed lines carrying the same molecule.
    "intra_list_duplicate",
    # Two proposed lines in the same therapeutic class, without sharing a molecule.
    "intra_list_class_overlap",
    # A charted drug missing from the proposed list, where stopping it abruptly is itself the
    # documented hazard. See ``_ABRUPT_STOP_CLASSES``.
    "high_risk_omission",
]


@dataclass(frozen=True)
class ProposedMedication:
    """One line of the list the clinician is reconciling *to*.

    ``drug`` is None when the name resolved to nothing — the line is kept rather than dropped,
    and ``name`` is what the clinician wrote, which is the only thing there is to show them.

    The dose triple is carried verbatim from the request and compared only against the chart's
    own triple, never parsed into a quantity. Deciding whether "500 mg BD" and "1 g daily" are
    the same daily dose is a real question and not this module's: it needs the drug's units and
    a frequency vocabulary, both of which live in ``app.core.dose_text``. What is wanted here is
    narrower and answerable — did anything about the written dose change — and answering the
    narrow question exactly is better than answering the wide one approximately underneath a
    clinician's assumption that it was answered.
    """

    name: str
    drug: DrugRef | None = None
    dose: str | None = None
    dose_unit: str | None = None
    frequency: str | None = None


@dataclass(frozen=True)
class ChartedCurrentMedication:
    """One current medication as the chart holds it, with the dose the chart wrote."""

    name: str
    drug: DrugRef | None = None
    dose: str | None = None
    dose_unit: str | None = None
    frequency: str | None = None


@dataclass(frozen=True)
class ReconciliationLine:
    """One row of the reconciliation table: what each list said, and how they compare."""

    disposition: Disposition
    # How to name this line to a clinician. The proposed name when there is one, because that is
    # the text in front of them; the charted name for a ``stop``.
    label: str
    summary: str
    proposed_name: str | None = None
    charted_name: str | None = None
    reference_id: str | None = None
    proposed_dose: str | None = None
    charted_dose: str | None = None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationFlag:
    """A finding about the proposed list itself, rather than about one line of it."""

    finding: ReconciliationFinding
    severity: Severity
    summary: str
    details: dict = field(default_factory=dict)
    drug_interaction_id: str | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    lines: list[ReconciliationLine] = field(default_factory=list)
    flags: list[ReconciliationFlag] = field(default_factory=list)


# Therapeutic classes where *stopping* is the documented hazard, keyed on the lower-cased
# ``drug_class`` the vocabulary carries. An omission from a new list is worth a clinician's
# attention for any drug; for these it is worth more than the ordinary note, because the harm
# from an unintended stop is abrupt, specific and well described:
#
#   vitamin k antagonist   thrombosis on unopposed withdrawal
#   anticonvulsant         withdrawal seizures, including in patients who are seizure-free
#   mood stabilizer        relapse; lithium discontinuation carries a rebound risk
#   corticosteroid         adrenal insufficiency after suppression, which is a medical emergency
#   thyroid hormone        the deficit is silent for weeks and then is not
#   insulin combination    diabetic ketoacidosis
#   cardiac glycoside      loss of rate control
#
# Deliberately a small, curated list of classes this vocabulary actually carries rather than an
# attempt at completeness. Every omission is reported regardless of class — this only decides
# which ones are additionally raised as a flag about the list, and a class missing from here
# loses the flag, never the line.
_ABRUPT_STOP_CLASSES: frozenset[str] = frozenset(
    {
        "vitamin k antagonist",
        "anticonvulsant",
        "mood stabilizer",
        "corticosteroid",
        "thyroid hormone",
        "insulin combination",
        "cardiac glycoside",
    }
)

# Interaction severity to the flag severity and wording used here. Mirrors the mapping in
# ``app.core.safety`` deliberately rather than importing it: that table also decides
# ``is_hard_block``, and an intra-list interaction is never a hard block. A hard block is a
# refusal to proceed, and there is nothing to refuse — nothing has been prescribed yet. What
# this is, is the one moment before both drugs exist on the chart at which the pair can be
# pointed out, and the clinician is choosing between them right now.
_INTRA_LIST_INTERACTION_SEVERITY: dict[str, Severity] = {
    "contraindicated": "critical",
    "major": "critical",
    "moderate": "warning",
    "minor": "info",
}
_UNRECOGNISED_INTERACTION_SEVERITY: Severity = "warning"


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


def _dose_text(dose: str | None, unit: str | None, frequency: str | None) -> str | None:
    """The chart's written dose as one comparable string, or None when nothing was written.

    Normalised only for whitespace and case, because this is a comparison of two transcriptions
    of the same prescription and not an interpretation of either. "500" + "mg" + "BD" and
    "500" + "MG" + "bd" are the same line typed twice; "500 mg BD" and "250 mg QID" are not the
    same line even though they are arguably the same daily dose, and reporting them as equal
    would hide a rewritten prescription behind arithmetic this module is not doing.
    """
    parts = [p for p in (_norm(dose), _norm(unit), _norm(frequency)) if p]
    return " ".join(parts) or None


def _identity(drug: DrugRef | None) -> frozenset[tuple[str, str]]:
    """Every token two products can be recognised as the same molecule by.

    Reference id **and** normalised generic name, for the product and for each of its
    ingredients, tagged so the two namespaces cannot collide. This is the same double-keyed
    match ``app.core.safety.check_duplicate_therapy`` makes, and reconciliation has to make it
    for a reason that is specific to comparing lists.

    One molecule has several vocabulary rows: aspirin is ASP-75 *and* ASP-150, levothyroxine is
    LT4-50 *and* LT4-100, because a row is a product at a strength. Matched on reference id
    alone, a patient charted on Ecosprin 75 whose new list says Ecosprin 150 reconciles as a
    stop plus a start — the drug reported as discontinued and separately as newly begun, when
    what happened is a dose change. That is worse than useless on this endpoint: an omission is
    the finding it exists to surface, and manufacturing false ones is how the real ones stop
    being read. The same pair inside one proposed list is a doubled antiplatelet dose reported
    as two unrelated drugs.

    Empty values are excluded on both sides, which is what keeps two *different* unidentified
    ingredients — Augmentin's clavulanic acid, Septran's trimethoprim, neither with a standalone
    row — from comparing equal. Same guard, and same reason, as the engine's.

    Empty for an unresolved line, so an unresolved line matches nothing rather than matching
    every other unresolved line: ``_shares_molecule`` intersects two of these, and an empty
    intersection is never a match. Two drug names nobody could read are not evidence of being
    the same drug.
    """
    if drug is None:
        return frozenset()
    tokens: set[tuple[str, str]] = set()
    for part in (drug, *drug.components):
        if part.reference_id:
            tokens.add(("ref", _norm(part.reference_id)))
        if _norm(part.generic_name):
            tokens.add(("name", _norm(part.generic_name)))
    return frozenset(tokens)


def _shares_molecule(a: DrugRef | None, b: DrugRef | None) -> bool:
    return bool(_identity(a) & _identity(b))


def _reference_ids(drug: DrugRef | None) -> frozenset[str]:
    """The reference ids alone — what a curated *rule* can be keyed on.

    Kept apart from ``_identity`` because the two are used for different things and widening
    either to the other would be wrong. A rule is looked up by reference id and by nothing else,
    so feeding it generic-name tokens would find no rule and cost a pointless pass; and matching
    two products for sameness by reference id alone is the bug ``_identity`` documents.
    """
    if drug is None:
        return frozenset()
    return frozenset(ingredient_reference_ids(drug))


def _label(drug: DrugRef | None, fallback: str) -> str:
    """What to call a line: the vocabulary's generic name, or what the clinician wrote."""
    if drug is not None and drug.generic_name:
        return drug.generic_name
    return fallback


def reconcile(
    proposed: list[ProposedMedication],
    charted: list[ChartedCurrentMedication],
) -> list[ReconciliationLine]:
    """Compare two medication lists line by line. Pure, order-stable, total.

    Total in the sense that matters clinically: every input line appears in the output exactly
    once, under some disposition. There is no path here that consumes a line and emits nothing,
    which is the property that makes the returned table safe to read as "these two lists, fully
    compared" — a reconciliation that quietly discards what it could not classify is worse than
    no reconciliation, because it is the same report minus the rows that needed attention.

    Proposed lines come first and in their submitted order, then the charted-only ones, so the
    table reads as the clinician's own list with the omissions gathered underneath it.
    """
    lines: list[ReconciliationLine] = []
    # Index of charted rows already claimed by a proposed line, by position rather than by
    # identity: a chart legitimately holds two rows for one drug (the merge keys a medication on
    # name *and* dose, so a dose change lands as a second row), and consuming "the charted
    # metformin" by identity would leave the second row looking like an omission.
    matched_charted: set[int] = set()

    for line in proposed:
        if line.drug is None:
            lines.append(
                ReconciliationLine(
                    disposition="unresolved_proposed",
                    label=line.name,
                    proposed_name=line.name,
                    proposed_dose=_dose_text(line.dose, line.dose_unit, line.frequency),
                    summary=(
                        f"“{line.name}” could not be matched to a known drug, so it was not "
                        "compared against this patient's current medications — this is not the "
                        "same as it being new, or being unchanged."
                    ),
                )
            )
            continue

        match_index = next(
            (
                i
                for i, row in enumerate(charted)
                if i not in matched_charted and _shares_molecule(line.drug, row.drug)
            ),
            None,
        )
        proposed_dose = _dose_text(line.dose, line.dose_unit, line.frequency)

        if match_index is None:
            lines.append(
                ReconciliationLine(
                    disposition="start",
                    label=_label(line.drug, line.name),
                    proposed_name=line.name,
                    reference_id=line.drug.reference_id or None,
                    proposed_dose=proposed_dose,
                    summary=(
                        f"{_label(line.drug, line.name)} is on the proposed list and is not "
                        "among this patient's current medications — a new start."
                    ),
                )
            )
            continue

        matched_charted.add(match_index)
        row = charted[match_index]
        charted_dose = _dose_text(row.dose, row.dose_unit, row.frequency)
        label = _label(line.drug, line.name)
        # A dose change needs *both* doses to compare. One side blank is not evidence of a
        # change: it is a record that did not write the dose down, which is a gap in the chart
        # rather than a modification to the therapy, and calling it a change would put a
        # clinician's attention on the wrong line. Reported as ``continue`` with both doses
        # carried through, so the blank is visible in the table.
        changed = (
            proposed_dose is not None and charted_dose is not None and proposed_dose != charted_dose
        )
        lines.append(
            ReconciliationLine(
                disposition="dose_change" if changed else "continue",
                label=label,
                proposed_name=line.name,
                charted_name=row.name,
                reference_id=line.drug.reference_id or None,
                proposed_dose=proposed_dose,
                charted_dose=charted_dose,
                summary=(
                    (
                        f"{label} appears on both lists with a different written dose "
                        f"(chart: {charted_dose}; proposed: {proposed_dose}) — worth confirming "
                        "which is intended."
                    )
                    if changed
                    else (
                        f"{label} appears on both the proposed list and this patient's current "
                        "medications."
                    )
                ),
            )
        )

    for i, row in enumerate(charted):
        if i in matched_charted:
            continue
        if row.drug is None:
            lines.append(
                ReconciliationLine(
                    disposition="unresolved_charted",
                    label=row.name,
                    charted_name=row.name,
                    charted_dose=_dose_text(row.dose, row.dose_unit, row.frequency),
                    summary=(
                        f"“{row.name}” is charted as current but could not be matched to a "
                        "known drug, so the proposed list was not compared against it — it may "
                        "or may not be continued by something above."
                    ),
                )
            )
            continue
        label = _label(row.drug, row.name)
        lines.append(
            ReconciliationLine(
                disposition="stop",
                label=label,
                charted_name=row.name,
                reference_id=row.drug.reference_id or None,
                charted_dose=_dose_text(row.dose, row.dose_unit, row.frequency),
                details={"drug_class": row.drug.drug_class} if row.drug.drug_class else {},
                summary=(
                    f"{label} is among this patient's current medications and is not on the "
                    "proposed list — worth confirming whether stopping it is intended."
                ),
            )
        )

    return lines


def check_intra_list_interactions(
    proposed: list[ProposedMedication], rules: list[InteractionRule]
) -> list[ReconciliationFlag]:
    """Curated interactions between two drugs that are *both* on the proposed list.

    The pair that no per-drug check can reach. Checking drug A against the chart and then drug B
    against the chart asks about A×chart and B×chart; A×B is asked by neither, and is asked by
    nothing at all until both have been prescribed and the next check happens to run.

    Every pair of *identities* is considered, so a combination product interacts through its
    components — the reason a rule keyed on warfarin fires against a proposed product whose
    generic name never says "warfarin". One flag per unordered pair of lines at the highest
    severity any rule for it carries, because two rules describing the same pair are two
    curations of one clinical fact and a clinician reading the list should see it once.
    """
    by_pair: dict[tuple[str, str], InteractionRule] = {}
    for rule in rules:
        key = _pair_key(rule.drug_a_reference_id, rule.drug_b_reference_id)
        existing = by_pair.get(key)
        if existing is None or _interaction_rank(rule) > _interaction_rank(existing):
            by_pair[key] = rule

    flags: list[ReconciliationFlag] = []
    for i, first in enumerate(proposed):
        if first.drug is None:
            continue
        for second in proposed[i + 1 :]:
            if second.drug is None:
                continue
            # Two lines carrying the same molecule are a duplicate, not an interaction: a drug
            # does not interact with itself, and every symmetric rule keyed on one id would
            # otherwise fire against the pair.
            if _shares_molecule(first.drug, second.drug):
                continue
            best: InteractionRule | None = None
            for a in sorted(_reference_ids(first.drug)):
                for b in sorted(_reference_ids(second.drug)):
                    candidate = by_pair.get(_pair_key(a, b))
                    if candidate is not None and (
                        best is None or _interaction_rank(candidate) > _interaction_rank(best)
                    ):
                        best = candidate
            if best is None:
                continue
            severity = _INTRA_LIST_INTERACTION_SEVERITY.get(
                _norm(best.severity), _UNRECOGNISED_INTERACTION_SEVERITY
            )
            first_label = _label(first.drug, first.name)
            second_label = _label(second.drug, second.name)
            flags.append(
                ReconciliationFlag(
                    finding="intra_list_interaction",
                    severity=severity,
                    summary=(
                        f"{first_label} and {second_label} are both on the proposed list and a "
                        f"curated interaction ({_norm(best.severity) or 'unspecified severity'}) "
                        f"applies to the pair: {best.description}"
                    ),
                    details={
                        "drugs": [first_label, second_label],
                        "interaction_severity": _norm(best.severity) or None,
                        # Says plainly why this could not have come from the per-drug screen,
                        # so the finding is not mistaken for a repeat of one already dismissed.
                        "both_proposed": True,
                    },
                    drug_interaction_id=best.interaction_id,
                )
            )
    return flags


def _pair_key(a: str | None, b: str | None) -> tuple[str, str]:
    """An unordered pair of reference ids as one hashable key.

    Sorted, because an interaction is symmetric and a rule curated as (warfarin, aspirin) has to
    match a list carrying them the other way round. The same reason
    ``app.core.safety._interaction_key`` sorts.
    """
    first, second = sorted((_norm(a), _norm(b)))
    return first, second


def _interaction_rank(rule: InteractionRule) -> int:
    order = {"contraindicated": 4, "major": 3, "moderate": 2, "minor": 1}
    return order.get(_norm(rule.severity), 2)


def check_intra_list_duplicates(
    proposed: list[ProposedMedication],
) -> list[ReconciliationFlag]:
    """The same molecule, or the same therapeutic class, twice in one proposed list.

    Both halves are ordinary transcription outcomes rather than exotic errors. A list assembled
    from two sources carries a brand on one line and its INN on another; a list assembled from
    one source carries a combination product alongside a single-ingredient one that overlaps it.
    Either way the patient receives a doubled dose of a molecule nobody intended to double.

    The class half is the weaker claim and is severity-graded accordingly: two drugs of one class
    is frequently deliberate (two antihypertensives of different classes is standard therapy;
    two of the *same* class usually is not, but "usually" is the operative word, and dual RAS
    blockade has legitimate uses). It is reported as something to confirm and never as an error.
    """
    flags: list[ReconciliationFlag] = []
    for i, first in enumerate(proposed):
        if first.drug is None:
            continue
        for second in proposed[i + 1 :]:
            if second.drug is None:
                continue
            first_label = _label(first.drug, first.name)
            second_label = _label(second.drug, second.name)
            shared = _identity(first.drug) & _identity(second.drug)
            if shared:
                # Named by the molecule where the tokens carry one, so the flag says
                # "aspirin" rather than "ASP-75". A pair that matched only on reference id has
                # no name token to show and falls back to the ids.
                ingredients = sorted(value for kind, value in shared if kind == "name") or sorted(
                    value for _kind, value in shared
                )
                flags.append(
                    ReconciliationFlag(
                        finding="intra_list_duplicate",
                        severity="critical",
                        summary=(
                            f"{first_label} and {second_label} are both on the proposed list and "
                            f"share an active ingredient ({', '.join(ingredients)}) — "
                            "prescribing both would give the patient that ingredient twice."
                        ),
                        details={
                            "drugs": [first_label, second_label],
                            "shared_ingredients": ingredients,
                        },
                    )
                )
                continue
            a_class, b_class = _norm(first.drug.drug_class), _norm(second.drug.drug_class)
            if a_class and a_class == b_class:
                flags.append(
                    ReconciliationFlag(
                        finding="intra_list_class_overlap",
                        severity="warning",
                        summary=(
                            f"{first_label} and {second_label} are both on the proposed list and "
                            f"are both {first.drug.drug_class} — worth confirming that two drugs "
                            "of one class is intended."
                        ),
                        details={
                            "drugs": [first_label, second_label],
                            "drug_class": first.drug.drug_class,
                        },
                    )
                )
    return flags


def check_high_risk_omissions(lines: list[ReconciliationLine]) -> list[ReconciliationFlag]:
    """Omitted drugs whose abrupt discontinuation is itself a documented hazard.

    Every omission already has a line of its own. This raises the ones from
    ``_ABRUPT_STOP_CLASSES`` to a flag as well, because the reconciliation table is read in
    order and a warfarin that fell off the list sits among nine other rows with the same visual
    weight as a dropped multivitamin. The two are not the same event.

    Phrased as a question about intent, never as an instruction to resume: a deliberate stop is
    exactly as common as an accidental one at these transitions, and the record does not say
    which this is. That is the clinician's to answer (Critical Safety Rule #4).
    """
    flags: list[ReconciliationFlag] = []
    for line in lines:
        if line.disposition != "stop":
            continue
        drug_class = _norm(line.details.get("drug_class"))
        if drug_class not in _ABRUPT_STOP_CLASSES:
            continue
        flags.append(
            ReconciliationFlag(
                finding="high_risk_omission",
                severity="critical",
                summary=(
                    f"{line.label} is charted as current, is absent from the proposed list, and "
                    f"belongs to a class ({line.details.get('drug_class')}) where stopping "
                    "abruptly carries its own documented risk — worth confirming the omission "
                    "is deliberate."
                ),
                details={
                    "drug": line.label,
                    "drug_class": line.details.get("drug_class"),
                },
            )
        )
    return flags


# Order the flags are presented in. Severity first, then a stable order within it, so the list a
# clinician reads top-down leads with what is most likely to change the prescription. Mirrors
# the intent of the drug-safety screen's ordering rather than sharing its code, because that
# ordering also ranks hard blocks, and nothing here is one.
_SEVERITY_ORDER: dict[str, int] = {"critical": 0, "warning": 1, "info": 2}


def reconcile_medications(
    proposed: list[ProposedMedication],
    charted: list[ChartedCurrentMedication],
    interaction_rules: list[InteractionRule] | None = None,
) -> ReconciliationResult:
    """The whole list-level reconciliation: the line table, plus every finding about the list.

    The one entry point the service layer calls, so that adding a check here reaches every
    caller rather than the ones someone remembered to update.
    """
    lines = reconcile(proposed, charted)
    flags = [
        *check_intra_list_duplicates(proposed),
        *check_intra_list_interactions(proposed, interaction_rules or []),
        *check_high_risk_omissions(lines),
    ]
    flags.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 3), f.finding, f.summary))
    return ReconciliationResult(lines=lines, flags=flags)


def proposed_reference_ids(proposed: list[ProposedMedication]) -> set[str]:
    """Every reference id a rule for anything on the proposed list could be keyed on.

    The service scopes its interaction-rule query to the drugs in play. A proposed list scoped to
    the *charted* drugs alone would load no rule whose both sides are new — which is precisely
    the intra-list pair this module exists to find, so the scope and the check have to be built
    from the same set or the check silently has no rules to apply. Exported for that reason,
    exactly as ``app.core.safety.ingredient_reference_ids`` is.
    """
    ids: set[str] = set()
    for line in proposed:
        ids |= _reference_ids(line.drug)
    return ids
