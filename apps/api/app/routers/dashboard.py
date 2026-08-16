"""Clinical dashboard routes: aggregates over the calling account's own panel.

Not patient-scoped, unlike almost everything else in this API — which is precisely why every
query behind these routes is anchored to `patients.account_id`. See
`app.services.dashboard_service` for the argument, and `tests/test_dashboard_metrics.py` for the
cross-account assertions that hold it.

Unmetered. Each of these is a handful of aggregate queries over one account's rows, bounded by
the panel rather than by the request, and a dashboard is the kind of screen that polls — a 429
here would show a clinician an empty tile with no explanation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.user import Account
from app.openapi import AUTH_ERRORS
from app.schemas.dashboard import (
    DashboardOverview,
    EncounterCounts,
    MedicationCounts,
    PatientCounts,
    SafetyFlagCounts,
)
from app.services.dashboard_service import DEFAULT_TOP_N, MAX_TOP_N, DashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_TOP_N = Query(
    default=DEFAULT_TOP_N,
    ge=1,
    le=MAX_TOP_N,
    description="How many drugs the most-prescribed list returns.",
)


@router.get(
    "",
    response_model=DashboardOverview,
    summary="Every dashboard tile in one request",
    responses=AUTH_ERRORS,
)
async def overview(
    limit: int = _TOP_N,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DashboardOverview:
    """Panel counts, encounter volume, most-prescribed drugs and the safety-flag distribution.

    All four tiles for a dashboard that renders them together, so a single screen is a single
    round trip. The individual routes below return the same objects for a client that refreshes
    one tile at a time.

    Aggregates only — no patient is identified in any of these responses, and no clinical
    content leaves the system through them.
    """
    return DashboardOverview.model_validate(
        await DashboardService(db).overview(account.id, limit=limit)
    )


@router.get(
    "/patients",
    response_model=PatientCounts,
    summary="Patient count by chart status",
    responses=AUTH_ERRORS,
)
async def patient_counts(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> PatientCounts:
    """How many charts this account holds, and how many of them are usable.

    `withdrawn` charts are counted, not hidden: a chart withdrawn under the DPDP Act is soft
    deleted rather than removed, and a dashboard whose total silently shrank would be reporting
    an erasure as an absence.

    `date_of_birth_missing` and `weight_missing` are safety numbers rather than administrative
    ones — every age-based check needs the first, and the paediatric dose check needs the
    second, so each missing value is a check that cannot run on that chart.
    """
    return PatientCounts.model_validate(await DashboardService(db).patients(account.id))


@router.get(
    "/encounters",
    response_model=EncounterCounts,
    summary="Encounter volume by type and lifecycle status",
    responses=AUTH_ERRORS,
)
async def encounter_counts(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> EncounterCounts:
    """Visit counts on two axes: what the visits were, and how many have been signed.

    An encounter with no recorded type is grouped as `(unspecified)` rather than dropped — the
    column is nullable and extraction routinely cannot read a type off a scanned note, so
    dropping them would understate the total.
    """
    return EncounterCounts.model_validate(await DashboardService(db).encounters(account.id))


@router.get(
    "/medications",
    response_model=MedicationCounts,
    summary="Most prescribed medications on this panel",
    responses=AUTH_ERRORS,
)
async def medication_counts(
    limit: int = _TOP_N,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> MedicationCounts:
    """Ranked by how many distinct patients are on each drug, not by how many rows it has.

    A titration that produced six `change` events for one patient is one patient on that drug;
    ranking by rows would put whichever drug is adjusted most often at the top of a list read as
    "what we prescribe most".

    Names are grouped as the chart wrote them — the generic where there is one, the brand
    otherwise — and are not resolved through the drug vocabulary here. An unresolved brand
    therefore appears under its own name rather than being folded into its INN.
    """
    return MedicationCounts.model_validate(
        await DashboardService(db).medications(account.id, limit=limit)
    )


@router.get(
    "/safety-flags",
    response_model=SafetyFlagCounts,
    summary="Safety-flag frequency distribution",
    responses=AUTH_ERRORS,
)
async def safety_flag_counts(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> SafetyFlagCounts:
    """Which deterministic checks fire, how often, and how much of it is a hard block.

    This is the alert-fatigue instrument: a check firing on nearly every screen is a check
    clinicians have learned to click through, and that is not visible from any per-patient view.

    Cumulative over this account's whole history — `drug_safety_checks` is append-only — not a
    snapshot of what is currently flagged. `GET /patients/{id}/drug-safety/flags` is the other
    number.
    """
    return SafetyFlagCounts.model_validate(await DashboardService(db).safety_flags(account.id))
