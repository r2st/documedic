"""Audit trail routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.openapi import PATIENT_ERRORS
from app.schemas.audit import AuditEntryResponse, AuditVerifyResponse
from app.schemas.common import PaginatedResponse, PaginationMeta
from app.services.audit_service import AuditService
from app.services.patient_service import PatientService

router = APIRouter(prefix="/patients/{patient_id}/audit", tags=["audit"])


@router.get(
    "",
    response_model=PaginatedResponse[AuditEntryResponse],
    summary="This patient's audit trail",
    responses=PATIENT_ERRORS,
)
async def patient_audit(
    patient_id: uuid.UUID,
    action: str | None = Query(
        default=None,
        max_length=100,
        description="Exact action name to filter by, e.g. `document_downloaded`.",
    ),
    limit: int = Query(
        default=50, ge=1, le=200, description="Entries to return. Newest (highest sequence) first."
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description=(
            "Entries to skip. The trail is append-only and `sequence` is unique per patient, "
            "so paging never re-partitions entries already written."
        ),
    ),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[AuditEntryResponse]:
    """Newest-first page of everything recorded against this chart — every access, every
    clinical suggestion, every override.

    Append-only and hash-chained: entries are never updated or deleted, and `record_hash`
    covers the previous entry's hash, so removing or editing one breaks the chain from that
    point on. `GET ../audit/verify` is what checks that.

    `payload` is deliberately thin. It holds identifiers and counts rather than clinical text,
    because this table is unencrypted, immutable, and never pruned.
    """
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


@router.get(
    "/verify",
    response_model=AuditVerifyResponse,
    summary="Verify this patient's audit hash chain",
    responses=PATIENT_ERRORS,
)
async def verify_chain(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> AuditVerifyResponse:
    """Recompute every entry's hash and check it against the one stored.

    `chain_valid: false` means an entry was altered or removed underneath the application —
    tamper evidence, not a transient error, and worth escalating rather than retrying.
    """
    await PatientService(db).get(account.id, patient_id)
    count, valid = await AuditService(db).verify_patient_chain(patient_id)
    return AuditVerifyResponse(patient_id=patient_id, entries_checked=count, chain_valid=valid)
