"""Medication reconciliation across a transition of care.

Wraps :mod:`app.core.med_reconciliation` with the two things a pure module cannot have: the
chart, and the drug vocabulary that turns a written name into an identity rules are keyed on.

The division of labour is deliberate and worth stating, because a reconciliation report that
answered only half of it would read as if it had answered both:

* **List against list** — ``app.core.med_reconciliation``. Which drugs continue, start, stop or
  change dose; what interacts or duplicates *within* the proposed list. Facts about two lists.
* **List against patient** — ``app.services.safety_service.SafetyService``, run once per
  resolved proposed drug. Allergies, contraindications, renal and hepatic thresholds, dose
  ranges. Facts about this patient, and the half that produces hard blocks.

Both halves run on every reconciliation. Running only the first would produce a tidy table with
an anaphylaxis in it; running only the second is what the per-drug endpoint already does, and is
exactly what misses an omission.

Every proposed drug therefore goes through the *ordinary* ``check_medication`` path, which means
each one persists its own ``DrugSafetyCheck`` rows and writes its own audit entry. That is not
incidental bookkeeping: a hard block raised here has to carry a check id, because the only way
past a hard block is ``POST ../override`` with written reasoning against that id (Critical
Safety Rule #3), and a reconciliation that raised blocks nobody could act on would push
clinicians to go and re-check the drug singly on the screen that does issue them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.med_reconciliation import (
    UNRESOLVED_DISPOSITIONS,
    ChartedCurrentMedication,
    ProposedMedication,
    ReconciliationFlag,
    ReconciliationLine,
    proposed_reference_ids,
    reconcile_medications,
)
from app.core.safety import DrugRef, SafetyContext, SafetyFlag
from app.exceptions import ValidationError
from app.services.audit_service import AuditService
from app.services.patient_service import PatientService

# ``_drug_ref`` rather than a second converter here: a ``DrugRef`` built without ``components``
# silently un-does the fixed-dose-combination pass, so there is exactly one construction site
# for it and this module uses that one.
from app.services.safety_service import SafetyService, _drug_ref

# Ceiling on one reconciliation request. A discharge summary from an Indian tertiary hospital
# runs to a dozen drugs and a polypharmacy outpatient chart to twenty; fifty is comfortably past
# any real list and short of the point where the O(n²) intra-list passes matter.
#
# Stated here as well as on the request schema because the two bound different things. The
# schema refuses an over-long list at the API edge; this is what the *service* contract promises
# every other caller, including the ones that do not come through a Pydantic model.
MAX_PROPOSED_MEDICATIONS = 50


@dataclass(frozen=True)
class ProposedLine:
    """One requested line, as submitted."""

    name: str
    dose: str | None = None
    dose_unit: str | None = None
    frequency: str | None = None


@dataclass(frozen=True)
class DrugSafetyFinding:
    """One per-patient safety flag, with the drug it is about and the check id to override it.

    ``drug`` is None for the chart-level notes, which are statements about the record rather
    than about any one medication — the same meaning ``SafetyFlagResponse.drug_reference_id``
    gives a null, so the two line up over the wire without a second convention.

    ``check_id`` is None for those same notes and non-None for everything else. That is not a
    cosmetic difference: a hard block is only overridable through the id of the persisted
    ``drug_safety_checks`` row it came from, so a finding that arrives without one cannot be
    acted on. Nothing chart-level is ever a hard block, which is what makes the None safe here.
    """

    drug: DrugRef | None
    check_id: uuid.UUID | None
    flag: SafetyFlag


@dataclass(frozen=True)
class Reconciliation:
    lines: list[ReconciliationLine] = field(default_factory=list)
    list_flags: list[ReconciliationFlag] = field(default_factory=list)
    safety_findings: list[DrugSafetyFinding] = field(default_factory=list)
    # Names on the proposed list the vocabulary could not identify. Duplicated out of ``lines``
    # (where each is an ``unresolved_proposed`` row) so the response can lead with the count:
    # "reconciled 9 of 11" is the first thing a clinician needs to know about a report that
    # otherwise reads as complete.
    unresolved_proposed: list[str] = field(default_factory=list)
    unresolved_charted: list[str] = field(default_factory=list)
    # How many current medications the chart held. Computed by the service rather than derived
    # from ``lines`` by each caller: the charted side of the comparison includes the rows that
    # resolved to nothing, and a caller counting only the lines it recognised would report a
    # smaller chart than the one actually reconciled against.
    charted_count: int = 0

    @property
    def has_hard_block(self) -> bool:
        return any(f.flag.is_hard_block for f in self.safety_findings)

    @property
    def reconciled_line_count(self) -> int:
        """Lines where two lists were actually compared against each other."""
        return sum(1 for line in self.lines if line.disposition not in UNRESOLVED_DISPOSITIONS)


class MedReconciliationService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.safety = SafetyService(db)
        self.audit = AuditService(db)

    async def reconcile(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        proposed: list[ProposedLine],
        context: str,
    ) -> Reconciliation:
        """Reconcile a proposed medication list against this patient's current medications.

        ``context`` is the transition this is happening at (admission/discharge/transfer/
        outpatient review) and is recorded on the trail. It changes no logic — the comparison is
        the same comparison — and exists because "who reconciled this chart, when, and at what
        transition" is the question asked of a reconciliation months later, and the transition is
        not recoverable from the drug list.

        Does not commit; the router owns the transaction boundary, as every service here does.
        """
        if not proposed:
            # An empty proposed list against a non-empty chart is not a reconciliation, it is a
            # request to stop everything, and it would be reported as exactly that: every
            # current medication as an omission. Refused rather than answered, because the
            # overwhelmingly likelier cause is a client that failed to send its list, and a
            # screen full of high-risk-omission flags produced by a serialisation bug is how a
            # clinician learns to dismiss them.
            raise ValidationError(
                "A reconciliation needs at least one proposed medication. To record that every "
                "current medication is being stopped, stop them individually so each carries "
                "its own reason."
            )
        if len(proposed) > MAX_PROPOSED_MEDICATIONS:
            raise ValidationError(
                f"A reconciliation takes at most {MAX_PROPOSED_MEDICATIONS} proposed "
                f"medications; {len(proposed)} were supplied."
            )

        # Raises PatientNotFoundError for a chart this account does not own, before any drug
        # name is resolved. The tenancy check has to come first: resolving names against the
        # shared vocabulary for a patient the caller cannot read leaks nothing, but *timing* the
        # reconciliation of a chart that does not exist against one that does is a difference a
        # caller can measure.
        await PatientService(self.db).get(account_id, patient_id)

        resolved_lines: list[ProposedMedication] = []
        for line in proposed:
            vocab = await self.safety.resolver.resolve(line.name)
            drug = None
            if vocab is not None:
                # Re-read through the reference id to get the full vocabulary row: ``resolve``
                # returns the match, and the ingredient list a combination product is evaluated
                # by hangs off the row. Resolving to a combination and then reconciling it as a
                # single opaque product is the exact failure ``_ingredients`` exists to prevent.
                row = await self.safety.resolver.resolve_reference_id(vocab.reference_id)
                if row is not None:
                    drug = _drug_ref(row)
            resolved_lines.append(
                ProposedMedication(
                    name=line.name,
                    drug=drug,
                    dose=line.dose,
                    dose_unit=line.dose_unit,
                    frequency=line.frequency,
                )
            )

        # Scoped to the proposed drugs as well as the charted ones. A rule whose *both* sides are
        # new is loaded by neither the chart's ids nor a single proposal's, and that rule is
        # precisely the intra-list interaction this whole path exists to find — so the scope is
        # built from the same identity set the check walks. See
        # ``med_reconciliation.proposed_reference_ids``.
        ctx = await self.safety.build_context(
            patient_id, proposed_reference_ids=proposed_reference_ids(resolved_lines)
        )
        charted = _charted_from_context(ctx)

        result = reconcile_medications(
            resolved_lines, charted, interaction_rules=ctx.interaction_rules
        )

        # The per-patient half. One ordinary ``check_medication`` per resolved proposal, so each
        # persists its checks and each hard block carries an id an override can name.
        #
        # ``check_medication`` appends the chart-level notes — an unreadable medication line, an
        # unidentifiable allergen, the Child-Pugh window — to *every* drug it evaluates, because
        # on the single-drug screen that is the only place a clinician would see them. Repeated
        # once per proposed line they would bury the per-drug findings: a ten-drug discharge list
        # would carry thirty copies of the same three sentences around the one anaphylaxis.
        #
        # They are removed by *value* rather than by check type. A chart-level note is by
        # construction identical for every drug — it is computed from the same memoised patient
        # facts — so subtracting the set ``chart_completeness_flags`` returns removes exactly the
        # repeated ones and nothing else. Filtering by ``check_type`` instead would have been
        # wrong in both directions: ``duplicate_therapy`` is emitted both by the chart-level
        # duplicate-orders check and by the per-drug one (dropping a real "the patient is
        # already on this" finding), and a chart-level check added to ``check_medication``
        # tomorrow would need remembering here or start repeating. The one flag this
        # deliberately keeps per-drug is the weight-staleness note carried with a proposed
        # weight-dosed drug, which genuinely differs per line and so does not match.
        chart_level = await self.safety.chart_completeness_flags(patient_id)
        safety_findings: list[DrugSafetyFinding] = []
        for resolved in resolved_lines:
            if resolved.drug is None:
                continue
            _, _, flags, check_ids = await self.safety.check_medication(
                account_id=account_id,
                patient_id=patient_id,
                drug_reference_id=resolved.drug.reference_id,
                drug_name=None,
                dose=resolved.dose,
                dose_unit=resolved.dose_unit,
                frequency=resolved.frequency,
            )
            for flag, check_id in zip(flags, check_ids, strict=True):
                if flag in chart_level:
                    continue
                safety_findings.append(
                    DrugSafetyFinding(drug=resolved.drug, check_id=check_id, flag=flag)
                )

        # Then once, for the chart. ``drug`` None because these are not about a drug, and
        # ``check_id`` None because they were not persisted under this reconciliation — the
        # copies that were persisted belong to the per-drug checks above.
        safety_findings.extend(
            DrugSafetyFinding(drug=None, check_id=None, flag=flag) for flag in chart_level
        )

        # Hard blocks first, then by severity, then stably by drug — the same principle the
        # single-drug screen orders by, for the same reason: the list is read top-down and the
        # thing that stops a prescription belongs at the top of it.
        safety_findings.sort(
            key=lambda f: (
                not f.flag.is_hard_block,
                _SEVERITY_RANK.get(f.flag.severity, 9),
                f.drug.generic_name if f.drug else "",
            )
        )

        reconciliation = Reconciliation(
            lines=result.lines,
            list_flags=result.flags,
            safety_findings=safety_findings,
            unresolved_proposed=[
                line.proposed_name or line.label
                for line in result.lines
                if line.disposition == "unresolved_proposed"
            ],
            unresolved_charted=[
                line.charted_name or line.label
                for line in result.lines
                if line.disposition == "unresolved_charted"
            ],
            charted_count=len(charted),
        )

        await self.audit.record(
            action="medications_reconciled",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="patient",
            entity_id=patient_id,
            payload={
                # Counts and dispositions only. The drug names themselves are the patient's
                # prescribing and belong on the rows that hold them, not in an append-only,
                # unencrypted, never-pruned trail — see tests/test_audit_payload_free_text.py.
                "context": context,
                "proposed_count": len(proposed),
                "charted_count": len(charted),
                "reconciled_count": reconciliation.reconciled_line_count,
                "unresolved_proposed": len(reconciliation.unresolved_proposed),
                "unresolved_charted": len(reconciliation.unresolved_charted),
                "dispositions": _disposition_counts(result.lines),
                "list_flag_count": len(result.flags),
                "hard_block": reconciliation.has_hard_block,
            },
        )
        return reconciliation


_SEVERITY_RANK: dict[str, int] = {"hard_block": 0, "critical": 1, "warning": 2, "info": 3}


def _disposition_counts(lines: list[ReconciliationLine]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in lines:
        counts[line.disposition] = counts.get(line.disposition, 0) + 1
    return counts


def _charted_from_context(ctx: SafetyContext) -> list[ChartedCurrentMedication]:
    """This patient's current medications, resolved and unresolved, as reconciliation inputs.

    Both lists, because reconciliation's promise is that every charted row was compared. A chart
    row nothing could resolve is absent from ``charted_doses`` and present in
    ``unresolved_current_meds``; taking only the first would silently shrink the "current
    medications" side of the comparison to the drugs the vocabulary happens to know, and then
    report agreement between the proposed list and a subset of the chart as agreement with the
    chart. Carried through with ``drug=None``, it matches nothing and is reported as
    ``unresolved_charted`` — visible, and honest about what was not compared.
    """
    charted = [
        ChartedCurrentMedication(
            name=dose.drug.generic_name,
            drug=dose.drug,
            dose=dose.dose,
            dose_unit=dose.dose_unit,
            frequency=dose.frequency,
        )
        for dose in ctx.charted_doses
    ]
    charted.extend(
        ChartedCurrentMedication(name=name, drug=None) for name in ctx.unresolved_current_meds
    )
    return charted
