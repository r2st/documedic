"""Longitudinal patient record route."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.openapi import PATIENT_ERRORS
from app.schemas.record import CriticalLabFlagItem, CriticalLabFlagsResponse, LongitudinalRecord
from app.services.audit_service import AuditService
from app.services.lab_safety_service import LabSafetyService
from app.services.patient_service import PatientService
from app.services.record_service import RecordService

router = APIRouter(prefix="/patients/{patient_id}/record", tags=["records"])
labs_router = APIRouter(prefix="/patients/{patient_id}/labs", tags=["records"])


@router.get(
    "",
    response_model=LongitudinalRecord,
    summary="The assembled longitudinal record",
    responses=PATIENT_ERRORS,
)
async def get_record(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> LongitudinalRecord:
    """Everything approved into the patient graph: medications, labs, conditions, allergies,
    encounters and derived markers, in one timeline.

    The widest clinical read in the API, and audited as `patient_record_viewed` for that
    reason. Reads the graph directly — no LLM, so it is unaffected when reasoning is offline.
    """
    # Ownership check (raises if not found / not owned) plus a PHI-access audit entry: this
    # returns the whole longitudinal record, the widest clinical read in the API.
    await PatientService(db).get(account.id, patient_id)
    record = await RecordService(db).assemble(patient_id)
    await AuditService(db).record(
        action="patient_record_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
    )
    await db.commit()
    return record


@labs_router.get(
    "/critical-flags",
    response_model=CriticalLabFlagsResponse,
    summary="Panic/critical values in the patient's latest labs",
    responses=PATIENT_ERRORS,
)
async def critical_lab_flags(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> CriticalLabFlagsResponse:
    """Deterministic, offline panic/critical-value check on the patient's latest labs.

    Re-runs on every call (no caching) so it reflects the current record, and re-audits any
    finding — Critical Safety Rule #8 (must work without an LLM).
    """
    flagged = await LabSafetyService(db).check_patient_labs(
        account_id=account.id, patient_id=patient_id
    )
    await db.commit()
    return CriticalLabFlagsResponse(
        patient_id=patient_id,
        flags=[
            CriticalLabFlagItem(
                lab_result_id=lab.id,
                marker_name=flag.marker_name,
                value=flag.value,
                unit=flag.unit,
                severity=flag.severity,
                summary=flag.summary,
                details=flag.details,
            )
            for lab, flag in flagged
        ],
    )
