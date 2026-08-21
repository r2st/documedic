"""Discharge summaries: the readiness gate, the reconciliation, and the chart write.

This is the service that makes a discharge summary a record rather than a document. Three
things happen here that do not happen anywhere else in the codebase:

**The chart is told.** ``finalize`` writes ``medication_events`` for every line the
reconciliation says has changed — starts for new drugs, changes for new doses, stops for
discontinuations — and retires the rows a stop contradicts, the same way
``GraphService._retire_current`` does for a merged prescription. Without it the patient goes
home on the new list and ``medication_events`` goes on carrying the admission's, so every
safety check at the next visit runs against a list that has been wrong since the day they left.
That is the defect this whole feature is about, and everything else here is the safety
apparatus around it.

**Nothing is written until every discontinuation is named.** ``app.core.med_reconciliation`` is
explicit that a ``stop`` disposition is a statement about two lists rather than an instruction —
it means the discharge list does not carry a drug the chart calls current, which is an intended
discontinuation about half the time and a line somebody forgot to type the other half. So
``finalize`` recomputes the stop set and compares it against ``confirmed_stops``, refusing on
any difference in either direction. Same recompute-and-compare ``HandoffService.send`` performs;
same reasoning, which is that a confirmation of a state that no longer holds is worse than no
confirmation because it is a signed statement that somebody checked.

**All of it or none of it.** A hard block, an unidentifiable drug name, an unacknowledged panic
value or a missing diagnosis refuses the whole finalisation with nothing written — not the
offending line. A discharge is one clinical act, and half-charting it leaves a record that reads
as a completed discharge with one medicine quietly missing, which is strictly worse than the
list the clinician started with. Same judgement ``ProtocolService.apply`` makes about an order
set, for the same reason.

The readiness rules themselves are in ``app.core.discharge`` — pure, offline, and pinned as such
by ``test_offline_engine_purity``. Deciding whether it is safe to send a patient home is the
last thing that should depend on a provider being reachable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discharge import (
    REQUIRED_SECTIONS,
    ChartAction,
    DischargeFacts,
    ReadinessItem,
    assess_readiness,
    blocking_items,
    chart_actions,
    stop_labels,
)
from app.core.med_reconciliation import ReconciliationFlag, ReconciliationLine
from app.core.scheduling import OCCUPYING_STATUSES
from app.exceptions import (
    DischargeFinalizedError,
    DischargeNotReadyError,
    DischargeStopsUnconfirmedError,
    EncounterNotFoundError,
    NotFoundError,
    ValidationError,
)
from app.models.appointment import Appointment
from app.models.discharge_summary import DischargeSummary
from app.models.document import Document
from app.models.encounter import Encounter
from app.models.medication_event import MedicationEvent
from app.services.audit_service import AuditService
from app.services.lab_safety_service import LabSafetyService
from app.services.med_reconciliation_service import (
    DrugSafetyFinding,
    MedReconciliationService,
    ProposedLine,
)
from app.services.patient_service import PatientService
from app.services.safety_service import SafetyService

# How many discharge summaries a chart's list read returns. A chart with more than this many is
# a chart with a data problem rather than a patient with fifty admissions on this system.
MAX_LIST = 50


@dataclass(frozen=True)
class DischargeAssessment:
    """Everything ``preview`` computes, and everything ``finalize`` re-computes before writing."""

    readiness: list[ReadinessItem] = field(default_factory=list)
    actions: list[ChartAction] = field(default_factory=list)
    lines: list[ReconciliationLine] = field(default_factory=list)
    list_flags: list[ReconciliationFlag] = field(default_factory=list)
    safety_findings: list[DrugSafetyFinding] = field(default_factory=list)
    charted_count: int = 0
    proposed_count: int = 0
    reconciled_count: int = 0

    @property
    def is_ready(self) -> bool:
        return not blocking_items(self.readiness)

    @property
    def stops(self) -> tuple[str, ...]:
        return stop_labels(self.actions)


class DischargeService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)
        self.reconciliation = MedReconciliationService(db)
        self.safety = SafetyService(db)

    # --- Lifecycle -----------------------------------------------------------------------

    async def create(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        encounter_id: uuid.UUID | None,
        medications: list[ProposedLine],
        **sections: str | None,
    ) -> DischargeSummary:
        """Start a summary. Nothing is frozen, nothing is charted, nothing is required yet."""
        await PatientService(self.db).get(account_id, patient_id)
        if encounter_id is not None:
            await self._assert_encounter(patient_id, encounter_id)

        summary = DischargeSummary(
            account_id=account_id,
            patient_id=patient_id,
            encounter_id=encounter_id,
            status="draft",
            discharge_medications=[_med_dict(line) for line in medications],
            **sections,
        )
        self.db.add(summary)
        await self.db.flush()
        await self.audit.record(
            action="discharge_summary_created",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="discharge_summary",
            entity_id=summary.id,
            # Counts and shape, never the prose or the drug names. The trail is append-only,
            # unencrypted and never pruned, and ``entity_id`` leads to the row that holds them —
            # see tests/test_audit_payload_free_text.py.
            payload={
                "status": "draft",
                "medication_count": len(medications),
                "linked_encounter": encounter_id is not None,
            },
        )
        return summary

    async def get(self, patient_id: uuid.UUID, summary_id: uuid.UUID) -> DischargeSummary:
        """The row, checked against the chart it is claimed to be on.

        Chart-scoped, not account-scoped: every caller has already established that this account
        owns the chart, either through ``PatientService.get`` or through :meth:`read`. Same
        division of labour as ``HandoffService.get``.
        """
        summary = await self.db.get(DischargeSummary, summary_id)
        if summary is None or summary.is_deleted or summary.patient_id != patient_id:
            raise NotFoundError(f"Discharge summary {summary_id} not found on this patient")
        return summary

    async def read(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, summary_id: uuid.UUID
    ) -> DischargeSummary:
        """One summary, with the tenancy check first.

        The chart check comes before the summary lookup so that a caller cannot distinguish a
        summary id that does not exist from one on a chart they do not own — and cannot *time*
        the difference either, which a plain 404-for-both does not cover on its own.
        """
        await PatientService(self.db).get(account_id, patient_id)
        return await self.get(patient_id, summary_id)

    async def list_for_patient(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, limit: int = MAX_LIST
    ) -> list[DischargeSummary]:
        await PatientService(self.db).get(account_id, patient_id)
        rows = await self.db.execute(
            select(DischargeSummary)
            .where(
                DischargeSummary.patient_id == patient_id,
                DischargeSummary.is_deleted.is_(False),
            )
            # ``id`` breaks the tie. ``created_at`` has one-second resolution on SQLite, so two
            # summaries drafted in the same second would otherwise page in an order that varies
            # between reads — the defect fixed for the patient list in an earlier round.
            .order_by(DischargeSummary.created_at.desc(), DischargeSummary.id)
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def update(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        summary_id: uuid.UUID,
        encounter_id: uuid.UUID | None,
        medications: list[ProposedLine] | None,
        **sections: str | None,
    ) -> DischargeSummary:
        """Edit a draft. Refused once finalised — a correction is a new summary.

        ``medications`` replaces the list wholesale when supplied. A partial update of a
        medication list has no safe reading: "these three changed" leaves every other line
        ambiguous between unchanged and removed, and completeness is the entire value of the
        list.
        """
        await PatientService(self.db).get(account_id, patient_id)
        summary = await self.get(patient_id, summary_id)
        self._assert_draft(summary, "edit")

        if encounter_id is not None:
            await self._assert_encounter(patient_id, encounter_id)
            summary.encounter_id = encounter_id
        for name, value in sections.items():
            if value is not None:
                setattr(summary, name, value)
        if medications is not None:
            summary.discharge_medications = [_med_dict(line) for line in medications]

        await self.db.flush()
        await self.audit.record(
            action="discharge_summary_updated",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="discharge_summary",
            entity_id=summary.id,
            payload={
                "status": summary.status,
                "medication_count": len(summary.discharge_medications),
                "sections_written": sorted(k for k, v in sections.items() if v is not None),
            },
        )
        return summary

    # --- The assessment ------------------------------------------------------------------

    async def assess(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, summary: DischargeSummary
    ) -> DischargeAssessment:
        """Reconcile the stored list against the chart and apply the readiness rules.

        Writes nothing to the discharge summary and nothing to the chart. It does persist
        ``drug_safety_checks`` rows, because it runs the ordinary per-drug ``check_medication``
        path for every take-home medicine — which is deliberate and is what makes a hard block
        raised here overridable: Critical Safety Rule #3 says the only way past one is an
        override naming the check that raised it, and a block with no check id behind it would
        be a refusal the clinician has no route around except to go and re-check the drug singly
        on another screen.
        """
        proposed = [
            ProposedLine(
                name=row.get("name", ""),
                dose=row.get("dose"),
                dose_unit=row.get("dose_unit"),
                frequency=row.get("frequency"),
            )
            for row in summary.discharge_medications
            if row.get("name")
        ]
        if not proposed:
            # An empty take-home list against a non-empty chart is not a discharge, it is a
            # request to stop everything, and ``MedReconciliationService`` refuses it for the
            # same reason — every current medication would come back as an omission, and a
            # screenful of high-risk-omission flags produced by a client that failed to send its
            # list is how a clinician learns to dismiss them.
            raise ValidationError(
                "A discharge summary needs at least one take-home medication before it can be "
                "reconciled. To record that the patient is going home on nothing, stop each "
                "current medication individually so every discontinuation carries its reason."
            )

        result = await self.reconciliation.reconcile(
            account_id=account_id,
            patient_id=patient_id,
            proposed=proposed,
            context="discharge",
        )
        actions = chart_actions(result.lines)
        facts = DischargeFacts(
            unacknowledged_critical_labs=await self._unacknowledged_critical_labs(
                account_id=account_id, patient_id=patient_id
            ),
            hard_blocks=sum(1 for f in result.safety_findings if f.flag.is_hard_block),
            unresolved_discharge_medications=tuple(result.unresolved_proposed),
            unresolved_charted_medications=tuple(result.unresolved_charted),
            high_risk_omissions=tuple(
                str(flag.details.get("drug") or flag.summary)
                for flag in result.list_flags
                if flag.finding == "high_risk_omission"
            ),
            follow_up_booked=await self._has_future_appointment(patient_id),
            follow_up_instructions=bool((summary.follow_up_instructions or "").strip()),
            documents_needing_confirmation=await self._count(
                Document,
                Document.patient_id == patient_id,
                Document.is_deleted.is_(False),
                Document.extraction_status == "needs_confirmation",
            ),
            missing_sections=tuple(
                section
                for section in REQUIRED_SECTIONS
                if not (getattr(summary, section, None) or "").strip()
            ),
        )
        return DischargeAssessment(
            readiness=assess_readiness(facts),
            actions=actions,
            lines=result.lines,
            list_flags=result.list_flags,
            safety_findings=result.safety_findings,
            charted_count=result.charted_count,
            proposed_count=len(proposed),
            reconciled_count=result.reconciled_line_count,
        )

    async def preview(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, summary_id: uuid.UUID
    ) -> tuple[DischargeSummary, DischargeAssessment]:
        """What finalising would do. Available on a finalised summary too, as a re-read.

        A finalised summary's *stored* snapshot is the record and is what
        ``GET ../discharge-summaries/{id}`` returns. This recomputes against today's chart, which
        is a different and occasionally important question: "would this discharge still be safe
        now" is what a clinician asks when a result lands after the patient has left.
        """
        await PatientService(self.db).get(account_id, patient_id)
        summary = await self.get(patient_id, summary_id)
        assessment = await self.assess(
            account_id=account_id, patient_id=patient_id, summary=summary
        )
        await self.audit.record(
            action="discharge_summary_previewed",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="discharge_summary",
            entity_id=summary.id,
            payload={
                # A preview runs the whole deterministic engine over the chart and returns what
                # stands against it, which is a disclosure of this patient's allergies,
                # interactions and outstanding results — recorded for the same reason
                # ``protocol_previewed`` is.
                "status": summary.status,
                "is_ready": assessment.is_ready,
                "blocking_count": len(blocking_items(assessment.readiness)),
                "advisory_count": len(assessment.readiness)
                - len(blocking_items(assessment.readiness)),
                "stop_count": len(assessment.stops),
                "proposed_count": assessment.proposed_count,
                "charted_count": assessment.charted_count,
            },
        )
        return summary, assessment

    # --- Finalisation --------------------------------------------------------------------

    async def finalize(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        summary_id: uuid.UUID,
        finalized_by: str,
        confirmed_stops: list[str],
        supersedes_id: uuid.UUID | None,
        correction_reason: str | None,
    ) -> tuple[DischargeSummary, DischargeAssessment]:
        """Attest to the list, write the chart, and freeze the document.

        Refused, with nothing written, when:

        * any readiness item is blocking (409 `discharge_not_ready`) — an unacknowledged panic
          value, a hard block, an unidentifiable take-home drug, or a missing diagnosis or
          hospital course;
        * the confirmed discontinuations do not match the recomputed set exactly
          (409 `discharge_stops_unconfirmed`), in either direction;
        * the summary has already been finalised (409 `discharge_finalized`).

        Then, and only then, the chart is written: one ``start`` event per new drug, one
        ``change`` per new dose with the superseded row retired, one ``stop`` per confirmed
        discontinuation with the rows it contradicts retired. ``continue`` writes nothing — the
        chart already carries that drug at that dose, and a second identical row would surface
        in the duplicate-therapy check as the patient being on it twice.
        """
        await PatientService(self.db).get(account_id, patient_id)
        summary = await self.get(patient_id, summary_id)
        self._assert_draft(summary, "finalize")

        if (supersedes_id is None) != (correction_reason is None):
            raise ValidationError(
                "A correction names both the summary it supersedes and the reason it was wrong. "
                "A pointer with no reason is a replacement nobody had to justify, and a reason "
                "pointing at nothing is a note."
            )
        if supersedes_id is not None:
            superseded = await self.get(patient_id, supersedes_id)
            if superseded.status != "finalized":
                raise ValidationError(
                    "Only a finalised discharge summary can be superseded. A draft is edited."
                )

        assessment = await self.assess(
            account_id=account_id, patient_id=patient_id, summary=summary
        )
        blocking = blocking_items(assessment.readiness)
        if blocking:
            raise DischargeNotReadyError(
                "This discharge cannot be finalised while the record still carries an "
                "unresolved blocking item. Each is listed with what it is about; a hard block "
                "is passed by recording an override with your reasoning on the drug-safety "
                "screen.",
                detail="; ".join(f"{item.key}: {item.summary}" for item in blocking),
            )

        required = set(assessment.stops)
        confirmed = {name.strip().lower() for name in confirmed_stops if name.strip()}
        if confirmed != required:
            raise DischargeStopsUnconfirmedError(
                detail=(
                    f"stop confirmation mismatch: confirmed={sorted(confirmed)} "
                    f"required={sorted(required)}"
                )
            )

        event_ids = await self._write_chart(
            account_id=account_id,
            patient_id=patient_id,
            summary=summary,
            actions=assessment.actions,
            finalized_by=finalized_by,
        )

        summary.status = "finalized"
        summary.finalized_at = datetime.now(UTC)
        summary.finalized_by = finalized_by
        summary.finalized_by_account_id = account_id
        summary.supersedes_id = supersedes_id
        summary.correction_reason = correction_reason
        summary.readiness = [item.as_dict() for item in assessment.readiness]
        summary.confirmed_stops = sorted(required)
        summary.medication_event_ids = [str(event_id) for event_id in event_ids]
        summary.reconciliation = _reconciliation_snapshot(assessment)
        await self.db.flush()

        await self.audit.record(
            action="discharge_summary_finalized",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="discharge_summary",
            entity_id=summary.id,
            payload={
                # The clinician's name is the clinician's, not the patient's, and it is the
                # clinical meaning of the entry: who sent this patient home, when, on a list of
                # what shape. The drug names live on the medication events this points at, each
                # of which wrote its own ``drug_safety_check`` row.
                "finalized_by": finalized_by,
                "medication_count": len(summary.discharge_medications),
                "charted_count": assessment.charted_count,
                "events_written": len(event_ids),
                "stops_confirmed": len(required),
                "dispositions": _disposition_counts(assessment.actions),
                # What the clinician read and proceeded past. A finalised summary never carries
                # a blocking item, so this is the advisory count — which is exactly what a later
                # review asks about.
                "advisory_count": len(assessment.readiness),
                "is_correction": supersedes_id is not None,
            },
        )
        return summary, assessment

    async def _write_chart(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        summary: DischargeSummary,
        actions: list[ChartAction],
        finalized_by: str,
    ) -> list[uuid.UUID]:
        """Chart the discharge list. Every event carries the encounter and the prescriber.

        ``_retire_current`` on the ``stop`` and ``change`` paths is the half that is easy to
        omit and useless to omit: without it the new row lands beside the one it contradicts,
        the earlier "continue Warfarin 5 mg" is still ``is_current``, and the safety engine, the
        records list and every count derived from them still have the patient on the old dose.
        Exactly the failure ``GraphService._retire_current`` exists to prevent on the extraction
        path, reached here by a different route.
        """
        today = datetime.now(UTC).date()
        now = datetime.now(UTC)
        written: list[uuid.UUID] = []

        for action in actions:
            if not action.writes_to_chart:
                continue
            row = (
                await self.safety.resolver.resolve_reference_id(action.reference_id)
                if action.reference_id
                else None
            )
            if row is None:
                # Unreachable through the API: ``assess_readiness`` blocks the finalisation on
                # any unresolved name, and a ``stop`` line's drug came off the chart's own
                # resolved doses. Kept as a guard rather than an assertion because the resolver
                # reads the database and the two reads are not in one statement.
                raise ValidationError(  # pragma: no cover - defended by the readiness gate
                    f"{action.label!r} no longer resolves to a known drug; nothing was charted."
                )

            if action.kind in ("stop", "change"):
                await self._retire_current(patient_id, row.id)

            event = MedicationEvent(
                patient_id=patient_id,
                encounter_id=summary.encounter_id,
                drug_vocabulary_id=row.id,
                # The INN from the vocabulary row rather than the label the list was typed
                # under: the vocabulary is the authority on a drug's generic name, and a
                # discharge list spelled differently must not introduce a second spelling.
                generic_name=row.generic_name,
                dose=action.dose,
                dose_unit=action.dose_unit,
                frequency=action.frequency,
                event_type=action.kind,
                event_date=today,
                # ``is_current`` follows the rule the whole chart is driven by:
                # ``event_type != "stop"``. A stop row that claimed to be current is the
                # disagreement between ``event_type`` and ``is_current`` that migration 0022
                # was written to end.
                is_current=action.kind != "stop",
                prescriber_name=finalized_by,
                # A clinician wrote this list and attested to it by finalising, which is what
                # this column means — unlike an extraction, where it records that a human
                # checked a transcription against the original page.
                clinician_confirmed=True,
                clinician_confirmed_at=now,
            )
            self.db.add(event)
            await self.db.flush()
            written.append(event.id)

        return written

    async def _retire_current(self, patient_id: uuid.UUID, vocabulary_id: uuid.UUID) -> None:
        """Take the patient off the drug the new event supersedes.

        Matched by vocabulary id, not by name and not by dose. Every action reaching here
        resolved through the vocabulary — the readiness gate refuses the finalisation otherwise
        — so there is no unresolved-name fallback to write, and matching on the id is what makes
        a discharge list written in generics retire rows charted under an Indian brand.
        """
        rows = await self.db.execute(
            select(MedicationEvent).where(
                MedicationEvent.patient_id == patient_id,
                MedicationEvent.is_deleted.is_(False),
                MedicationEvent.is_current.is_(True),
                MedicationEvent.drug_vocabulary_id == vocabulary_id,
            )
        )
        for row in rows.scalars().all():
            row.is_current = False

    # --- Chart facts ---------------------------------------------------------------------

    async def _unacknowledged_critical_labs(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID
    ) -> int:
        queue = await LabSafetyService(self.db).outstanding_critical_values(account_id=account_id)
        return sum(1 for lab, _flag in queue.entries if lab.patient_id == patient_id)

    async def _has_future_appointment(self, patient_id: uuid.UUID) -> bool:
        """Anything on the diary this patient is still expected at.

        ``scheduled`` only. A completed appointment is one they already attended and a cancelled
        one arranges nothing, so counting either would report a follow-up that does not exist —
        which is the specific claim this fact is used to make.
        """
        count = await self._count(
            Appointment,
            Appointment.patient_id == patient_id,
            Appointment.is_deleted.is_(False),
            Appointment.status.in_(OCCUPYING_STATUSES),
            Appointment.starts_at > datetime.now(UTC),
        )
        return count > 0

    async def _count(self, model: type, *predicates: ColumnElement[bool]) -> int:
        result = await self.db.execute(select(func.count()).select_from(model).where(*predicates))
        return int(result.scalar_one())

    async def _assert_encounter(self, patient_id: uuid.UUID, encounter_id: uuid.UUID) -> None:
        encounter = await self.db.get(Encounter, encounter_id)
        if encounter is None or encounter.is_deleted or encounter.patient_id != patient_id:
            raise EncounterNotFoundError(f"Encounter {encounter_id} not found on this patient")

    @staticmethod
    def _assert_draft(summary: DischargeSummary, verb: str) -> None:
        if summary.status != "draft":
            raise DischargeFinalizedError(
                detail=f"{verb} refused: discharge summary {summary.id} is {summary.status}"
            )


def _med_dict(line: ProposedLine) -> dict:
    return {
        "name": line.name,
        "dose": line.dose,
        "dose_unit": line.dose_unit,
        "frequency": line.frequency,
    }


def _disposition_counts(actions: list[ChartAction]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for action in actions:
        counts[action.kind] = counts.get(action.kind, 0) + 1
    return counts


def _reconciliation_snapshot(assessment: DischargeAssessment) -> dict:
    """The comparison as it stood, stored on the finalised row and never recomputed.

    Deliberately holds the *lines and flags*, not the per-patient safety findings. Those were
    each persisted as their own ``drug_safety_checks`` row when ``check_medication`` ran, with
    their own ids, their own audit entries and their own override route; copying them in here
    would put a second, divergeable copy of a safety finding in a frozen JSON column, where an
    override recorded against the real one would never be reflected.
    """
    return {
        "lines": [
            {
                "disposition": line.disposition,
                "label": line.label,
                "summary": line.summary,
                "proposed_name": line.proposed_name,
                "charted_name": line.charted_name,
                "reference_id": line.reference_id,
                "proposed_dose": line.proposed_dose,
                "charted_dose": line.charted_dose,
            }
            for line in assessment.lines
        ],
        "list_flags": [
            {
                "finding": flag.finding,
                "severity": flag.severity,
                "summary": flag.summary,
            }
            for flag in assessment.list_flags
        ],
        "chart_actions": [action.as_dict() for action in assessment.actions],
        "charted_count": assessment.charted_count,
        "proposed_count": assessment.proposed_count,
        "reconciled_count": assessment.reconciled_count,
    }
