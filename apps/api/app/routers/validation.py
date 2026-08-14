"""Phase 4 routes: validation harness, performance metrics, CDSCO dossier, safety reports."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db
from app.dependencies import get_current_account, rate_limit
from app.exceptions import NotFoundError
from app.models.user import Account
from app.openapi import AUTH_ERRORS, errors
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


@router.post(
    "/validation/run",
    response_model=ValidationRunOut,
    status_code=201,
    summary="Execute the validation case set against the reasoning engine",
    responses=AUTH_ERRORS | errors(429),
    dependencies=[Depends(rate_limit("validation_run"))],
)
async def run_validation(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ValidationRunOut:
    """Replay the curated cases and score the engine's output against their known answers.

    The Phase 4 instrumentation behind the CDSCO SaMD submission: top-1 and top-3 diagnostic
    accuracy, can't-miss recall, citation faithfulness. Each run is persisted so accuracy can
    be tracked across prompt and corpus changes rather than asserted once.

    The most expensive route in the system, and metered per hour rather than per minute
    accordingly: one request replays every vignette, and each vignette costs a full eight-agent
    panel plus its intake rounds. A run takes minutes, so the ceiling is only ever reached by a
    loop.
    """
    run = await ValidationService(db).run(account.id)
    return ValidationRunOut.model_validate(run)


@router.get(
    "/validation/runs",
    response_model=list[ValidationRunSummary],
    summary="Past validation runs",
    responses=AUTH_ERRORS,
)
async def list_validation_runs(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[ValidationRunSummary]:
    """Headline scores per run, newest first — the accuracy trend over time."""
    runs = await ValidationService(db).list_runs(account.id)
    return [ValidationRunSummary.model_validate(r) for r in runs]


@router.get(
    "/validation/runs/{run_id}",
    response_model=ValidationRunOut,
    summary="One validation run, case by case",
    responses=errors(401, 404),
)
async def get_validation_run(
    run_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ValidationRunOut:
    """The full per-case breakdown, including which cases the engine got wrong."""
    run = await ValidationService(db).get_run(account.id, run_id)
    if run is None:
        raise NotFoundError(
            "That validation run was not found. It may have been from a previous "
            "deployment — start a new run from the metrics page.",
            detail=f"validation run {run_id} absent",
        )
    return ValidationRunOut.model_validate(run)


@router.get(
    "/metrics/performance",
    summary="Engine latency and clinician-decision mix",
    responses=AUTH_ERRORS,
)
async def performance_metrics(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Observed pipeline latency plus how clinicians actually responded to suggestions.

    The acceptance/dismissal mix is the automation-bias signal worth watching: near-total
    acceptance is not obviously good news, it may mean the output is being rubber-stamped.
    """
    return await MetricsService(db).performance(account.id)


@router.get(
    "/pilot/status",
    summary="Whether this deployment is a monitored pilot",
    responses=AUTH_ERRORS,
)
async def pilot_status(account: Account = Depends(get_current_account)) -> dict:
    """Pilot mode is a posture, not a permission: every clinical safety rule applies either
    way. What it changes is the expectation that near-misses get reported to
    `POST /safety-reports`."""
    return {
        "pilot_mode": settings.pilot_mode,
        "message": (
            "Monitored pilot — all clinical safety rules apply. Please file a safety report "
            "for any near-miss or adverse event."
            if settings.pilot_mode
            else "Pilot mode is off."
        ),
    }


@router.get(
    "/regulatory/samd-dossier",
    response_model=None,
    summary="Generate the CDSCO SaMD regulatory dossier",
    responses=AUTH_ERRORS,
)
async def samd_dossier(
    format: str = Query(
        default="json",
        pattern="^(json|markdown)$",
        description="`json` for the structured dossier, `markdown` for the rendered document.",
    ),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse | dict:
    """Assemble the device description, risk classification, safety rules and validation
    evidence into the submission dossier.

    Generating it is itself audited (`regulatory_dossier_generated`) — a regulatory artefact
    has to be traceable to the moment and the data it was built from.
    """
    service = RegulatoryService(db)
    await service.record_generation(account.id)
    if format == "markdown":
        return PlainTextResponse(await service.render_markdown(account.id))
    return await service.build_dossier(account.id)


@router.post(
    "/safety-reports",
    response_model=SafetyReportOut,
    status_code=201,
    summary="File a near-miss or adverse-event report",
    responses=AUTH_ERRORS,
)
async def file_safety_report(
    body: SafetyReportIn,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> SafetyReportOut:
    """Post-market surveillance: the clinician-facing channel for anything that went wrong.

    `severity` is one of `near_miss`, `non_serious`, `serious`, `sentinel_event`. Linking
    `patient_id` and `session_id` is optional but is what makes a report investigable against
    the audit trail afterwards.
    """
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


@router.get(
    "/safety-reports",
    response_model=list[SafetyReportOut],
    summary="Safety reports filed on this deployment",
    responses=AUTH_ERRORS,
)
async def list_safety_reports(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[SafetyReportOut]:
    """Newest first. Feeds the post-market surveillance section of the SaMD dossier."""
    reports = await SafetyReportService(db).list_reports(account.id)
    return [SafetyReportOut.model_validate(r) for r in reports]
