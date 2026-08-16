"""A patient's prescribing history, assembled per drug rather than per row.

The gap
-------
``medication_events`` is an event log: one row per start, stop, change, continue or one-time
dose, each carrying the dose and frequency written at that moment. Every read of it in this API
returned it *as* a log — the chart view sorts current-first then newest-first, the FHIR export
emits one ``MedicationStatement`` per row — so the question a clinician actually asks about a
drug ("when did this start, what has it been, and who changed it") could only be answered by
scrolling a mixed list of every drug's events interleaved by date and reconstructing each
drug's story by eye.

What this assembles
-------------------
The same rows, grouped by the drug they are about and ordered oldest-first *within* each group,
with three things computed across each group that no single row carries:

* **the span** — when the drug first appears and when it stopped, where the record says;
* **the dose changes** — each entry says what the dose was before it, so a titration reads as a
  sequence rather than as five rows that happen to share a name;
* **whether it is still on the chart** — ``is_current`` is a per-row flag written at merge time,
  and the group's answer is not simply "any row is current": a later ``stop`` ends the therapy
  whatever an earlier row's flag says, which is the same precedence
  ``export_service._medication`` applies and the one the chart is driven by.

What it deliberately does not do
--------------------------------
It does not merge two drugs it cannot prove are the same drug. Grouping is by
``drug_vocabulary_id`` where the row has one and by normalised name where it does not, and the
two never collapse into each other: a row that resolved to Amlodipine and an unresolved
"Amlodipin 5mg" line stay separate groups. Guessing they are one is the string-equality shortcut
CLAUDE.md pitfall #4 forbids, and the consequence here — a fabricated dose change between two
drugs that were never the same prescription — is worse than two adjacent groups.

Nor does it interpret. No adherence judgement, no gap analysis, no "the patient stopped taking
this": the record holds prescribing events, not dispensing or ingestion, and the distance
between them is exactly where a confident-sounding inference would be wrong.

Deterministic and offline — a query and a regrouping, no LLM anywhere on this path.

Bounded, in two independent places
----------------------------------
This read is a whole-chart read, and it was unbounded in both directions: every medication event
the chart holds was loaded, grouped, and serialised into one response. That is fine for the
twelve-event chart it was written against and wrong as a shape — a patient with twenty years of
prescribing, or one whose list arrived through the bulk importer, is the same route returning
megabytes assembled in memory, on the ordinary authenticated rate budget. The paged chart read
(``GET ../record``) and the FHIR export both bounded themselves; this one, added later, did not.

The two bounds are separate because they solve different problems and neither implies the other:

* ``EVENT_CEILING`` bounds the **rows read**, before any grouping. It has to be a ceiling on the
  read rather than a page, because every figure a group reports — ``started_on``, the dose
  changes, ``is_current`` — is computed *across* the whole group, so paging the SQL would produce
  groups that are confidently wrong rather than visibly short. When it is hit the response says
  so (``events_truncated``), because a prescribing history missing its oldest half is
  indistinguishable from a shorter history, and that is a clinical difference.
* ``limit``/``offset`` page the **drug groups** returned, after grouping. That is what keeps a
  response bounded for a chart on forty drugs, and it is safe precisely because it happens after
  every per-group figure has been computed from the full set.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.drug_vocabulary import DrugVocabulary
from app.models.encounter import Encounter
from app.models.medication_event import MedicationEvent

# Event types that put a patient *on* a drug, as opposed to taking them off it. ``continue`` and
# ``change`` both mean the therapy is running; ``one_time`` is a single administration, which
# starts and ends at the same moment and so is neither a start nor a stop of ongoing therapy.
_ONGOING_EVENT_TYPES = frozenset({"start", "change", "continue"})


def _dose_text(row: MedicationEvent) -> str | None:
    """ "500 mg BD" — the three dose columns as the one string a clinician compares.

    A change is detected on this rendering rather than on the columns individually, because
    "500 mg twice daily" -> "500 mg once daily" is a dose change and comparing only ``dose``
    would miss it, while comparing the three columns pairwise would report three changes for
    one edit.
    """
    text = " ".join(
        str(part).strip()
        for part in (row.dose, row.dose_unit, row.frequency)
        if part and str(part).strip()
    )
    return text or None


@dataclass
class TimelineEntry:
    """One medication event, in the context of the drug's own history."""

    id: uuid.UUID
    event_type: str
    event_date: date | None
    end_date: date | None
    dose: str | None
    dose_unit: str | None
    frequency: str | None
    route: str | None
    dose_text: str | None
    # What the dose was on the previous entry for this drug, when it differed. None both when
    # this is the first entry and when nothing changed — the two are distinguished by
    # ``dose_changed``, because "the dose before this was nothing" and "the dose did not change"
    # are different statements and a null cannot say which.
    previous_dose_text: str | None = None
    dose_changed: bool = False
    duration_text: str | None = None
    prescriber_name: str | None = None
    is_current: bool = False
    clinician_confirmed: bool = False
    source_document_id: uuid.UUID | None = None
    encounter_id: uuid.UUID | None = None
    encounter_date: date | None = None
    encounter_type: str | None = None


@dataclass
class TimelineDrug:
    """Every event this record holds about one drug, and what they add up to."""

    drug_vocabulary_id: uuid.UUID | None
    reference_id: str | None
    generic_name: str | None
    brand_name_raw: str | None
    entries: list[TimelineEntry] = field(default_factory=list)
    started_on: date | None = None
    stopped_on: date | None = None
    is_current: bool = False
    dose_change_count: int = 0
    # True when the vocabulary could not identify this drug, so the group is keyed on the name
    # the chart wrote. Carried onto the wire because everything downstream of an unresolved drug
    # — every interaction, contraindication and allergy rule — did not run for it, and a timeline
    # that looks identical either way would hide that.
    unresolved: bool = False

    @property
    def display_name(self) -> str:
        return self.generic_name or self.brand_name_raw or "unnamed medication"


@dataclass
class Timeline:
    """One page of drug groups, and what the whole chart holds behind it."""

    drugs: list[TimelineDrug]
    # Before paging, both of them. `total_events` counts the events in every group, not only the
    # ones on this page, so it answers "how much prescribing is on this chart" rather than "how
    # much did you just send me".
    total_drugs: int
    total_events: int
    # True when the chart holds more events than EVENT_CEILING and the oldest were kept. Carried
    # to the wire: a history missing its most recent entries reads exactly like a history that
    # ended, and a clinician must not have to guess which they are looking at.
    events_truncated: bool


# How many medication events one call will read before it stops. Sized so that no real chart
# reaches it — a patient on ten drugs for twenty years, re-prescribed monthly, is about 2,400
# events — while still bounding what a chart assembled by a broken importer or an automated
# ingest can cost a single request.
EVENT_CEILING = 5000

# Drug groups per page, and the ceiling a client may ask for. Both are about response size rather
# than database work: the events are already read and grouped by the time paging applies. Twenty
# five is more drugs than a chart normally carries, so the ordinary request is one page.
DEFAULT_DRUG_LIMIT = 25
MAX_DRUG_LIMIT = 200


class PrescriptionTimelineService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def timeline(
        self,
        patient_id: uuid.UUID,
        *,
        limit: int = DEFAULT_DRUG_LIMIT,
        offset: int = 0,
    ) -> Timeline:
        """This chart's prescribing history, grouped by drug, as one page of groups.

        Three queries regardless of how many drugs the chart holds: the events, the vocabulary
        rows they point at, and the encounters they were recorded at. The per-row alternative
        would be an N+1 on the two lookups, which is the shape this codebase has removed from
        the safety path twice.

        ``limit``/``offset`` page the **groups**, and every figure inside a group is still
        computed from every event the ceiling let through — see the module docstring for why
        those are two different bounds. ``Timeline.total_drugs`` and ``total_events`` are the
        counts before paging, so a short page and a last page are distinguishable.
        """
        rows = list(
            (
                await self.db.execute(
                    select(MedicationEvent)
                    .where(
                        MedicationEvent.patient_id == patient_id,
                        MedicationEvent.is_deleted.is_(False),
                    )
                    # Oldest first, which is the order the groups are built in and therefore the
                    # order a dose change is detected against. Undated rows sort last: a row with
                    # no date cannot be placed in the sequence, and putting it first would make
                    # every later entry read as a change from it. The primary key breaks ties so
                    # two events charted on the same day order the same way on every call —
                    # SQLite's ``created_at`` has one-second resolution and cannot.
                    .order_by(
                        MedicationEvent.event_date.asc().nullslast(),
                        MedicationEvent.created_at.asc(),
                        MedicationEvent.id,
                    )
                    # One past the ceiling, so "we read exactly the ceiling" and "there was
                    # more" are distinguishable without a second COUNT over the same rows.
                    .limit(EVENT_CEILING + 1)
                )
            )
            .scalars()
            .all()
        )
        events_truncated = len(rows) > EVENT_CEILING
        if events_truncated:
            # The *oldest* events are kept and the newest dropped, because the ordering above is
            # oldest-first and a group's story is built forwards: `started_on` and every
            # `previous_dose_text` depend on the beginning being present. Dropping the tail
            # loses recent entries, which the response admits; dropping the head would silently
            # invent a start date and a first dose for every drug on the chart.
            rows = rows[:EVENT_CEILING]
        if not rows:
            return Timeline(drugs=[], total_drugs=0, total_events=0, events_truncated=False)

        vocab = await self._vocabulary(rows)
        encounters = await self._encounters(rows)

        groups: dict[tuple[str, str], TimelineDrug] = {}
        for row in rows:
            key = self._key(row)
            group = groups.get(key)
            if group is None:
                vocab_row = vocab.get(row.drug_vocabulary_id) if row.drug_vocabulary_id else None
                group = TimelineDrug(
                    drug_vocabulary_id=row.drug_vocabulary_id,
                    reference_id=vocab_row.reference_id if vocab_row else None,
                    generic_name=row.generic_name
                    or (vocab_row.generic_name if vocab_row else None),
                    brand_name_raw=row.brand_name_raw,
                    unresolved=vocab_row is None,
                )
                groups[key] = group
            group.entries.append(self._entry(row, group, encounters))

        for group in groups.values():
            self._summarise(group)
        ordered = sorted(
            groups.values(),
            key=lambda g: (
                # Current therapy first — it is what a clinician is looking for — then the most
                # recently touched, then by name so an unordered tie is still deterministic.
                not g.is_current,
                -_ordinal(_latest_date(g)),
                g.display_name.lower(),
            ),
        )
        return Timeline(
            drugs=ordered[offset : offset + limit],
            # Counts over the whole assembly, not over the page: a client must not have to infer
            # "is there more" from the length of the array it was handed.
            total_drugs=len(ordered),
            total_events=sum(len(g.entries) for g in ordered),
            events_truncated=events_truncated,
        )

    @staticmethod
    def _key(row: MedicationEvent) -> tuple[str, str]:
        """What makes two rows the same drug.

        The vocabulary id when the row has one, because that is the only identity in this system
        that survives a brand name being written differently. Otherwise the normalised name the
        chart wrote — which groups "Zyxolol 40" with "zyxolol 40" and with nothing else. The two
        key *spaces* are tagged apart so a name can never collide with an id, and an unresolved
        row is never folded into a resolved group: "these two strings look alike" is not evidence
        that they are one prescription.
        """
        if row.drug_vocabulary_id:
            return ("vocabulary", str(row.drug_vocabulary_id))
        name = (row.generic_name or row.brand_name_raw or "").strip().lower()
        return ("name", name or "(unnamed)")

    async def _vocabulary(self, rows: list[MedicationEvent]) -> dict[uuid.UUID, DrugVocabulary]:
        ids = {row.drug_vocabulary_id for row in rows if row.drug_vocabulary_id}
        if not ids:
            return {}
        result = await self.db.execute(select(DrugVocabulary).where(DrugVocabulary.id.in_(ids)))
        return {row.id: row for row in result.scalars().all()}

    async def _encounters(self, rows: list[MedicationEvent]) -> dict[uuid.UUID, Encounter]:
        ids = {row.encounter_id for row in rows if row.encounter_id}
        if not ids:
            return {}
        result = await self.db.execute(select(Encounter).where(Encounter.id.in_(ids)))
        return {row.id: row for row in result.scalars().all()}

    @staticmethod
    def _entry(
        row: MedicationEvent, group: TimelineDrug, encounters: dict[uuid.UUID, Encounter]
    ) -> TimelineEntry:
        dose_text = _dose_text(row)
        # The previous *stated* dose, not simply the previous entry's: a stop line carries no
        # dose, and reading its None as "the dose changed to nothing and then back" would invent
        # two changes out of one discontinuation and restart.
        previous = next(
            (entry.dose_text for entry in reversed(group.entries) if entry.dose_text), None
        )
        changed = bool(dose_text and previous and dose_text != previous)
        encounter = encounters.get(row.encounter_id) if row.encounter_id else None
        return TimelineEntry(
            id=row.id,
            event_type=row.event_type,
            event_date=row.event_date,
            end_date=row.end_date,
            dose=row.dose,
            dose_unit=row.dose_unit,
            frequency=row.frequency,
            route=row.route,
            dose_text=dose_text,
            previous_dose_text=previous if changed else None,
            dose_changed=changed,
            duration_text=row.duration_text,
            prescriber_name=row.prescriber_name,
            is_current=row.is_current,
            clinician_confirmed=row.clinician_confirmed,
            source_document_id=row.source_document_id,
            encounter_id=row.encounter_id,
            encounter_date=encounter.encounter_date if encounter else None,
            encounter_type=encounter.encounter_type if encounter else None,
        )

    @staticmethod
    def _summarise(group: TimelineDrug) -> None:
        """The span, the change count, and whether the therapy is still running.

        ``is_current`` follows the precedence the chart itself is driven by and the export
        already states: a ``stop`` event ends the therapy whatever an earlier row's flag says,
        and only then does the flag on the ongoing rows decide. The order matters — reading
        "any row is current" first would leave a discontinued drug on the current list forever,
        because the merge writes ``is_current`` per row and never revisits its predecessors.
        """
        ongoing = [e for e in group.entries if e.event_type in _ONGOING_EVENT_TYPES]
        stops = [e for e in group.entries if e.event_type == "stop"]
        group.started_on = next((e.event_date for e in group.entries if e.event_date), None)
        last_stop = next((e for e in reversed(stops) if e.event_date or e.end_date), None)
        last_ongoing = next((e for e in reversed(ongoing) if e.event_date), None)
        # A stop only ends the therapy if nothing restarted it afterwards. Undated events cannot
        # be placed in that comparison, so a stop with no date is treated as the last word — the
        # conservative reading for a *timeline*, where claiming a drug is current is the answer
        # that puts it back in front of a prescriber.
        stopped_at = (last_stop.event_date or last_stop.end_date) if last_stop else None
        restarted_at = last_ongoing.event_date if last_ongoing else None
        restarted = (
            stopped_at is not None and restarted_at is not None and restarted_at > stopped_at
        )
        if stops and not restarted:
            group.is_current = False
            group.stopped_on = next(
                (e.end_date or e.event_date for e in reversed(stops) if e.end_date or e.event_date),
                None,
            )
        else:
            group.is_current = any(e.is_current for e in ongoing)
            group.stopped_on = (
                next((e.end_date for e in reversed(ongoing) if e.end_date), None)
                if not group.is_current
                else None
            )
        group.dose_change_count = sum(1 for e in group.entries if e.dose_changed)


def _latest_date(group: TimelineDrug) -> date | None:
    return next(
        (e.event_date for e in reversed(group.entries) if e.event_date),
        None,
    )


def _ordinal(value: date | None) -> int:
    """A sortable integer for a possibly-absent date; undated sorts oldest."""
    return value.toordinal() if value is not None else 0


def serialise(groups: list[TimelineDrug]) -> list[dict[str, Any]]:
    """The timeline as plain dicts, for the Pydantic response model."""
    return [
        {
            "drug_vocabulary_id": group.drug_vocabulary_id,
            "reference_id": group.reference_id,
            "generic_name": group.generic_name,
            "brand_name_raw": group.brand_name_raw,
            "display_name": group.display_name,
            "unresolved": group.unresolved,
            "started_on": group.started_on,
            "stopped_on": group.stopped_on,
            "is_current": group.is_current,
            "dose_change_count": group.dose_change_count,
            "event_count": len(group.entries),
            "entries": [vars(entry) for entry in group.entries],
        }
        for group in groups
    ]
