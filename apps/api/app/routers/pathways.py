"""Clinical pathway routes: staged, guideline-cited care pathways per condition.

Reference lookups, not reasoning-engine output -- these do not go through the Verifier Agent
(see app/core/pathways.py module docstring for why that's consistent with the other guideline
routes in app/routers/guidelines.py).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.openapi import AUTH_ERRORS, PATIENT_ERRORS, errors
from app.schemas.pathway import ClinicalPathwayOut, PatientPathwaysResponse
from app.services.pathway_service import PathwayService

router = APIRouter(tags=["pathways"])


@router.get(
    "/pathways",
    response_model=list[str],
    summary="Conditions a curated pathway exists for",
    responses=AUTH_ERRORS,
)
async def list_pathways(
    account: Account = Depends(get_current_account),
) -> list[str]:
    """The condition names accepted by `GET /pathways/{condition_name}`. Static reference
    data, identical for every account."""
    return PathwayService.available()


@router.get(
    "/pathways/{condition_name}",
    response_model=ClinicalPathwayOut,
    summary="The staged care pathway for one condition",
    responses=errors(401, 404),
)
async def get_pathway(
    condition_name: str,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ClinicalPathwayOut:
    """A guideline-cited pathway with its stages, each carrying its source citation.

    Reference material, not reasoning-engine output — nothing here is patient-specific, so it
    does not pass through the Verifier. A condition with no curated pathway is a 404
    (`pathway_not_found`); the differential and drug-safety checks are unaffected by that.
    """
    result = await PathwayService(db).get(condition_name)
    return ClinicalPathwayOut(**result)


@router.get(
    "/patients/{patient_id}/pathways",
    response_model=PatientPathwaysResponse,
    summary="Pathways matching this patient's recorded conditions",
    responses=PATIENT_ERRORS,
)
async def patient_pathways(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PatientPathwaysResponse:
    """Cross-references the chart's conditions against the curated pathways.

    A lookup, not a recommendation: it reports which pathways exist for what is already
    documented, and says nothing about what the patient should be given.
    """
    result = await PathwayService(db).for_patient(account.id, patient_id)
    return PatientPathwaysResponse(**result)
