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
from app.schemas.pathway import ClinicalPathwayOut, PatientPathwaysResponse
from app.services.pathway_service import PathwayService

router = APIRouter(tags=["pathways"])


@router.get("/pathways", response_model=list[str])
async def list_pathways(
    account: Account = Depends(get_current_account),
) -> list[str]:
    return PathwayService.available()


@router.get("/pathways/{condition_name}", response_model=ClinicalPathwayOut)
async def get_pathway(
    condition_name: str,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ClinicalPathwayOut:
    result = await PathwayService(db).get(condition_name)
    return ClinicalPathwayOut(**result)


@router.get("/patients/{patient_id}/pathways", response_model=PatientPathwaysResponse)
async def patient_pathways(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PatientPathwaysResponse:
    result = await PathwayService(db).for_patient(account.id, patient_id)
    return PatientPathwaysResponse(**result)
