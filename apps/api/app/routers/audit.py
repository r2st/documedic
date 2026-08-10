"""Audit trail routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.schemas.audit import AuditEntryResponse, AuditVerifyResponse
from app.schemas.common import PaginatedResponse, PaginationMeta
from app.services.audit_service import AuditService
from app.services.patient_service import PatientService

router = APIRouter(prefix="/patients/{patient_id}/audit", tags=["audit"])


@router.get("", response_model=PaginatedResponse[AuditEntryResponse])
async def patient_audit(
    patient_id: uuid.UUID,
    action: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[AuditEntryResponse]:
    await PatientService(db).get(account.id, patient_id)  # ownership check
    items, total = await AuditService(db).list_for_patient(
        patient_id, action=action, limit=limit, offset=offset
    )
    return PaginatedResponse[AuditEntryResponse](
        items=[AuditEntryResponse.model_validate(i) for i in items],
        pagination=PaginationMeta(
            total=total, limit=limit, offset=offset, has_more=offset + len(items) < total
        ),
    )


@router.get("/verify", response_model=AuditVerifyResponse)
async def verify_chain(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> AuditVerifyResponse:
    await PatientService(db).get(account.id, patient_id)
    count, valid = await AuditService(db).verify_patient_chain(patient_id)
    return AuditVerifyResponse(patient_id=patient_id, entries_checked=count, chain_valid=valid)
