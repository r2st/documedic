"""Patient CRUD + search routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.exceptions import UnsupportedQueryParameterError
from app.models.patient import Patient
from app.models.user import Account
from app.schemas.common import MessageResponse, PaginatedResponse, PaginationMeta
from app.schemas.patient import (
    PatientCreate,
    PatientResponse,
    PatientSearchRequest,
    PatientSummary,
    PatientUpdate,
)
from app.services.patient_service import PatientService

router = APIRouter(prefix="/patients", tags=["patients"])


def _page(
    items: list[Patient], total: int, limit: int, offset: int
) -> PaginatedResponse[PatientSummary]:
    return PaginatedResponse[PatientSummary](
        items=[PatientSummary.model_validate(p) for p in items],
        pagination=PaginationMeta(
            total=total, limit=limit, offset=offset, has_more=offset + len(items) < total
        ),
    )


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
    request: Request,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[PatientSummary]:
    """Page through the account's patients. Filtering by name/phone lives on
    ``POST /patients/search`` -- see :class:`PatientSearchRequest` for why.
    """
    # Rejected loudly rather than ignored. FastAPI drops undeclared query parameters
    # silently, so a client still sending ?search=Ramesh would get an unfiltered first page
    # back and look like it worked -- while having already written the name into every
    # access log on the path. A 400 makes the migration impossible to miss.
    if "search" in request.query_params:
        raise UnsupportedQueryParameterError(
            "Patient search no longer accepts a `search` query parameter, because the term "
            "is a direct identifier and query strings are logged in cleartext. Use "
            "POST /patients/search with the term in the request body."
        )
    items, total = await PatientService(db).list(account.id, limit=limit, offset=offset)
    return _page(items, total, limit, offset)


@router.post("/search", response_model=PaginatedResponse[PatientSummary])
async def search_patients(
    body: PatientSearchRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[PatientSummary]:
    """Search the account's patients by name or phone, with the term in the request body.

    POST, not GET, purely so the identifier stays out of URLs and therefore out of access
    logs, browser history and Referer headers. It is a read: nothing is created or mutated.
    """
    items, total = await PatientService(db).list(
        account.id, search=body.search, limit=body.limit, offset=body.offset
    )
    return _page(items, total, body.limit, body.offset)


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
