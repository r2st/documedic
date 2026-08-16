"""Patient CRUD + search routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account, rate_limit, require_recent_authentication
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
from app.schemas.patient_import import (
    COLUMN_HELP,
    ImportRowResult,
    PatientImportResponse,
)
from app.services.patient_import_service import PatientImportService
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


@router.post(
    "/import",
    response_model=PatientImportResponse,
    summary="Create charts in bulk from a CSV of demographics",
    responses=AUTH_ERRORS | errors(403, 413, 429),
    dependencies=[Depends(rate_limit("patient_import"))],
)
async def import_patients(
    file: UploadFile = File(..., description="A CSV file. " + COLUMN_HELP),
    dry_run: bool = Query(
        default=False,
        description=(
            "Validate and report without writing anything. Costs the same rate-limit budget as "
            "a real import."
        ),
    ),
    account: Account = Depends(require_recent_authentication),
    db: AsyncSession = Depends(get_db),
) -> PatientImportResponse:
    """Load a practice's patient list from a spreadsheet, one decided outcome per row.

    Send the file as `multipart/form-data`. It is read as CSV — which is what every spreadsheet
    exports, including Excel — in whichever of UTF-8, UTF-16 or CP1252 it decodes as, with the
    delimiter (`,`, `;` or tab) taken from the header row. The response says which of each it
    used, so a mangled name is diagnosable without experiment.

    **`consent_given` is a required column and a row without it is not created.** Under the DPDP
    Act consent is the lawful basis for holding these identifiers at all, so the bulk path
    refuses exactly what the create form refuses. `yes`/`no`, `true`/`false` and `1`/`0` are all
    read; anything else is reported rather than assumed.

    **A row whose name is already on this account is never created**, and is reported as
    `duplicate` (same name and date of birth) or `possible_duplicate` (same name, and one of the
    two has no date of birth, so they cannot be told apart). There is no override: two charts for
    one patient split their allergies and prescriptions across both, and every deterministic
    safety check then runs against half a record. Rename or add a date of birth and upload again.

    **Ambiguous dates are refused, not guessed.** `03/04/1990` is 3 April in one locale's export
    and 4 March in another's, and a wrong date of birth changes which dose ceiling a child's
    prescription is judged against. The order is inferred from the file's own unambiguous rows —
    any date with a component over 12 settles it for the whole file — and rows that nothing
    settles are refused individually asking for `YYYY-MM-DD`. A file that proves both orders is
    refused whole.

    Partial success is the normal outcome: valid rows are created even when others are refused,
    so a file with three bad lines does not have to be uploaded again in full. Use `dry_run=true`
    to see the same report with nothing written.

    **Needs a recent password** (`403 reauthentication_required`): this route creates charts in
    bulk from a signed-in workstation. Every chart created is audited as `patient_created` as
    usual, and the upload itself as `patients_imported`.
    """
    raw = await file.read()
    result = await PatientImportService(db).import_csv(account.id, raw, dry_run=dry_run)
    return PatientImportResponse(
        dry_run=result.dry_run,
        total_rows=result.total_rows,
        created=result.created,
        skipped=result.skipped,
        invalid=result.invalid,
        encoding=result.encoding,
        delimiter=result.delimiter,
        date_convention=result.date_convention,
        unknown_columns=list(result.unknown_columns),
        rows=[
            ImportRowResult(
                row_number=row.row_number,
                status=row.status,
                full_name=row.full_name,
                patient_id=row.patient_id,
                existing_patient_id=row.existing_patient_id,
                message=row.message,
            )
            for row in result.rows
        ],
    )


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
    responses=PATIENT_ERRORS | errors(403),
)
async def delete_patient(
    patient_id: uuid.UUID,
    account: Account = Depends(require_recent_authentication),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    """Soft delete: the chart stops appearing and stops resolving, and the deletion is audited.

    The rows survive underneath, because the audit trail is append-only and its hash chain
    references them — a clinical record that can be made to vanish is not an audit trail.

    **Needs a recent password.** Returns `403 reauthentication_required` when the password has
    not been confirmed within `REAUTHENTICATION_MAX_AGE_MINUTES`; POST `/auth/reauthenticate`
    and retry. This is the one route that takes a whole chart out of clinical use with a single
    call, and it is reachable from any signed-in workstation — which on this product's shared
    login is a screen someone else may be standing at.
    """
    await PatientService(db).soft_delete(account.id, patient_id)
    return MessageResponse(message="Patient deleted")
