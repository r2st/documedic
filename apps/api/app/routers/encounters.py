"""Encounter lifecycle routes: open, edit, sign, amend.

Every route here is patient-scoped and account-checked before it touches an encounter, so an
encounter id belonging to another clinician's chart is a 404 exactly like one that never
existed. The write routes go through ``get_for_processing``, which is the consent gate: a chart
whose patient has withdrawn consent stays readable and stops accepting new clinical content.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.encounter import Encounter
from app.models.user import Account
from app.openapi import PATIENT_ERRORS, errors
from app.schemas.common import PaginatedResponse, PaginationMeta
from app.schemas.encounter import (
    EncounterAmend,
    EncounterCreate,
    EncounterResponse,
    EncounterUpdate,
)
from app.services.audit_service import AuditService
from app.services.encounter_service import EncounterService
from app.services.patient_service import PatientService

router = APIRouter(prefix="/patients/{patient_id}/encounters", tags=["encounters"])

# Conflict (409) is the shape every refused lifecycle step takes — the caller is entitled to the
# chart, the record is simply not in a state that admits the change. (422 is FastAPI's own, from
# the request model, and ``errors()`` refuses to redocument it.)
LIFECYCLE_ERRORS = PATIENT_ERRORS | errors(409)


def _page(
    items: list[Encounter], total: int, limit: int, offset: int
) -> PaginatedResponse[EncounterResponse]:
    return PaginatedResponse[EncounterResponse](
        items=[EncounterResponse.model_validate(e) for e in items],
        pagination=PaginationMeta(
            total=total, limit=limit, offset=offset, has_more=offset + len(items) < total
        ),
    )


@router.get(
    "",
    response_model=PaginatedResponse[EncounterResponse],
    summary="Page through this chart's visits",
    responses=PATIENT_ERRORS,
)
async def list_encounters(
    patient_id: uuid.UUID,
    limit: int = Query(default=25, ge=1, le=100, description="Visits to return, newest first."),
    offset: int = Query(default=0, ge=0, description="Visits to skip."),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PaginatedResponse[EncounterResponse]:
    """Every visit on the chart, newest first, whatever its lifecycle state.

    Drafts are **not** filtered out. A visit somebody started and never signed is part of what
    the chart holds, and hiding it here would leave it discoverable only by the clinician who
    opened it — which is how two people end up writing the same consultation twice.

    Amended visits stay in the list alongside the amendments that superseded them, each
    carrying `amends_encounter_id`, because the record of what the note said when a decision was
    made is the reason the amendment mechanism exists at all.

    Audited as `encounter_list_viewed`: the response carries presenting complaints and notes, so
    it is a disclosure of clinical content and the read itself is a recorded event.
    """
    await PatientService(db).get(account.id, patient_id)
    items, total = await EncounterService(db).list(patient_id, limit=limit, offset=offset)
    await AuditService(db).record(
        action="encounter_list_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        payload={"returned": len(items), "total": total},
    )
    await db.commit()
    return _page(items, total, limit, offset)


@router.post(
    "",
    response_model=EncounterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open a new visit",
    responses=PATIENT_ERRORS,
)
async def create_encounter(
    patient_id: uuid.UUID,
    body: EncounterCreate,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> EncounterResponse:
    """Open a visit as a `draft`.

    A draft is a working note: it can be edited freely and it carries no attestation. Nothing on
    this chart treats it as a clinical statement until it is signed.
    """
    await PatientService(db).get_for_processing(account.id, patient_id)
    encounter = await EncounterService(db).create(account.id, patient_id, body)
    return EncounterResponse.model_validate(encounter)


@router.get(
    "/{encounter_id}",
    response_model=EncounterResponse,
    summary="One visit",
    responses=PATIENT_ERRORS,
)
async def get_encounter(
    patient_id: uuid.UUID,
    encounter_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> EncounterResponse:
    """One visit, with its lifecycle state, its signature and — if it is an amendment — the
    encounter it supersedes and the reason recorded for it.

    Audited as `encounter_viewed`.
    """
    await PatientService(db).get(account.id, patient_id)
    encounter = await EncounterService(db).get(patient_id, encounter_id)
    await AuditService(db).record(
        action="encounter_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="encounter",
        entity_id=encounter_id,
    )
    await db.commit()
    return EncounterResponse.model_validate(encounter)


@router.patch(
    "/{encounter_id}",
    response_model=EncounterResponse,
    summary="Edit an unsigned visit",
    responses=LIFECYCLE_ERRORS,
)
async def update_encounter(
    patient_id: uuid.UUID,
    encounter_id: uuid.UUID,
    body: EncounterUpdate,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> EncounterResponse:
    """Partial update of a `draft` or `in_progress` visit — omitted fields are left alone.

    A signed or amended visit is a **409** with `code: encounter_signed`, not a silent no-op and
    not a partial write. Its content is frozen at the database as well as here; record an
    amendment instead, which preserves the signed note and adds the correction beside it.

    `status` accepts only `draft` and `in_progress`. Signing and amending are their own routes
    because each records a clinical act with its own audit entry.
    """
    await PatientService(db).get_for_processing(account.id, patient_id)
    encounter = await EncounterService(db).update(account.id, patient_id, encounter_id, body)
    return EncounterResponse.model_validate(encounter)


@router.post(
    "/{encounter_id}/sign",
    response_model=EncounterResponse,
    summary="Sign a visit, freezing its content",
    responses=LIFECYCLE_ERRORS,
)
async def sign_encounter(
    patient_id: uuid.UUID,
    encounter_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> EncounterResponse:
    """Attest to the visit. The signing account and the moment are recorded on the row, and the
    clinical content can never change again — a correction is an amendment.

    Signing an encounter that is already signed is a **409**: a second signature would either
    overwrite the first attestation or record two, with nothing on the chart to say which one it
    means.

    Signing an **amendment** is what makes it the current version of the visit. The encounter it
    amends moves to `amended` in the same transaction, and only then — an amendment left in
    draft leaves the original untouched.
    """
    await PatientService(db).get_for_processing(account.id, patient_id)
    encounter = await EncounterService(db).sign(account.id, patient_id, encounter_id)
    return EncounterResponse.model_validate(encounter)


@router.post(
    "/{encounter_id}/amend",
    response_model=EncounterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open an amendment to a signed visit",
    responses=LIFECYCLE_ERRORS,
)
async def amend_encounter(
    patient_id: uuid.UUID,
    encounter_id: uuid.UUID,
    body: EncounterAmend,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> EncounterResponse:
    """Create a new **draft** encounter that supersedes the signed one, carrying the corrections
    and a documented reason.

    Returns the amendment, not the original: nothing about the original changes here. The
    amendment has to be signed in its own right (`POST .../{amendment_id}/sign`), and that is
    the point at which the original becomes `amended`.

    `amendment_reason` is required and must actually say something — it is the whole difference
    between an amendment and an edit, and it is what a reader months later has to reconstruct
    why the note changed.

    Fields left out are copied from the signed encounter, so an amendment correcting only the
    note reads as a complete version of the visit rather than a diff.
    """
    await PatientService(db).get_for_processing(account.id, patient_id)
    amendment = await EncounterService(db).amend(account.id, patient_id, encounter_id, body)
    return EncounterResponse.model_validate(amendment)
