"""Drug-safety check routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safety import DrugRef, SafetyFlag, has_hard_block
from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.openapi import PATIENT_ERRORS
from app.schemas.med_reconciliation import (
    MedicationReconciliationRequest,
    MedicationReconciliationResponse,
    ReconciliationFlagResponse,
    ReconciliationLineResponse,
)
from app.schemas.safety import (
    ActiveFlagsResponse,
    DrugSafetyOverrideRequest,
    DrugSafetyOverrideResponse,
    SafetyCheckRequest,
    SafetyCheckResponse,
    SafetyFlagResponse,
)
from app.services.audit_service import AuditService
from app.services.med_reconciliation_service import MedReconciliationService, ProposedLine
from app.services.safety_service import SafetyService

router = APIRouter(prefix="/patients/{patient_id}/drug-safety", tags=["drug-safety"])


def _flag_to_response(
    flag: SafetyFlag,
    check_id: uuid.UUID | None = None,
    drug: DrugRef | None = None,
) -> SafetyFlagResponse:
    """One flag as it goes over the wire.

    ``drug`` names the current medication the flag was raised about, and is passed only by the
    endpoint that evaluates several — see ``SafetyFlagResponse.drug_reference_id``. Left null by
    ``POST ../check``, where every per-drug flag is about the single proposed drug the response
    already names at the top level, and by the chart-level notes, which are about the record
    rather than about any medication.
    """
    return SafetyFlagResponse(
        id=check_id,
        check_type=flag.check_type,
        severity=flag.severity,
        is_hard_block=flag.is_hard_block,
        summary=flag.summary,
        details=flag.details,
        drug_interaction_id=_uuid(flag.drug_interaction_id),
        contraindication_id=_uuid(flag.contraindication_id),
        allergy_id=_uuid(flag.allergy_id),
        drug_reference_id=drug.reference_id if drug else None,
        drug_name=drug.generic_name if drug else None,
    )


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (ValueError, TypeError):
        return None


@router.post(
    "/check",
    response_model=SafetyCheckResponse,
    summary="Check a proposed medication against this patient's record",
    responses=PATIENT_ERRORS,
)
async def check_medication(
    patient_id: uuid.UUID,
    body: SafetyCheckRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> SafetyCheckResponse:
    """Deterministic allergy, interaction and contraindication check. No LLM on this path.

    Supply `drug_reference_id` when you have it, or `drug_name` (an Indian brand name such as
    "Crocin" is fine — it resolves through the DrugVocabulary to its INN, which is how an
    allergy recorded against "Paracetamol" catches it). A name nothing resolves to is a 422
    rather than an unchecked pass: an unresolved drug cannot be evaluated, and reporting "no
    problems found" for a drug that was never checked is the dangerous answer.

    `is_hard_block` means an allergy or absolute contraindication. It is not advisory — the
    action is blocked until a clinician records an override at `POST ../override` with written
    reasoning. Flags carry the `id` that override needs.

    Supply `dose`, `dose_unit` and `frequency` to have the *dose* checked as well, against the
    curated therapeutic range for the drug and against this patient's age, weight and renal
    function. Omitting them checks the drug and not the dose — which is the ordinary request
    while a clinician is still choosing between drugs, and raises nothing on its own. A dose
    finding is never a hard block: dose ceilings are exceeded deliberately and routinely, for
    reasons this record does not hold.
    """
    vocab, ctx, flags, check_ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient_id,
        drug_reference_id=body.drug_reference_id,
        drug_name=body.drug_name,
        dose=body.dose,
        dose_unit=body.dose_unit,
        frequency=body.frequency,
        modality=body.modality,
    )
    blocked = has_hard_block(flags)
    return SafetyCheckResponse(
        patient_id=patient_id,
        proposed_drug_reference_id=vocab.reference_id,
        proposed_drug_name=vocab.generic_name,
        is_blocked=blocked,
        is_hard_block=blocked,
        checked_against={
            "current_medications": len(ctx.current_meds),
            "allergies": len(ctx.allergies),
            "conditions": len(ctx.conditions),
            "egfr_available": ctx.egfr is not None,
            # Same statement for the other measured axis. A hepatic threshold with no liver
            # panel to apply it to reports itself in `flags`, but only for a drug that has such
            # a rule — this says what the chart carries regardless, so "renal thresholds were
            # applied and hepatic ones could not be" is legible without reading the flag list.
            "hepatic_markers_available": bool(ctx.hepatic),
            # Counted separately from `current_medications`, which holds only what could be
            # matched to the vocabulary. Without this the count reads as the whole medication
            # list and quietly shrinks by whatever the resolver could not read.
            "unresolved_medications": len(ctx.unresolved_current_meds),
            # Likewise for `allergies`, which counts every documented allergy including the
            # ones carrying no reference id and no drug class. Those were cross-checked against
            # nothing but an exact generic-name match, so the headline count overstates how
            # much of Critical Safety Rule #3 actually ran.
            "unresolved_allergies": len(ctx.unresolved_allergies),
        },
        flags=[_flag_to_response(f, cid) for f, cid in zip(flags, check_ids, strict=True)],
    )


@router.get(
    "/flags",
    response_model=ActiveFlagsResponse,
    summary="Re-check every medication the patient is currently on",
    responses=PATIENT_ERRORS,
)
async def active_flags(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ActiveFlagsResponse:
    """The standing safety picture: each current medication evaluated against all the others.

    Recomputed per call rather than cached, so it reflects the chart as it is now — a lab
    result approved a minute ago can move a drug into a renal contraindication. Flags here
    carry no `id`: they are a live view, not the persisted check records that `POST /check`
    writes and that an override refers to.

    Reading it discloses the patient's whole current medication list and every allergy that
    bears on it, so the access is audited as `drug_safety_flags_viewed`.
    """
    service = SafetyService(db)
    results = await service.active_flags(account_id=account.id, patient_id=patient_id)
    flags: list[SafetyFlagResponse] = []
    for vocab, flag_list in results:
        # Attributed to the medication it was raised about. A pairwise finding legitimately
        # appears twice here — warfarin's interaction with aspirin is a fact about both, and a
        # clinician scanning what is wrong with each drug wants it under each — but only if the
        # response says which drug each row is under. Flattening it away turned a chart with
        # four current medications and four interacting pairs into eight indistinguishable
        # rows, and the duplication scales with the square of the medication list.
        flags.extend(_flag_to_response(f, None, vocab) for f in flag_list)
    # Once for the chart, not once per drug: a medication or an allergen the resolver could
    # not read is missing from every drug's evaluation here, so an empty list above is not
    # the same as a clean one. Served from the facts the call above already loaded.
    flags.extend(_flag_to_response(f) for f in await service.chart_completeness_flags(patient_id))
    # After the check, immediately before the commit: the append lock is transaction-scoped
    # on PostgreSQL, so auditing first would hold it across the whole re-evaluation.
    await AuditService(db).record(
        action="drug_safety_flags_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        # What was disclosed, in counts. Drug names and flag summaries are clinical content.
        payload={"drugs_evaluated": len(results), "flag_count": len(flags)},
    )
    await db.commit()
    return ActiveFlagsResponse(patient_id=patient_id, flags=flags)


@router.post(
    "/reconcile",
    response_model=MedicationReconciliationResponse,
    summary="Reconcile a whole proposed medication list against the chart",
    responses=PATIENT_ERRORS,
)
async def reconcile_medications(
    patient_id: uuid.UUID,
    body: MedicationReconciliationRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> MedicationReconciliationResponse:
    """List-level reconciliation at a transition of care. Deterministic; no LLM on this path.

    `POST ../check` asks whether one drug is safe for this patient. This asks what is different
    between two medication lists, which is a question no number of single-drug checks answers.
    Three of its findings are structurally unreachable from that endpoint:

    * a charted drug **absent** from the proposed list — there is no drug to pass in, so the
      check is not one that failed but one that was never called;
    * an interaction between two drugs that are **both new** — checked singly, each is clean,
      because neither is on the chart yet;
    * the same molecule arriving **twice in one list**, typically as a brand on one line and its
      INN on another.

    Every proposed line is *also* put through the ordinary per-drug check, so allergies,
    contraindications and renal/hepatic thresholds are evaluated exactly as they are on the
    single-drug screen — and the hard blocks come back carrying the same `id`, so
    `POST ../override` works against them unchanged. Nothing here writes to the chart: the
    response is a comparison, and starting, stopping or changing a medication is still done
    through the ordinary medication routes so that each carries its own reason.

    A proposed name that resolves to no known drug does not fail the request; it is returned
    under `unresolved_proposed` and counted out of `reconciled_count`. Refusing the whole list
    over one unseeded brand would leave the clinician with no reconciliation at all, and the
    remaining lines are where the omissions are.
    """
    result = await MedReconciliationService(db).reconcile(
        account_id=account.id,
        patient_id=patient_id,
        proposed=[
            ProposedLine(
                name=med.name, dose=med.dose, dose_unit=med.dose_unit, frequency=med.frequency
            )
            for med in body.medications
        ],
        context=body.context,
    )
    await db.commit()
    return MedicationReconciliationResponse(
        patient_id=patient_id,
        context=body.context,
        lines=[
            ReconciliationLineResponse.model_validate(line, from_attributes=True)
            for line in result.lines
        ],
        list_flags=[
            ReconciliationFlagResponse(
                finding=flag.finding,
                severity=flag.severity,
                summary=flag.summary,
                details=flag.details,
                drug_interaction_id=_uuid(flag.drug_interaction_id),
            )
            for flag in result.list_flags
        ],
        safety_flags=[
            _flag_to_response(f.flag, f.check_id, f.drug) for f in result.safety_findings
        ],
        is_blocked=result.has_hard_block,
        proposed_count=len(body.medications),
        charted_count=result.charted_count,
        reconciled_count=result.reconciled_line_count,
        unresolved_proposed=result.unresolved_proposed,
        unresolved_charted=result.unresolved_charted,
    )


@router.post(
    "/override",
    response_model=DrugSafetyOverrideResponse,
    status_code=201,
    summary="Record a documented clinician override of a hard block",
    responses=PATIENT_ERRORS,
)
async def override_hard_block(
    patient_id: uuid.UUID,
    body: DrugSafetyOverrideRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DrugSafetyOverrideResponse:
    """The only sanctioned way past a hard block, and it is never silent.

    `reasoning` is required and must be substantive (10 characters minimum) — the override is
    a clinical decision that has to stand up on the record months later. The original check is
    neither deleted nor edited; this adds a linked, equally immutable record and audits it as
    `drug_safety_hard_block_overridden`.

    422 if the referenced check is only advisory: an advisory flag needs no override, and
    accepting one here would train clinicians to click through the ones that do matter.
    """
    override = await SafetyService(db).override_hard_block(
        account_id=account.id,
        patient_id=patient_id,
        drug_safety_check_id=body.drug_safety_check_id,
        reasoning=body.reasoning,
    )
    return DrugSafetyOverrideResponse.model_validate(override, from_attributes=True)


@router.get(
    "/overrides",
    response_model=list[DrugSafetyOverrideResponse],
    summary="Hard-block overrides recorded on this chart",
    responses=PATIENT_ERRORS,
)
async def list_overrides(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[DrugSafetyOverrideResponse]:
    """Every override on this patient, with its reasoning. Append-only — nothing here is
    editable or removable."""
    overrides = await SafetyService(db).list_overrides(account_id=account.id, patient_id=patient_id)
    return [DrugSafetyOverrideResponse.model_validate(o, from_attributes=True) for o in overrides]
