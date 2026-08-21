"""What has to be true before a patient goes home, and what the chart is told when they do.

A discharge medication list is the most dangerous document a hospital produces. It is written
under time pressure at the end of an admission, it is the only list the patient and their next
clinician will ever see, and every difference between it and the chart is silent: a drug held
for a procedure and never restarted, a dose halved for renal impairment that recovered, a
regular medicine nobody transcribed. The literature calls the result an unintentional
discrepancy and finds one on roughly half of discharges.

This project could already *compare* two medication lists — ``app.core.med_reconciliation`` does
list-against-list and ``SafetyService`` does list-against-patient — but the comparison was
transient. It was computed for a request, rendered, and discarded. Nothing recorded which drugs
were continued, started, stopped or changed at the transition; nothing stopped a discharge going
out over an unacknowledged panic potassium; and, worst of the three, the chart was never told.
The patient went home on the new list and the record went on saying they were on the old one, so
every safety check at the next visit ran against a medication list that had been wrong since the
day they left.

This module is the deterministic half of fixing that. Two pure functions:

* :func:`assess_readiness` — the rules that decide whether a discharge may be finalised at all,
  given plain facts about the chart. Blocking items refuse the finalisation; advisory items are
  reported and proceeded past.
* :func:`chart_actions` — the translation from a reconciliation's dispositions into the
  medication events finalising will write.

**Why this is pure and offline** (Critical Safety Rule #8). Deciding whether it is safe to send
a patient home is the last thing that should depend on a provider being reachable. Every rule
here is a function of numbers and strings the service layer hands in; there is no clock, no
database, no configuration and no LLM anywhere in the graph, and ``test_offline_engine_purity``
pins that as a property of the import graph rather than a convention.

**Why a stop is confirmed and not inferred.** ``med_reconciliation`` is explicit that its
dispositions are statements about two lists rather than instructions: a ``stop`` disposition
means "the discharge list does not carry a drug the chart calls current", which is an intended
discontinuation about half the time and a transcription omission the other half. So
:func:`chart_actions` produces a *proposal*, and the service refuses to finalise unless the
clinician has named each discontinuation — the same recompute-and-compare the handover checklist
uses, for the same reason: a confirmation of a state that no longer holds is worse than no
confirmation, because it is a signed statement that somebody checked.

**Microcopy is prescriber-framed throughout** (Critical Safety Rule #4). Nothing here tells a
clinician to do anything; every line states what the record shows and leaves the decision where
it belongs. ``tests/test_discharge_summary.py`` runs every string this module can emit through
``has_certainty_language``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.core.med_reconciliation import ReconciliationLine

# A discharge summary is being written, or it has been finalised and is part of the record.
# There is no third state: a discharge that was started and abandoned is a draft nobody
# finalised, and deleting it is chart withdrawal's job rather than a lifecycle of its own.
#
# In one place so the check constraint, the service's transition table and the schema enum
# cannot drift apart, exactly as ``ENCOUNTER_STATUSES`` and ``HANDOFF_STATUSES`` are.
DISCHARGE_STATUSES: tuple[str, ...] = ("draft", "finalized")

# The narrative a discharge summary is not a discharge summary without. Both are what the next
# clinician reads first and neither is recoverable from the structured chart: the diagnosis at
# discharge is frequently *not* the admitting one, and what happened in between exists nowhere
# else at all.
#
# Deliberately short. Follow-up instructions and advice for the patient are strongly wanted and
# are raised as advisory items below, not as refusals — a discharge held up over a blank advice
# box is a discharge that gets written somewhere else.
REQUIRED_SECTIONS: tuple[str, ...] = ("discharge_diagnosis", "hospital_course")

Severity = Literal["blocking", "advisory"]

# Ranking for the readiness list. Blocking before advisory is not a matter of taste — the list
# is read top-down and the items that stop a discharge belong at the top of it — and within each
# group the order is fixed so that two runs over the same chart produce the same document.
_ITEM_RANK: tuple[str, ...] = (
    "unacknowledged_critical_labs",
    "active_hard_blocks",
    "unresolved_discharge_medications",
    "missing_sections",
    "high_risk_omissions",
    "unresolved_charted_medications",
    "no_follow_up_arranged",
    "documents_needing_confirmation",
)


@dataclass(frozen=True)
class ReadinessItem:
    """One thing the record says about this discharge, and whether it stops it.

    ``count`` is the number of things behind the item — panic values, unreadable lines, missing
    sections — and is what the UI leads with. It is never zero: an item with nothing behind it
    is omitted from the list entirely rather than shown as satisfied, the same judgement the
    handover checklist makes. A list that always shows eight rows, three of them permanently
    "0 — nothing to do", is a list people learn to scroll past, and then the one that mattered
    is scrolled past identically.
    """

    key: str
    severity: Severity
    summary: str
    count: int
    subjects: tuple[str, ...] = ()

    @property
    def is_blocking(self) -> bool:
        return self.severity == "blocking"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "severity": self.severity,
            "summary": self.summary,
            "count": self.count,
            "subjects": list(self.subjects),
        }


@dataclass(frozen=True)
class DischargeFacts:
    """Everything about the chart the readiness rules read, as plain values.

    A dataclass of numbers and strings rather than the chart itself, which is what keeps this
    module a pure function and lets the whole rule set be exercised without a database. Every
    field is gathered by ``DischargeService`` from queries that already exist for other screens.
    """

    # Panic and critical laboratory values on this chart that nobody has acknowledged. Counted
    # for this patient specifically, not for the account's whole queue.
    unacknowledged_critical_labs: int = 0
    # Hard blocks standing against the discharge medication list, from the per-patient half of
    # reconciliation. Critical Safety Rule #3: a hard block is passed only by a documented
    # override against the check that raised it, never by a screen that decided to proceed.
    hard_blocks: int = 0
    # Names on the discharge list the drug vocabulary could not identify.
    unresolved_discharge_medications: tuple[str, ...] = ()
    # Charted current medications the vocabulary could not identify, and which were therefore
    # compared against nothing.
    unresolved_charted_medications: tuple[str, ...] = ()
    # Charted drugs absent from the discharge list whose therapeutic class makes an abrupt stop
    # the documented hazard. Produced by ``med_reconciliation.check_high_risk_omissions``.
    high_risk_omissions: tuple[str, ...] = ()
    # Whether anything at all arranges what happens next: a future appointment on the diary, or
    # written follow-up instructions. Either satisfies it; neither is an advisory item.
    follow_up_booked: bool = False
    follow_up_instructions: bool = False
    # Documents on this chart still flagged as needing a human to check them against the
    # original. Advisory: an unread scan at discharge may hold a medicine nobody transcribed.
    documents_needing_confirmation: int = 0
    # Required narrative sections that are still empty.
    missing_sections: tuple[str, ...] = ()


def assess_readiness(facts: DischargeFacts) -> list[ReadinessItem]:
    """What this chart currently says about discharging this patient, most serious first.

    Blocking items refuse the finalisation. Each of the four is a state in which the discharge
    document would assert something the record cannot support:

    * an unacknowledged critical value means a result nobody has acted on, and a discharge
      summary written over it says the admission concluded;
    * a hard block means a drug on the take-home list that the deterministic engine refuses,
      which Rule #3 makes a refusal rather than a warning;
    * an unidentifiable drug name means a take-home medicine that was checked against nothing —
      no allergy, no interaction, no dose range — while the document reads as reconciled;
    * a missing diagnosis or hospital course means the two things the next clinician opens the
      document for are not in it.

    Everything else is advisory: reported, counted, and proceeded past by a clinician who has
    read it. The split is deliberately conservative about what it refuses. A rule that blocks a
    discharge for something the clinician cannot fix from this screen does not make the
    discharge safer; it makes it happen on paper instead.
    """
    items: list[ReadinessItem] = []

    if facts.unacknowledged_critical_labs > 0:
        items.append(
            ReadinessItem(
                key="unacknowledged_critical_labs",
                severity="blocking",
                summary=(
                    f"{facts.unacknowledged_critical_labs} critical laboratory "
                    f"{_plural(facts.unacknowledged_critical_labs, 'value', 'values')} on this "
                    "chart carries no record of being acknowledged. A discharge summary written "
                    "over an unacknowledged panic value states that the episode concluded."
                ),
                count=facts.unacknowledged_critical_labs,
            )
        )

    if facts.hard_blocks > 0:
        items.append(
            ReadinessItem(
                key="active_hard_blocks",
                severity="blocking",
                summary=(
                    f"{facts.hard_blocks} safety hard "
                    f"{_plural(facts.hard_blocks, 'block', 'blocks')} stands against the "
                    "discharge medication list. A hard block is passed by recording an override "
                    "with written reasoning against the check that raised it."
                ),
                count=facts.hard_blocks,
            )
        )

    if facts.unresolved_discharge_medications:
        names = tuple(facts.unresolved_discharge_medications)
        items.append(
            ReadinessItem(
                key="unresolved_discharge_medications",
                severity="blocking",
                summary=(
                    f"{len(names)} {_plural(len(names), 'name', 'names')} on the discharge list "
                    "could not be matched to a known drug, so nothing was checked against the "
                    "chart for them — no allergy, no interaction, no dose range."
                ),
                count=len(names),
                subjects=names,
            )
        )

    if facts.missing_sections:
        sections = tuple(facts.missing_sections)
        items.append(
            ReadinessItem(
                key="missing_sections",
                severity="blocking",
                summary=(
                    f"{len(sections)} required {_plural(len(sections), 'section', 'sections')} "
                    f"of the summary {_plural(len(sections), 'is', 'are')} empty: "
                    f"{', '.join(_readable(s) for s in sections)}."
                ),
                count=len(sections),
                subjects=sections,
            )
        )

    if facts.high_risk_omissions:
        names = tuple(facts.high_risk_omissions)
        items.append(
            ReadinessItem(
                key="high_risk_omissions",
                severity="advisory",
                summary=(
                    f"{len(names)} charted {_plural(len(names), 'medication', 'medications')} "
                    "absent from the discharge list belongs to a class where an abrupt stop is "
                    "itself the documented hazard."
                ),
                count=len(names),
                subjects=names,
            )
        )

    if facts.unresolved_charted_medications:
        names = tuple(facts.unresolved_charted_medications)
        items.append(
            ReadinessItem(
                key="unresolved_charted_medications",
                severity="advisory",
                summary=(
                    f"{len(names)} current {_plural(len(names), 'medication', 'medications')} on "
                    "the chart could not be matched to a known drug, so the discharge list was "
                    "not compared against it."
                ),
                count=len(names),
                subjects=names,
            )
        )

    if not facts.follow_up_booked and not facts.follow_up_instructions:
        items.append(
            ReadinessItem(
                key="no_follow_up_arranged",
                severity="advisory",
                summary=(
                    "Nothing on this chart says what happens next: no future appointment is "
                    "booked and the follow-up instructions are empty."
                ),
                count=1,
            )
        )

    if facts.documents_needing_confirmation > 0:
        count = facts.documents_needing_confirmation
        items.append(
            ReadinessItem(
                key="documents_needing_confirmation",
                severity="advisory",
                summary=(
                    f"{count} {_plural(count, 'document', 'documents')} on this chart still "
                    "awaits checking against the original, and an unread page may carry a "
                    "medicine the discharge list does not."
                ),
                count=count,
            )
        )

    items.sort(key=lambda item: (not item.is_blocking, _ITEM_RANK.index(item.key)))
    return items


def blocking_items(items: list[ReadinessItem]) -> list[ReadinessItem]:
    return [item for item in items if item.is_blocking]


# What finalising writes onto the chart for one line of the reconciliation.
#
# ``continue`` is present and writes nothing, which is the point of listing it: the chart
# already carries a current row for the drug at that dose, and a second identical row would show
# up in the duplicate-therapy check as the patient being on the drug twice. It is in the
# proposal so the document can state that the line was reconciled and deliberately left alone,
# rather than being silently absent from a table that reads as complete.
ChartActionKind = Literal["start", "change", "stop", "continue"]

# The reconciliation dispositions that mean the chart has to change, mapped to what it changes
# to. ``unresolved_proposed`` and ``unresolved_charted`` are absent on purpose: neither line was
# ever compared, and a discharge carrying one is refused by ``assess_readiness`` before this is
# reached.
_DISPOSITION_ACTIONS: dict[str, ChartActionKind] = {
    "start": "start",
    "dose_change": "change",
    "stop": "stop",
    "continue": "continue",
}


@dataclass(frozen=True)
class ChartAction:
    """One medication event finalising this discharge would write, or explicitly would not."""

    kind: ChartActionKind
    label: str
    reference_id: str | None = None
    dose: str | None = None
    dose_unit: str | None = None
    frequency: str | None = None
    # The dose the chart held before this discharge, for a ``change``. Carried so the summary can
    # state the move rather than only the destination — "125 microgram, previously 100
    # microgram" is the line a reader checks, and the previous dose is gone from the chart the
    # moment the row is retired.
    previous_dose: str | None = None
    details: dict = field(default_factory=dict)

    @property
    def writes_to_chart(self) -> bool:
        return self.kind != "continue"

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "reference_id": self.reference_id,
            "dose": self.dose,
            "dose_unit": self.dose_unit,
            "frequency": self.frequency,
            "previous_dose": self.previous_dose,
            "details": dict(self.details),
        }


def chart_actions(lines: list[ReconciliationLine]) -> list[ChartAction]:
    """The medication events this discharge proposes, in a fixed order.

    A *proposal*. ``med_reconciliation`` produces statements about two lists, and the one that
    must never be executed on its own authority is ``stop``: "the discharge list does not carry a
    drug the chart calls current" is an intended discontinuation about half the time and a line
    somebody forgot to type the other half. ``DischargeService`` requires each stop to be named
    by the clinician before it writes any of them.

    Ordered starts, then changes, then stops, then continuations. Not cosmetic: the order the
    events are written in is the order they appear in the chart's own history, and a reader
    walking the discharge afterwards should meet what the patient is now on before what they
    came off.
    """
    actions: list[ChartAction] = []
    for line in lines:
        kind = _DISPOSITION_ACTIONS.get(line.disposition)
        if kind is None:
            continue
        actions.append(
            ChartAction(
                kind=kind,
                label=line.label,
                reference_id=line.reference_id,
                dose=line.proposed_dose if kind != "stop" else None,
                previous_dose=line.charted_dose if kind in ("change", "stop") else None,
                details=dict(line.details),
            )
        )
    order = {"start": 0, "change": 1, "stop": 2, "continue": 3}
    actions.sort(key=lambda a: (order[a.kind], a.label.lower()))
    return actions


def stop_labels(actions: list[ChartAction]) -> tuple[str, ...]:
    """The discontinuations a clinician has to confirm, folded for comparison.

    Lower-cased and sorted, because the confirmation arrives as text a client echoed back and
    the comparison must not turn on capitalisation. Sorted so the two sides of the comparison
    are compared as sets in a stable order and a mismatch reads as a mismatch rather than as a
    reordering.
    """
    return tuple(sorted(a.label.strip().lower() for a in actions if a.kind == "stop"))


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _readable(section: str) -> str:
    return section.replace("_", " ")
