"""Drug-safety check routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.safety import SafetyFlag, has_hard_block
from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.openapi import PATIENT_ERRORS
from app.schemas.safety import (
    ActiveFlagsResponse,
    DrugSafetyOverrideRequest,
    DrugSafetyOverrideResponse,
    SafetyCheckRequest,
    SafetyCheckResponse,
    SafetyFlagResponse,
)
from app.services.audit_service import AuditService
from app.services.safety_service import SafetyService

router = APIRouter(prefix="/patients/{patient_id}/drug-safety", tags=["drug-safety"])


def _flag_to_response(flag: SafetyFlag, check_id: uuid.UUID | None = None) -> SafetyFlagResponse:
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
    """
    vocab, ctx, flags, check_ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient_id,
        drug_reference_id=body.drug_reference_id,
        drug_name=body.drug_name,
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
    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient_id)
    flags: list[SafetyFlagResponse] = []
    for _vocab, flag_list in results:
        flags.extend(_flag_to_response(f) for f in flag_list)
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
