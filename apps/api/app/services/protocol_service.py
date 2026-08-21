"""Previewing and applying curated order sets against a real chart.

The templates are data (:mod:`app.core.order_sets`); this is what makes applying one safe.

**An order set gets exactly the checks a single prescription gets, plus one.** Every medication
in the selection goes through ``SafetyService.check_medication`` — the same call the prescribing
screen makes, so the same allergy, interaction, contraindication, renal, hepatic, dose-range,
age and weight-staleness rules run, and each hard block leaves a persisted
``drug_safety_checks`` row an override can name. The "plus one" is
``check_intra_list_interactions``: a template proposing two drugs that interact *with each
other* is a pair the per-drug checks cannot see, because neither is on the chart yet. That is
the same reasoning medication reconciliation makes about a proposed list, and the same pure
function does the work.

**A hard block refuses the whole application.** Not the blocked line — the application. A
protocol is one clinical act, and half-applying it leaves a chart that looks like a completed
workup with the contraindicated drug quietly missing. The clinician deselects the item and
applies again, or overrides the block on the prescribing screen where the override flow with
its documented reasoning already lives; this path deliberately does not grow a second one.

**The preview is recomputed at apply time.** A preview read at the start of a consultation may
be describing a chart that has since acquired an allergy, and the checks that matter are the
ones standing at the moment something is written. Same judgement as the handover checklist's
recompute, and the reasoning engine's re-check before publishing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.med_reconciliation import ProposedMedication, check_intra_list_interactions
from app.core.order_sets import (
    VERSION,
    FollowUpItem,
    InvestigationItem,
    MedicationItem,
    OrderSet,
    get_order_set,
    order_sets_for_condition,
)
from app.core.safety import DrugRef, SafetyFlag, ingredient_reference_ids
from app.exceptions import (
    HardBlockError,
    ProtocolTemplateNotFoundError,
    ProtocolTemplateUnusableError,
    ValidationError,
)
from app.models.condition import Condition
from app.models.medication_event import MedicationEvent
from app.models.protocol_application import ProtocolApplication
from app.services.appointment_service import AppointmentService
from app.services.audit_service import AuditService
from app.services.patient_service import PatientService
from app.services.safety_service import SafetyService, _drug_ref

_SEVERITY_RANK: dict[str, int] = {"hard_block": 0, "critical": 1, "warning": 2, "info": 3}


@dataclass(frozen=True)
class ProtocolFinding:
    """One safety finding raised by the selection, with the drug it concerns.

    ``drug`` is None for the chart-level notes and for the intra-list interaction findings,
    which are about a *pair* rather than about one line. ``check_id`` is non-None only where a
    ``drug_safety_checks`` row was persisted — which is the only handle an override has, so a
    hard block that arrived without one could not be acted on. Nothing chart-level and nothing
    intra-list is a hard block, which is what makes the None safe.
    """

    drug_label: str | None
    check_id: uuid.UUID | None
    flag: SafetyFlag


@dataclass(frozen=True)
class ProtocolPreview:
    order_set: OrderSet
    version: str
    selected_keys: tuple[str, ...]
    investigations: tuple[InvestigationItem, ...] = ()
    medications: tuple[MedicationItem, ...] = ()
    follow_ups: tuple[FollowUpItem, ...] = ()
    findings: list[ProtocolFinding] = field(default_factory=list)
    # Template medications the vocabulary could not identify. Never empty in a healthy
    # deployment — ``tests/test_protocol_templates.py`` refuses a template that has one — and
    # surfaced rather than swallowed because a drug that cannot be resolved cannot be checked.
    unresolved_medications: tuple[str, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return any(finding.flag.is_hard_block for finding in self.findings)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if not finding.flag.is_hard_block)


class ProtocolService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.safety = SafetyService(db)
        self.audit = AuditService(db)

    # --- Catalogue --------------------------------------------------------------------------

    def template(self, key: str) -> OrderSet:
        order_set = get_order_set(key)
        if order_set is None:
            raise ProtocolTemplateNotFoundError(detail=f"unknown protocol template {key!r}")
        return order_set

    async def suggested_for(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID
    ) -> tuple[OrderSet, ...]:
        """Templates whose condition this chart already carries.

        Matched against *documented* conditions only — never against a reasoning-engine
        differential. A template is an order set, and suggesting one from a hypothesis would
        put the panel's suspicion one click away from being charted as therapy, which is the
        automation bias this product is built to resist.
        """
        await PatientService(self.db).get(account_id, patient_id)
        rows = await self.db.execute(
            select(Condition.condition_name).where(
                Condition.patient_id == patient_id,
                Condition.is_deleted.is_(False),
            )
        )
        matched: dict[str, OrderSet] = {}
        for name in rows.scalars().all():
            for order_set in order_sets_for_condition(name or ""):
                matched[order_set.key] = order_set
        return tuple(matched[key] for key in sorted(matched))

    # --- Preview ----------------------------------------------------------------------------

    async def preview(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        key: str,
        selected_keys: list[str] | None,
    ) -> ProtocolPreview:
        """What this template would chart, and every safety finding standing against it.

        ``selected_keys`` of ``None`` means the whole template — the ordinary first read. An
        empty *list* is not the same thing and is refused: it is a request to apply nothing,
        which is almost always a client that failed to send its selection, and answering it
        with a clean preview of an empty set is the wrong direction to fail in.
        """
        await PatientService(self.db).get(account_id, patient_id)
        order_set = self.template(key)
        selection = self._resolve_selection(order_set, selected_keys)

        investigations = tuple(i for i in order_set.investigations if i.key in selection)
        medications = tuple(m for m in order_set.medications if m.key in selection)
        follow_ups = tuple(f for f in order_set.follow_ups if f.key in selection)

        findings, unresolved = await self._evaluate(
            account_id=account_id, patient_id=patient_id, medications=medications
        )
        return ProtocolPreview(
            order_set=order_set,
            version=VERSION,
            selected_keys=tuple(sorted(selection)),
            investigations=investigations,
            medications=medications,
            follow_ups=follow_ups,
            findings=findings,
            unresolved_medications=unresolved,
        )

    def _resolve_selection(self, order_set: OrderSet, selected_keys: list[str] | None) -> set[str]:
        available = set(order_set.item_keys())
        if selected_keys is None:
            return available
        chosen = {key.strip() for key in selected_keys if key and key.strip()}
        if not chosen:
            raise ValidationError(
                "No items were selected, so there is nothing to apply. Choose at least one "
                "investigation, medication or follow-up from the template.",
                detail=f"empty selection for template {order_set.key!r}",
            )
        unknown = sorted(chosen - available)
        if unknown:
            raise ValidationError(
                "Some of the selected items are not part of this template. Reload the template "
                "and choose again.",
                # The keys are curated identifiers from a shared file, not patient data, so
                # naming them in the log is safe and is what makes a stale client diagnosable.
                detail=f"template {order_set.key!r} has no items {unknown}",
            )
        return chosen

    async def _evaluate(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        medications: tuple[MedicationItem, ...],
    ) -> tuple[list[ProtocolFinding], tuple[str, ...]]:
        """Every safety finding the selection raises: per drug, across the list, and chart-level."""
        resolved: list[tuple[MedicationItem, DrugRef]] = []
        unresolved: list[str] = []
        for item in medications:
            match = await self.safety.resolver.resolve(item.generic_name)
            row = (
                await self.safety.resolver.resolve_reference_id(match.reference_id)
                if match is not None
                else None
            )
            if row is None:
                unresolved.append(item.generic_name)
                continue
            resolved.append((item, _drug_ref(row)))

        findings: list[ProtocolFinding] = []
        if not resolved:
            # No medication selected (an investigation-only template, or an investigations-only
            # selection). The chart-level notes are still worth returning — "this chart has an
            # allergy the vocabulary cannot identify" bears on ordering a workup too — but there
            # is nothing to build a drug context around, so they are read directly.
            chart_level = await self.safety.chart_completeness_flags(patient_id)
            findings.extend(
                ProtocolFinding(drug_label=None, check_id=None, flag=flag) for flag in chart_level
            )
            return _ordered(findings), tuple(unresolved)

        # The per-drug half. Each call persists its checks, so every hard block carries the id
        # an override names, exactly as the single-drug prescribing screen does.
        #
        # ``check_medication`` appends the chart-level notes to *every* drug it evaluates,
        # because on that screen it is the only place a clinician would see them. Repeated once
        # per template line they would bury the per-drug findings, so they are subtracted by
        # value and re-added once — the same treatment, and the same reasoning, as
        # ``MedReconciliationService.reconcile``.
        chart_level = await self.safety.chart_completeness_flags(patient_id)
        for item, drug in resolved:
            _vocab, _context, flags, check_ids = await self.safety.check_medication(
                account_id=account_id,
                patient_id=patient_id,
                drug_reference_id=drug.reference_id,
                drug_name=None,
                dose=item.typical_dose,
                dose_unit=item.dose_unit,
                frequency=item.frequency,
            )
            for flag, check_id in zip(flags, check_ids, strict=True):
                if flag in chart_level:
                    continue
                findings.append(
                    ProtocolFinding(drug_label=drug.generic_name, check_id=check_id, flag=flag)
                )

        # The pair the per-drug pass cannot see: two drugs in the *same template* that interact
        # with each other. Neither is on the chart yet, so neither appears in the other's
        # context — and a curated set that pairs them is exactly the mistake a template can make
        # once and then repeat on every patient it is applied to.
        proposed_ids: set[str] = set()
        for _item, drug in resolved:
            proposed_ids |= ingredient_reference_ids(drug)
        ctx = await self.safety.build_context(patient_id, proposed_reference_ids=proposed_ids)
        intra_list = check_intra_list_interactions(
            [
                ProposedMedication(
                    name=item.generic_name,
                    drug=drug,
                    dose=item.typical_dose,
                    dose_unit=item.dose_unit,
                    frequency=item.frequency,
                )
                for item, drug in resolved
            ],
            ctx.interaction_rules,
        )
        findings.extend(
            ProtocolFinding(
                drug_label=None,
                check_id=None,
                flag=SafetyFlag(
                    check_type="drug_interaction",
                    severity=flag.severity,
                    summary=flag.summary,
                    details=flag.details,
                    # Never a hard block from this path even where the underlying rule is one:
                    # an intra-list finding has no persisted check row, so nothing could
                    # override it, and a block a clinician cannot pass is a dead end rather
                    # than a safety control. The pair is surfaced at critical severity and the
                    # per-drug check raises the real block once one of the two is charted.
                    is_hard_block=False,
                ),
            )
            for flag in intra_list
        )

        findings.extend(
            ProtocolFinding(drug_label=None, check_id=None, flag=flag) for flag in chart_level
        )
        return _ordered(findings), tuple(unresolved)

    # --- Apply ------------------------------------------------------------------------------

    async def apply(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        key: str,
        selected_keys: list[str] | None,
        applied_by: str | None,
        encounter_id: uuid.UUID | None,
        follow_up_provider_name: str | None,
        follow_up_starts_at: datetime | None,
    ) -> tuple[ProtocolApplication, ProtocolPreview]:
        """Chart the selection, all of it or none of it.

        Refused, with nothing written, when:

        * a hard block stands against any selected medication (409 `hard_block`) — see the
          module docstring for why the block is not simply dropped from the list;
        * a selected medication cannot be resolved to a known drug (409
          `protocol_template_unusable`) — charting a medication nothing can evaluate is the
          failure this codebase has closed twice already;
        * the follow-up booking clashes with an existing appointment (409
          `appointment_conflict`, raised by ``AppointmentService``).

        The last of those is the one that looks harsh. It is the same all-or-nothing rule: a
        protocol whose medications were charted and whose review was not is a chart that reads
        as a completed plan with the recall silently missing, and the recall is the part nobody
        notices is absent.
        """
        preview = await self.preview(
            account_id=account_id, patient_id=patient_id, key=key, selected_keys=selected_keys
        )
        if preview.unresolved_medications:
            raise ProtocolTemplateUnusableError(
                detail=(
                    f"template {key!r} names unresolvable drugs "
                    f"{sorted(preview.unresolved_medications)}"
                )
            )
        if preview.is_blocked:
            blocked = [f.flag.summary for f in preview.findings if f.flag.is_hard_block]
            raise HardBlockError(
                "A safety hard block stands against one of the medications in this template, so "
                "nothing was charted. Deselect that medication and apply the rest, or record an "
                "override with your reasoning on the drug-safety screen first.",
                detail=f"template {key!r} blocked: {'; '.join(blocked)}",
            )

        today = datetime.now(UTC).date()
        medication_ids: list[str] = []
        for item in preview.medications:
            match = await self.safety.resolver.resolve(item.generic_name)
            row = (
                await self.safety.resolver.resolve_reference_id(match.reference_id)
                if match is not None
                else None
            )
            # Cannot be None: ``preview`` refused above on any unresolved medication. Kept as a
            # guard rather than an assertion because the resolver reads the database and the
            # two reads are not in the same statement.
            if row is None:  # pragma: no cover - defended by the refusal above
                raise ProtocolTemplateUnusableError(
                    detail=f"template {key!r} medication {item.generic_name!r} vanished mid-apply"
                )
            event = MedicationEvent(
                patient_id=patient_id,
                encounter_id=encounter_id,
                drug_vocabulary_id=row.id,
                # The INN, from the vocabulary row rather than from the template's own string:
                # the vocabulary is the authority on a drug's generic name, and a template that
                # spelled it differently must not introduce a second spelling into the chart.
                generic_name=row.generic_name,
                dose=item.typical_dose,
                dose_unit=item.dose_unit,
                frequency=item.frequency,
                route=item.route,
                event_type="start",
                event_date=today,
                is_current=True,
                prescriber_name=applied_by,
                # A clinician selected this item and applied it, which is an attestation in the
                # sense this column means — unlike an extraction, where the flag says a human
                # has checked the transcription against the original.
                clinician_confirmed=True,
                clinician_confirmed_at=datetime.now(UTC),
            )
            self.db.add(event)
            await self.db.flush()
            medication_ids.append(str(event.id))

        appointment_id: uuid.UUID | None = None
        if preview.follow_ups and follow_up_provider_name:
            follow_up = preview.follow_ups[0]
            starts_at = follow_up_starts_at or _default_follow_up_slot(today, follow_up)
            appointment = await AppointmentService(self.db).book(
                account_id=account_id,
                patient_id=patient_id,
                provider_name=follow_up_provider_name,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(minutes=settings.protocol_follow_up_duration_minutes),
                appointment_type=follow_up.appointment_type,
                reason=follow_up.label,
                source_protocol_key=key,
                encounter_id=encounter_id,
            )
            appointment_id = appointment.id

        application = ProtocolApplication(
            account_id=account_id,
            patient_id=patient_id,
            encounter_id=encounter_id,
            template_key=preview.order_set.key,
            template_version=preview.version,
            template_title=preview.order_set.title,
            selected_keys=list(preview.selected_keys),
            ordered_investigations=[
                {"key": item.key, "label": item.label, "marker_name": item.marker_name}
                for item in preview.investigations
            ],
            medication_event_ids=medication_ids,
            follow_up_appointment_id=appointment_id,
            applied_by=applied_by,
            warning_count=preview.warning_count,
        )
        self.db.add(application)
        await self.db.flush()

        await self.audit.record(
            action="protocol_applied",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="protocol_application",
            entity_id=application.id,
            payload={
                # Curated identifiers and counts. The template key and version are shared
                # reference data rather than anything about this patient; the drug names it
                # charted are the patient's prescribing and live on the medication events the
                # application points at, each of which wrote its own ``drug_safety_check`` entry.
                "template_key": application.template_key,
                "template_version": application.template_version,
                "selected_count": len(application.selected_keys),
                "investigations_ordered": len(application.ordered_investigations),
                "medications_charted": len(medication_ids),
                "follow_up_booked": appointment_id is not None,
                # Non-blocking findings the clinician proceeded past. A hard block never
                # reaches here, so this is the count of what there was to read.
                "warning_count": preview.warning_count,
                "applied_by": applied_by,
            },
        )
        return application, preview

    async def list_applications(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, limit: int
    ) -> list[ProtocolApplication]:
        await PatientService(self.db).get(account_id, patient_id)
        rows = await self.db.execute(
            select(ProtocolApplication)
            .where(ProtocolApplication.patient_id == patient_id)
            .order_by(ProtocolApplication.created_at.desc(), ProtocolApplication.id)
            .limit(limit)
        )
        return list(rows.scalars().all())


def _ordered(findings: list[ProtocolFinding]) -> list[ProtocolFinding]:
    """Hard blocks first, then by severity, then stably by drug.

    The same order the single-drug screen and the reconciliation report use, for the same
    reason: the list is read top-down and the thing that stops a prescription belongs at the
    top of it.
    """
    findings.sort(
        key=lambda f: (
            not f.flag.is_hard_block,
            _SEVERITY_RANK.get(f.flag.severity, 9),
            f.drug_label or "",
        )
    )
    return findings


def _default_follow_up_slot(today: date, follow_up: FollowUpItem) -> datetime:
    """The template's interval, landed at the clinic's configured local hour.

    Computed rather than left to the caller because the ordinary application supplies no time —
    "review in twelve weeks" is a date, not an appointment — and a follow-up booked at whatever
    o'clock the request happened to be made is a booking somebody has to move.
    """
    local_midnight = datetime.combine(
        today + timedelta(days=follow_up.interval_days), time(0, 0), tzinfo=UTC
    )
    return (
        local_midnight
        + timedelta(hours=settings.protocol_follow_up_local_hour)
        - timedelta(minutes=settings.clinic_utc_offset_minutes)
    )
