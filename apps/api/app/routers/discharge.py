"""Discharge summary routes."""

from __future__ import annotations

import uuid
from typing import cast

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.discharge_summary import DischargeSummary
from app.models.user import Account
from app.openapi import PATIENT_ERRORS, errors
from app.routers.safety import _flag_to_response, _uuid
from app.schemas.discharge import (
    ChartActionResponse,
    DischargeCreateRequest,
    DischargeFinalizeRequest,
    DischargeListResponse,
    DischargePreviewResponse,
    DischargeResponse,
    DischargeStatus,
    DischargeUpdateRequest,
    ReadinessItemResponse,
)
from app.schemas.med_reconciliation import (
    ReconciliationFlagResponse,
    ReconciliationLineResponse,
)
from app.services.discharge_service import DischargeAssessment, DischargeService
from app.services.med_reconciliation_service import ProposedLine

router = APIRouter(prefix="/patients/{patient_id}/discharge-summaries", tags=["discharge"])

_SECTIONS = (
    "admission_reason",
    "hospital_course",
    "discharge_diagnosis",
    "follow_up_instructions",
    "patient_instructions",
)


def _to_response(summary: DischargeSummary) -> DischargeResponse:
    return DischargeResponse(
        id=summary.id,
        patient_id=summary.patient_id,
        encounter_id=summary.encounter_id,
        # The column is a plain ``String`` holding one of the two lifecycle values; the response
        # narrows it to the Literal the client is typed by. Both the check constraint and
        # ``DischargeService`` keep the column inside that set.
        status=cast(DischargeStatus, summary.status),
        admission_reason=summary.admission_reason,
        hospital_course=summary.hospital_course,
        discharge_diagnosis=summary.discharge_diagnosis,
        follow_up_instructions=summary.follow_up_instructions,
        patient_instructions=summary.patient_instructions,
        discharge_medications=list(summary.discharge_medications or []),
        reconciliation=dict(summary.reconciliation or {}),
        readiness=[ReadinessItemResponse(**item) for item in summary.readiness or []],
        confirmed_stops=list(summary.confirmed_stops or []),
        medication_event_ids=[
            parsed
            for parsed in (_uuid(value) for value in summary.medication_event_ids or [])
            if parsed is not None
        ],
        finalized_at=summary.finalized_at,
        finalized_by=summary.finalized_by,
        supersedes_id=summary.supersedes_id,
        correction_reason=summary.correction_reason,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
    )


def _to_preview(
    patient_id: uuid.UUID, summary: DischargeSummary, assessment: DischargeAssessment
) -> DischargePreviewResponse:
    return DischargePreviewResponse(
        patient_id=patient_id,
        discharge_summary_id=summary.id,
        readiness=[ReadinessItemResponse(**item.as_dict()) for item in assessment.readiness],
        is_ready=assessment.is_ready,
        chart_actions=[
            ChartActionResponse(
                kind=action.kind,
                label=action.label,
                reference_id=action.reference_id,
                dose=action.dose,
                dose_unit=action.dose_unit,
                frequency=action.frequency,
                previous_dose=action.previous_dose,
            )
            for action in assessment.actions
        ],
        stops_requiring_confirmation=list(assessment.stops),
        lines=[
            ReconciliationLineResponse.model_validate(line, from_attributes=True)
            for line in assessment.lines
        ],
        list_flags=[
            ReconciliationFlagResponse(
                finding=flag.finding,
                severity=flag.severity,
                summary=flag.summary,
                details=flag.details,
                drug_interaction_id=_uuid(flag.drug_interaction_id),
            )
            for flag in assessment.list_flags
        ],
        safety_flags=[
            _flag_to_response(f.flag, f.check_id, f.drug) for f in assessment.safety_findings
        ],
        charted_count=assessment.charted_count,
        proposed_count=assessment.proposed_count,
        reconciled_count=assessment.reconciled_count,
    )


def _lines(body: DischargeCreateRequest) -> list[ProposedLine]:
    return [
        ProposedLine(name=med.name, dose=med.dose, dose_unit=med.dose_unit, frequency=med.frequency)
        for med in body.medications
    ]


@router.post(
    "",
    response_model=DischargeResponse,
    status_code=201,
    summary="Start a discharge summary",
    responses=errors(401, 404),
)
async def create_discharge_summary(
    patient_id: uuid.UUID,
    body: DischargeCreateRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DischargeResponse:
    """A draft. Nothing is required yet, nothing is frozen, and nothing is charted.

    Every field is optional on purpose. A discharge summary that could not be saved half-written
    and come back to is a discharge summary written in one pass at the end of a shift, which is
    the condition this whole feature exists to improve. What is required is checked at
    `POST ../{id}/finalize`, where it can be reported alongside everything else the clinician
    has to read.

    `encounter_id` links the admission this closes. Optional: outpatient care ends without an
    inpatient encounter to point at, and pointing at the nearest visit instead is worse than a
    null. 404 `encounter_not_found` if it is not on this chart.
    """
    summary = await DischargeService(db).create(
        account_id=account.id,
        patient_id=patient_id,
        encounter_id=body.encounter_id,
        medications=_lines(body),
        **{name: getattr(body, name) for name in _SECTIONS},
    )
    await db.commit()
    return _to_response(summary)


@router.get(
    "",
    response_model=DischargeListResponse,
    summary="Every discharge summary on this chart",
    responses=PATIENT_ERRORS,
)
async def list_discharge_summaries(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DischargeListResponse:
    """Newest first. Drafts and finalised summaries together — a draft is part of the story."""
    summaries = await DischargeService(db).list_for_patient(
        account_id=account.id, patient_id=patient_id
    )
    return DischargeListResponse(
        patient_id=patient_id, summaries=[_to_response(s) for s in summaries]
    )


@router.get(
    "/{summary_id}",
    response_model=DischargeResponse,
    summary="One discharge summary, as it stands or as it was finalised",
    responses=PATIENT_ERRORS,
)
async def get_discharge_summary(
    patient_id: uuid.UUID,
    summary_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DischargeResponse:
    """The stored row, snapshots included.

    On a finalised summary the reconciliation and readiness snapshots are what stood at the
    moment of finalising and are never recomputed — the record's job is to say what was true
    when the patient went home. `POST ../{id}/preview` is the endpoint that asks the other
    question, "would this still be safe now".
    """
    summary = await DischargeService(db).read(
        account_id=account.id, patient_id=patient_id, summary_id=summary_id
    )
    return _to_response(summary)


@router.patch(
    "/{summary_id}",
    response_model=DischargeResponse,
    summary="Edit a draft discharge summary",
    responses=errors(401, 404, 409),
)
async def update_discharge_summary(
    patient_id: uuid.UUID,
    summary_id: uuid.UUID,
    body: DischargeUpdateRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DischargeResponse:
    """409 `discharge_finalized` once it has been finalised. A correction is a new summary.

    `medications` replaces the stored list wholesale. A partial update of a medication list has
    no safe reading: "these three changed" leaves every other line ambiguous between unchanged
    and removed, and completeness is the entire value of the list. Omit the field to leave it
    alone; send it to replace it.
    """
    summary = await DischargeService(db).update(
        account_id=account.id,
        patient_id=patient_id,
        summary_id=summary_id,
        encounter_id=body.encounter_id,
        # ``None`` means "leave the list alone"; an explicitly empty list is not distinguishable
        # from an omitted one in this schema and is treated as an omission, because clearing a
        # take-home list is not an edit anyone means to make in passing.
        medications=_lines(body) or None,
        **{name: getattr(body, name) for name in _SECTIONS},
    )
    await db.commit()
    return _to_response(summary)


@router.post(
    "/{summary_id}/preview",
    response_model=DischargePreviewResponse,
    summary="What finalising would do, and what stands in the way",
    responses=errors(401, 404),
)
async def preview_discharge(
    patient_id: uuid.UUID,
    summary_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DischargePreviewResponse:
    """Reconciles the take-home list against the chart and applies the readiness rules.

    Writes nothing to the chart and nothing to the summary. It does persist a
    `drug_safety_checks` row per take-home medicine, which is deliberate: a hard block is passed
    only by an override naming the check that raised it (Critical Safety Rule #3), so a block
    reported without a check id would be a refusal with no route around it.

    Read this immediately before finalising. `stops_requiring_confirmation` is the exact set to
    echo back as `confirmed_stops`, and the server recomputes it — a discontinuation confirmed
    against a chart that has since moved is refused rather than accepted.

    **Deterministic and offline-capable** (Critical Safety Rule #8). Deciding whether it is safe
    to send a patient home is the last thing that should wait on an LLM provider being
    reachable, so no part of this path calls one.

    422 if the summary carries no take-home medications: an empty list against a non-empty chart
    is a request to stop everything, and it would come back as a screenful of omission flags
    produced by a client that failed to send its list.
    """
    summary, assessment = await DischargeService(db).preview(
        account_id=account.id, patient_id=patient_id, summary_id=summary_id
    )
    await db.commit()
    return _to_preview(patient_id, summary, assessment)


@router.post(
    "/{summary_id}/finalize",
    response_model=DischargePreviewResponse,
    summary="Finalise the discharge, write the chart, and freeze the document",
    responses=errors(401, 404, 409),
)
async def finalize_discharge(
    patient_id: uuid.UUID,
    summary_id: uuid.UUID,
    body: DischargeFinalizeRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DischargePreviewResponse:
    """The act that makes the chart agree with the document.

    Finalising writes `medication_events` for every line the reconciliation says has changed —
    a `start` per new drug, a `change` per new dose, a `stop` per confirmed discontinuation —
    and retires the current rows each supersedes. Without that, the patient goes home on the
    new list and the chart goes on carrying the admission's, so every safety check at the next
    visit runs against a list that has been wrong since the day they left.

    **Every discontinuation must be confirmed.** A `stop` disposition means the take-home list
    does not carry a drug the chart calls current, which is an intended discontinuation about
    half the time and a line somebody forgot to type the other half. `confirmed_stops` must
    equal `stops_requiring_confirmation` from the preview exactly — a mismatch in either
    direction is 409 `discharge_stops_unconfirmed`, because a drug that has *stopped* being
    absent since the preview means the clinician confirmed a picture that is no longer current.

    **All of it or none of it.** 409 `discharge_not_ready` while any readiness item is blocking
    — an unacknowledged critical lab value, a safety hard block, an unidentifiable take-home
    drug, or a missing diagnosis or hospital course — with nothing written. A discharge is one
    clinical act, and half-charting it leaves a record that reads as complete with one medicine
    quietly missing.

    409 `discharge_finalized` if it has already been finalised. A correction is a new summary
    naming `supersedes_id` and `correction_reason`; the finalised row is immutable by trigger
    (Critical Safety Rule #7).
    """
    summary, assessment = await DischargeService(db).finalize(
        account_id=account.id,
        patient_id=patient_id,
        summary_id=summary_id,
        finalized_by=body.finalized_by,
        confirmed_stops=body.confirmed_stops,
        supersedes_id=body.supersedes_id,
        correction_reason=body.correction_reason,
    )
    await db.commit()
    return _to_preview(patient_id, summary, assessment)
