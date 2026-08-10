"""Phase 4 routes: validation harness, performance metrics, CDSCO dossier, safety reports."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db
from app.dependencies import get_current_account
from app.exceptions import NotFoundError
from app.models.user import Account
from app.schemas.validation import (
    SafetyReportIn,
    SafetyReportOut,
    ValidationRunOut,
    ValidationRunSummary,
)
from app.services.metrics_service import MetricsService
from app.services.regulatory_service import RegulatoryService
from app.services.safety_report_service import SafetyReportService
from app.services.validation_service import ValidationService

router = APIRouter(tags=["validation"])


@router.post("/validation/run", response_model=ValidationRunOut, status_code=201)
async def run_validation(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ValidationRunOut:
    run = await ValidationService(db).run(account.id)
    return ValidationRunOut.model_validate(run)


@router.get("/validation/runs", response_model=list[ValidationRunSummary])
async def list_validation_runs(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[ValidationRunSummary]:
    runs = await ValidationService(db).list_runs(account.id)
    return [ValidationRunSummary.model_validate(r) for r in runs]


@router.get("/validation/runs/{run_id}", response_model=ValidationRunOut)
async def get_validation_run(
    run_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ValidationRunOut:
    run = await ValidationService(db).get_run(account.id, run_id)
    if run is None:
        raise NotFoundError("Validation run not found")
    return ValidationRunOut.model_validate(run)


@router.get("/metrics/performance")
async def performance_metrics(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await MetricsService(db).performance(account.id)


@router.get("/pilot/status")
async def pilot_status(account: Account = Depends(get_current_account)) -> dict:
    return {
        "pilot_mode": settings.pilot_mode,
        "message": (
            "Monitored pilot — all clinical safety rules apply. Please file a safety report "
            "for any near-miss or adverse event."
            if settings.pilot_mode
            else "Pilot mode is off."
        ),
    }


@router.get("/regulatory/samd-dossier", response_model=None)
async def samd_dossier(
    format: str = Query(default="json", pattern="^(json|markdown)$"),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse | dict:
    service = RegulatoryService(db)
    await service.record_generation(account.id)
    if format == "markdown":
        return PlainTextResponse(await service.render_markdown(account.id))
    return await service.build_dossier(account.id)


@router.post("/safety-reports", response_model=SafetyReportOut, status_code=201)
async def file_safety_report(
    body: SafetyReportIn,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> SafetyReportOut:
    report = await SafetyReportService(db).file_report(
        account_id=account.id,
        category=body.category,
        severity=body.severity,
        description=body.description,
        patient_id=body.patient_id,
        session_id=body.session_id,
        detail=body.detail,
    )
    return SafetyReportOut.model_validate(report)


@router.get("/safety-reports", response_model=list[SafetyReportOut])
async def list_safety_reports(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[SafetyReportOut]:
    reports = await SafetyReportService(db).list_reports(account.id)
    return [SafetyReportOut.model_validate(r) for r in reports]
