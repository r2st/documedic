"""Patient CRUD + search routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.schemas.common import MessageResponse, PaginatedResponse, PaginationMeta
from app.schemas.patient import (
    PatientCreate,
    PatientResponse,
    PatientSummary,
    PatientUpdate,
)
from app.services.patient_service import PatientService

router = APIRouter(prefix="/patients", tags=["patients"])


@router.post("", response_model=PatientResponse, status_code=status.HTTP_201_CREATED)
async def create_patient(
    body: PatientCreate,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PatientResponse:
    patient = await PatientService(db).create(account.id, body)
    return PatientResponse.model_validate(patient)


@router.get("", response_model=PaginatedResponse[PatientSummary])
async def list_patients(
    search: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[PatientSummary]:
    items, total = await PatientService(db).list(
        account.id, search=search, limit=limit, offset=offset
    )
    return PaginatedResponse[PatientSummary](
        items=[PatientSummary.model_validate(p) for p in items],
        pagination=PaginationMeta(
            total=total, limit=limit, offset=offset, has_more=offset + len(items) < total
        ),
    )


@router.get("/{patient_id}", response_model=PatientResponse)
async def get_patient(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PatientResponse:
    patient = await PatientService(db).get_for_display(account.id, patient_id)
    return PatientResponse.model_validate(patient)


@router.patch("/{patient_id}", response_model=PatientResponse)
async def update_patient(
    patient_id: uuid.UUID,
    body: PatientUpdate,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PatientResponse:
    patient = await PatientService(db).update(account.id, patient_id, body)
    return PatientResponse.model_validate(patient)


@router.delete("/{patient_id}", response_model=MessageResponse)
async def delete_patient(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    await PatientService(db).soft_delete(account.id, patient_id)
    return MessageResponse(message="Patient deleted")
