"""A readable handover summary of a chart that is already in the record.

The gap
-------
A clinician picking up a patient they have not met had two ways to find out what was on the
chart: read all five sections of ``GET ../record`` — which is paged per section, newest-first,
and says nothing about what is *not* there — or open a full eight-agent reasoning session, which
answers a completely different question, costs a panel, and produces clinical opinions nobody
asked for. Neither is "tell me who this patient is".

What this is, and what it deliberately is not
---------------------------------------------
A **restatement** of charted facts, not a clinical opinion about them. That distinction decides
everything else in this module, because it is what places this outside Critical Safety Rule #1's
gate rather than around it. The Verifier exists to stand between the reasoning engine's
*opinions* — a differential, an investigation, a management option — and the clinician; nothing
here produces one. No ``ClinicalSuggestion`` is written, no autonomy tier is assigned, no
hypothesis is generated, and the endpoint's own contract says the output is a summary of the
record rather than advice about it.

That claim is only worth anything if it is enforced, so it is, three times over and never by
trusting the model:

1. **The prompt refuses the task.** ``prompts.CLINICAL_SUMMARY`` excludes diagnosis,
   recommendation, prognosis and any drug or condition not already in the record.
2. **Every string is re-framed on the way out.** ``app.core.clinical_language`` — the
   deterministic Rule #4 control — runs over each field. A model that wrote "start metformin"
   anyway reaches the clinician as "Guidelines support considering metformin", and the response
   says the rewrite fired.
3. **The facts travel with the prose.** The deterministic ``chart`` block is assembled from the
   database and returned beside the narrative, so what the summary was built from is on the same
   screen as the summary. Evidence before conclusion (Rule #6) applies to a paragraph as much as
   to a Reasoning Theatre panel.

Offline
-------
Rule #8 governs the *deterministic safety checks*, not this, and a summary is not one. But the
chart block is a plain query and it is built first and unconditionally, so a request made with
no provider reachable still answers with the structured record and ``degraded: true``, rather
than 503ing. A clinician who cannot get the prose can still get the facts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm import LLMClient, available_providers
from app.agents.prompts import CLINICAL_SUMMARY
from app.agents.untrusted import fenced
from app.agents.util import as_text, complete_json_off_loop
from app.config import settings
from app.core.clinical import age_from_dob
from app.core.clinical_language import prescriber_framed, prescriber_framed_list
from app.core.dates import is_plausible_clinical_date
from app.core.safety import CONDITION_RESOLVED_STATUSES
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.encounter import Encounter
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient

# How much of each section reaches the prompt. A summary is a summary: the whole point is that a
# clinician does not read 300 medication rows, and putting 300 of them in front of a model buys
# a worse paragraph at a higher token cost. The chart block returned to the caller carries the
# same slice, so what the clinician sees is exactly what the summary was written from — a wider
# block beside a narrower prompt would be the more misleading of the two options.
MAX_CONDITIONS = 40
MAX_MEDICATIONS = 40
MAX_LABS = 30
MAX_ENCOUNTERS = 10
MAX_ALLERGIES = 30

# The list fields the model is asked for. Named once so the framing pass, the empty-response
# fallback and the schema cannot drift apart.
_LIST_FIELDS = (
    "active_problems",
    "current_medications",
    "recent_investigations",
    "recent_encounters",
    "record_gaps",
)


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _number(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


@dataclass
class ChartFacts:
    """What the record says, before anything has been written about it.

    Returned to the caller in full. It is not prompt scaffolding that happens to be serialisable
    — it is the evidence half of the response, and the narrative is only readable as a summary
    *of* something if that something is on the screen with it.
    """

    patient_id: uuid.UUID
    age_years: int | None = None
    sex: str | None = None
    active_conditions: list[dict[str, Any]] = field(default_factory=list)
    current_medications: list[dict[str, Any]] = field(default_factory=list)
    recent_labs: list[dict[str, Any]] = field(default_factory=list)
    recent_encounters: list[dict[str, Any]] = field(default_factory=list)
    allergies: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        """True when there is nothing charted to summarise at all."""
        return not (
            self.active_conditions
            or self.current_medications
            or self.recent_labs
            or self.recent_encounters
            or self.allergies
        )


class ClinicalSummaryService:
    """Assemble a chart, then narrate it. Never the other way round."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def summarize(self, patient: Patient) -> dict[str, Any]:
        """The chart, the narrative written from it, and how the narrative was produced.

        ``degraded`` is true whenever the prose did not come from a live provider — no key
        configured, every provider failing, or the demo net answering — because a clinician
        reading a handover paragraph has to be able to tell a summary of their patient from a
        plausible arrangement of words. It is the same signal a reasoning run carries and it is
        computed the same way.
        """
        facts = await self.chart_facts(patient)
        narrative, source = await self._narrate(facts)
        framed, reframed = _frame(narrative)
        return {
            "patient_id": patient.id,
            "generated_at": datetime.now(UTC),
            # Deliberately first in the response body as well as in the code: evidence before
            # conclusion, which is Rule #6 applied to a paragraph.
            "chart": _serialise(facts),
            "summary": framed,
            "source": source,
            "degraded": source != "model",
            # Whether the deterministic Rule #4 control had to rewrite anything the model wrote.
            # Surfaced rather than swallowed: it is a fact about the provider on this call, and
            # the only place a prompt being ignored is visible.
            "prescriber_framing_applied": reframed,
        }

    async def chart_facts(self, patient: Patient) -> ChartFacts:
        """Everything the summary is allowed to be about, straight from the record.

        Five queries, each already the shape its section needs. Conditions the chart calls
        resolved are excluded — ``CONDITION_RESOLVED_STATUSES``, the same set the safety engine
        reads, so "active problem" means one thing across this codebase — while medications are
        filtered on ``is_current``, which is the flag the chart itself is driven by.

        Allergies are here even though nobody asked for them in the summary's own field list.
        They are the one part of a chart whose omission from a handover is dangerous rather than
        merely incomplete, and the cost of carrying them is one query.
        """
        facts = ChartFacts(patient_id=patient.id, sex=patient.sex)
        if patient.date_of_birth and is_plausible_clinical_date(patient.date_of_birth):
            age = age_from_dob(patient.date_of_birth, datetime.now(UTC).date())
            facts.age_years = age if age >= 0 else None

        conditions = await self.db.execute(
            select(Condition)
            .where(
                Condition.patient_id == patient.id,
                Condition.is_deleted.is_(False),
                Condition.status.not_in(tuple(CONDITION_RESOLVED_STATUSES)),
            )
            .order_by(Condition.onset_date.desc().nullslast(), Condition.condition_name)
            .limit(MAX_CONDITIONS)
        )
        facts.active_conditions = [
            {
                "condition_name": row.condition_name,
                "icd10_code": row.icd10_code,
                "status": row.status,
                "onset_date": _iso(row.onset_date),
                "severity": row.severity,
                "clinician_confirmed": row.clinician_confirmed,
            }
            for row in conditions.scalars().all()
        ]

        meds = await self.db.execute(
            select(MedicationEvent)
            .where(
                MedicationEvent.patient_id == patient.id,
                MedicationEvent.is_deleted.is_(False),
                MedicationEvent.is_current.is_(True),
            )
            .order_by(MedicationEvent.event_date.desc().nullslast(), MedicationEvent.id)
            .limit(MAX_MEDICATIONS)
        )
        facts.current_medications = [
            {
                "generic_name": row.generic_name,
                "brand_name_raw": row.brand_name_raw,
                "dose": row.dose,
                "dose_unit": row.dose_unit,
                "frequency": row.frequency,
                "route": row.route,
                "event_date": _iso(row.event_date),
                "clinician_confirmed": row.clinician_confirmed,
            }
            for row in meds.scalars().all()
        ]

        labs = await self.db.execute(
            select(LabResult)
            .where(LabResult.patient_id == patient.id, LabResult.is_deleted.is_(False))
            .order_by(
                LabResult.sample_date.desc().nullslast(),
                LabResult.created_at.desc(),
                LabResult.id,
            )
            .limit(MAX_LABS)
        )
        facts.recent_labs = [
            {
                "marker_name": row.marker_name,
                "value_numeric": _number(row.value_numeric),
                "value_text": row.value_text,
                "unit": row.unit,
                "reference_range_low": _number(row.reference_range_low),
                "reference_range_high": _number(row.reference_range_high),
                "is_abnormal": row.is_abnormal,
                "abnormality_direction": row.abnormality_direction,
                "sample_date": _iso(row.sample_date),
                "clinician_confirmed": row.clinician_confirmed,
            }
            for row in labs.scalars().all()
        ]

        encounters = await self.db.execute(
            select(Encounter)
            .where(Encounter.patient_id == patient.id, Encounter.is_deleted.is_(False))
            .order_by(Encounter.encounter_date.desc(), Encounter.id)
            .limit(MAX_ENCOUNTERS)
        )
        facts.recent_encounters = [
            {
                "id": str(row.id),
                "encounter_date": _iso(row.encounter_date),
                "encounter_type": row.encounter_type,
                "presenting_complaint": row.presenting_complaint,
                "status": row.status,
            }
            for row in encounters.scalars().all()
        ]

        allergies = await self.db.execute(
            select(Allergy)
            .where(
                Allergy.patient_id == patient.id,
                Allergy.is_deleted.is_(False),
                # "unknown" alongside "active" for the reason the safety engine reads it that
                # way: an allergy nobody has been able to confirm is the one a handover most
                # needs to carry, and reading it as absent is a fail-open.
                Allergy.status.in_(("active", "unknown")),
            )
            .order_by(Allergy.allergen_name, Allergy.id)
            .limit(MAX_ALLERGIES)
        )
        facts.allergies = [
            {
                "allergen_name": row.allergen_name,
                "allergen_type": row.allergen_type,
                "severity": row.severity,
                "reaction_description": row.reaction_description,
                "status": row.status,
            }
            for row in allergies.scalars().all()
        ]
        return facts

    async def _narrate(self, facts: ChartFacts) -> tuple[dict[str, Any], str]:
        """``(summary fields, how they were produced)``.

        Three sources, and the caller is told which: ``model`` (a provider answered),
        ``deterministic`` (none did, so the chart is restated without prose), and ``empty`` (an
        empty chart, which is answered without spending a call at all — there is nothing to
        summarise and a model asked to summarise nothing writes something).

        **The demo net is deliberately not one of them.** ``is_available()`` is true when no
        provider is configured but ``llm_demo_fallback`` is on, and ``complete_json`` then serves
        simulated scenario data — which is the right answer for a Reasoning Theatre nobody is
        treating a patient from, and the wrong one here: a handover paragraph is read as a
        statement about *this* patient, and a fabricated one is worse than no paragraph. So the
        gate is ``available_providers()``, and a ``_demo`` marker coming back from a mid-call
        failover is refused for the same reason. R57's defect was exactly this distinction —
        ``degraded`` asking "is a key configured" rather than "did the call work".
        """
        if facts.is_empty():
            return _empty_summary(), "empty"
        if not available_providers():
            return _deterministic_summary(facts), "deterministic"
        result = await complete_json_off_loop(
            LLMClient(max_tokens=settings.summary_max_tokens),
            CLINICAL_SUMMARY,
            "Patient record to summarise:\n" + fenced("patient record", _prompt_text(facts)),
        )
        if not isinstance(result, dict) or result.get("_demo"):
            return _deterministic_summary(facts), "deterministic"
        narrative = {
            "overview": as_text(result.get("overview")),
            **{field_name: _string_list(result.get(field_name)) for field_name in _LIST_FIELDS},
        }
        if not narrative["overview"] and not any(narrative[f] for f in _LIST_FIELDS):
            # A provider that answered with a well-formed object containing nothing. Serving it
            # would present an empty handover for a chart that has content on it, which reads as
            # "there is nothing here" rather than as "the summary failed".
            return _deterministic_summary(facts), "deterministic"
        return narrative, "model"


def _string_list(value: Any) -> list[str]:
    """A model-supplied list, flattened to non-empty strings.

    A model that answers a list field with a bare string, an object, or a list of objects is the
    ordinary case rather than the exceptional one — see ``app.agents.util.objects`` for how much
    of this engine that has cost — and every item here is rendered directly into a clinician's
    screen.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [text for text in (as_text(item) for item in items) if text]


def _frame(narrative: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Every field through the deterministic Rule #4 control, and whether anything moved."""
    overview, overview_changed = prescriber_framed(narrative.get("overview", ""))
    framed: dict[str, Any] = {"overview": overview}
    changed = overview_changed
    for field_name in _LIST_FIELDS:
        items, item_changed = prescriber_framed_list(list(narrative.get(field_name, [])))
        framed[field_name] = items
        changed = changed or item_changed
    return framed, changed


def _empty_summary() -> dict[str, Any]:
    return {
        "overview": (
            "This chart holds no conditions, medications, laboratory results or visits yet, so "
            "there is nothing on record to summarise."
        ),
        "active_problems": [],
        "current_medications": [],
        "recent_investigations": [],
        "recent_encounters": [],
        "record_gaps": [
            "The whole record is empty — nothing has been uploaded or charted for this patient."
        ],
    }


def _deterministic_summary(facts: ChartFacts) -> dict[str, Any]:
    """The chart restated without a provider: no prose, no interpretation, no invention.

    Every line is a rendering of a row. It is duller than the model's version and that is the
    point — the alternative when reasoning is offline is an empty screen, and a clinician who
    can read the medication list has most of what a handover paragraph was going to tell them.
    """
    who = ", ".join(
        part
        for part in (
            f"{facts.age_years} years old" if facts.age_years is not None else None,
            facts.sex if facts.sex and facts.sex != "unknown" else None,
        )
        if part
    )
    return {
        "overview": (
            f"Record summary assembled from the chart{f' for a patient {who}' if who else ''}: "
            f"{len(facts.active_conditions)} active condition(s), "
            f"{len(facts.current_medications)} current medication(s), "
            f"{len(facts.recent_labs)} recent laboratory result(s) and "
            f"{len(facts.recent_encounters)} recorded visit(s). "
            "AI narrative is paused — this is the charted data, not a written summary of it."
        ),
        "active_problems": [_condition_line(row) for row in facts.active_conditions],
        "current_medications": [_medication_line(row) for row in facts.current_medications],
        "recent_investigations": [_lab_line(row) for row in facts.recent_labs],
        "recent_encounters": [_encounter_line(row) for row in facts.recent_encounters],
        "record_gaps": _gaps(facts),
    }


def _condition_line(row: dict[str, Any]) -> str:
    parts = [str(row["condition_name"])]
    if row.get("status"):
        parts.append(f"status {row['status']}")
    if row.get("onset_date"):
        parts.append(f"onset {row['onset_date']}")
    return " — ".join(parts)


def _medication_line(row: dict[str, Any]) -> str:
    name = row.get("generic_name") or row.get("brand_name_raw") or "unnamed medication"
    dose = " ".join(
        str(part) for part in (row.get("dose"), row.get("dose_unit"), row.get("frequency")) if part
    )
    return f"{name} {dose}".strip()


def _lab_line(row: dict[str, Any]) -> str:
    value = row.get("value_numeric")
    rendered = row.get("value_text") if value is None else value
    line = f"{row['marker_name']} {rendered if rendered is not None else '(no value)'}"
    if row.get("unit"):
        line += f" {row['unit']}"
    if row.get("sample_date"):
        line += f" ({row['sample_date']})"
    if row.get("is_abnormal"):
        line += " — flagged outside the stated reference range"
    return line


def _encounter_line(row: dict[str, Any]) -> str:
    parts = [str(row.get("encounter_date") or "undated")]
    if row.get("encounter_type"):
        parts.append(str(row["encounter_type"]))
    if row.get("presenting_complaint"):
        parts.append(str(row["presenting_complaint"]))
    return " — ".join(parts)


def _gaps(facts: ChartFacts) -> list[str]:
    """What a clinician reading this chart cannot tell from it.

    Deterministic, and stated even when a provider is available — the model is asked for the
    same field, but a gap is an absence, and asking a model to notice what is *not* in the text
    it was given is the least reliable thing one can ask it for.
    """
    gaps: list[str] = []
    if not facts.active_conditions:
        gaps.append("No active conditions are charted.")
    if not facts.current_medications:
        gaps.append("No current medications are charted.")
    if not facts.allergies:
        gaps.append(
            "No allergies are recorded. That is not the same as no allergies — nothing on this "
            "chart says the question was asked."
        )
    if not facts.recent_labs:
        gaps.append("No laboratory results are on file.")
    if not facts.recent_encounters:
        gaps.append("No visits are recorded, so there is no documented consultation history.")
    return gaps


def _prompt_text(facts: ChartFacts) -> str:
    """The chart as the model sees it: lines, not JSON.

    Rendered rather than dumped because a model reading ``{"value_numeric": null,
    "value_text": "positive"}`` writes about nulls. The renderers are the same ones the offline
    path uses, so the two summaries are written from identically shaped facts.
    """
    sections: list[tuple[str, list[str]]] = [
        (
            "Demographics",
            [
                line
                for line in (
                    f"Age: {facts.age_years} years" if facts.age_years is not None else None,
                    f"Sex: {facts.sex}" if facts.sex else None,
                )
                if line
            ],
        ),
        ("Active conditions", [_condition_line(row) for row in facts.active_conditions]),
        ("Current medications", [_medication_line(row) for row in facts.current_medications]),
        (
            "Allergies",
            [
                " — ".join(
                    str(part)
                    for part in (
                        row["allergen_name"],
                        row.get("severity"),
                        row.get("reaction_description"),
                    )
                    if part
                )
                for row in facts.allergies
            ],
        ),
        ("Recent laboratory results", [_lab_line(row) for row in facts.recent_labs]),
        ("Recent visits", [_encounter_line(row) for row in facts.recent_encounters]),
    ]
    blocks = [
        f"{title}:\n" + "\n".join(f"- {line}" for line in lines)
        for title, lines in sections
        if lines
    ]
    return "\n\n".join(blocks) if blocks else "Nothing on file."


def _serialise(facts: ChartFacts) -> dict[str, Any]:
    return {
        "age_years": facts.age_years,
        "sex": facts.sex,
        "active_conditions": facts.active_conditions,
        "current_medications": facts.current_medications,
        "recent_labs": facts.recent_labs,
        "recent_encounters": facts.recent_encounters,
        "allergies": facts.allergies,
    }
