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
from app.openapi import AUTH_ERRORS, PATIENT_ERRORS, errors
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


@router.post(
    "",
    response_model=PatientResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open a new patient chart",
    responses=AUTH_ERRORS,
)
async def create_patient(
    body: PatientCreate,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PatientResponse:
    """Create the chart. `consent_given` must be true.

    That is a 422 with `code: consent_required` when it is not — under the DPDP Act there is
    no lawful basis for storing the identifiers this record holds without recorded consent, so
    it is a precondition rather than a field the clinician can fill in later.
    """
    patient = await PatientService(db).create(account.id, body)
    return PatientResponse.model_validate(patient)


@router.get(
    "",
    response_model=PaginatedResponse[PatientSummary],
    summary="Page through this account's patients",
    responses=AUTH_ERRORS | errors(400),
)
async def list_patients(
    request: Request,
    limit: int = Query(
        default=25, ge=1, le=100, description="Patients to return. Newest-updated first."
    ),
    offset: int = Query(default=0, ge=0, description="Patients to skip."),
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


@router.post(
    "/search",
    response_model=PaginatedResponse[PatientSummary],
    summary="Search this account's patients by name or phone",
    responses=AUTH_ERRORS,
)
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


@router.get(
    "/{patient_id}",
    response_model=PatientResponse,
    summary="One patient's demographics",
    responses=PATIENT_ERRORS,
)
async def get_patient(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PatientResponse:
    """Decrypt and return the chart header. Writes a `patient_viewed` entry to the audit trail:
    this discloses direct identifiers, so the read itself is a recorded event.

    A chart owned by another account is a 404, identical to one that does not exist.
    """
    patient = await PatientService(db).get_for_display(account.id, patient_id)
    return PatientResponse.model_validate(patient)


@router.patch(
    "/{patient_id}",
    response_model=PatientResponse,
    summary="Amend a patient's demographics",
    responses=PATIENT_ERRORS,
)
async def update_patient(
    patient_id: uuid.UUID,
    body: PatientUpdate,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PatientResponse:
    """Partial update — omitted fields are left as they are. Audited as `patient_updated`.

    Demographics only. Clinical content (medications, labs, conditions, allergies) never
    arrives this way; it is merged from an approved document extraction.
    """
    patient = await PatientService(db).update(account.id, patient_id, body)
    return PatientResponse.model_validate(patient)


@router.delete(
    "/{patient_id}",
    response_model=MessageResponse,
    summary="Withdraw a patient chart from use",
    responses=PATIENT_ERRORS,
)
async def delete_patient(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Soft delete: the chart stops appearing and stops resolving, and the deletion is audited.

    The rows survive underneath, because the audit trail is append-only and its hash chain
    references them — a clinical record that can be made to vanish is not an audit trail.
    """
    await PatientService(db).soft_delete(account.id, patient_id)
    return MessageResponse(message="Patient deleted")
