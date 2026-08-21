"""Reads a chart's lab history and hands it to the pure trend engine.

The division of labour is the same one every deterministic feature in this codebase uses: the
database work is here and every judgement is in ``app.core.lab_trend``, which imports nothing
that can fail. What this module adds is the ownership check, the bounds, and the audit entry.

The bounds are the part worth reading. A lab history is the fastest-growing collection on a
chart — one row per analyte per panel, forever — and a trend read that fetched all of it would
be a route whose cost grows without limit for a patient who has been managed well for ten years.
Both axes are capped, and both caps are visible in the response rather than silently applied.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lab_safety import canonical_marker
from app.core.lab_trend import LabTrend, TrendObservation, build_trend, build_trends
from app.exceptions import LabMarkerNotFoundError
from app.models.lab_result import LabResult
from app.services.audit_service import AuditService

# The most lab rows one trend read will consider. Chosen to be far above a real chart's traffic
# for the markers a clinician trends — thirty years of quarterly renal panels is under 400 rows
# for one marker — while still bounding the query for a chart that has ingested a decade of
# daily inpatient bloods.
MAX_OBSERVATIONS = 2000

# The most series returned by the all-markers read. A chart with more distinct analytes than
# this is not a chart somebody is reading trends off; it is one that has ingested a research
# panel, and returning 400 sparse series would bury the eight that matter.
MAX_SERIES = 60


@dataclass(frozen=True)
class TrendReport:
    """The series, plus what the caps left out.

    ``truncated`` is not decoration. A trend computed over the newest 2000 rows of a longer
    history has a "first value" that is not the patient's first value, and a percent change
    measured from it is measured from an arbitrary point. A client that knows the read was
    truncated can say so; one that does not will draw the number as though it were the whole
    story.
    """

    trends: list[LabTrend]
    observations_considered: int
    truncated: bool
    series_omitted: int


class LabTrendService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    async def _patient(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> None:
        """The ownership check. Delegated for the reason ``LabSafetyService._patient`` gives.

        Imported inside the method because ``PatientService`` reaches this module's siblings and
        a module-level import would close a cycle.
        """
        from app.services.patient_service import PatientService

        await PatientService(self.db).get(account_id, patient_id)

    async def _observations(
        self, patient_id: uuid.UUID, *, marker_name: str | None = None
    ) -> tuple[list[TrendObservation], bool]:
        """The chart's lab rows as trend input, newest first, capped. Returns ``(rows, truncated)``.

        Newest first, then reversed by the engine's own sort, because the cap has to bite on the
        *oldest* rows: a series truncated at its recent end is not a shortened trend, it is a
        wrong one — the last value would be a result from years ago presented as the current one.

        A soft-deleted row is excluded here rather than filtered afterwards. It is excluded from
        every other clinical read in this codebase, and a value withdrawn from the chart
        appearing on the trend line would be the withdrawal quietly failing.
        """
        statement = select(LabResult).where(
            LabResult.patient_id == patient_id,
            LabResult.is_deleted.is_(False),
        )
        if marker_name is not None:
            statement = statement.where(LabResult.marker_name == marker_name)
        statement = statement.order_by(
            LabResult.sample_date.desc().nullslast(),
            LabResult.id.desc(),
        ).limit(MAX_OBSERVATIONS + 1)

        rows = list((await self.db.execute(statement)).scalars().all())
        truncated = len(rows) > MAX_OBSERVATIONS
        return [
            TrendObservation(
                observation_id=str(row.id),
                marker_name=row.marker_name,
                # Every numeric column is ``Numeric(18, 6)``, which SQLAlchemy hands back as
                # ``Decimal``. The engine is a float computation and mixing the two raises on
                # the first arithmetic, so the widening happens once, here, at the boundary.
                value=None if row.value_numeric is None else float(row.value_numeric),
                unit=row.unit,
                # ``sample_date`` is a timestamp column and the trend axis is days. Trending on
                # the timestamp would put two draws from one morning at different x positions
                # and compute a rate of change over four hours, which extrapolates to a number
                # that is arithmetically correct and clinically meaningless.
                sample_date=None if row.sample_date is None else row.sample_date.date(),
                reference_low=(
                    None if row.reference_range_low is None else float(row.reference_range_low)
                ),
                reference_high=(
                    None if row.reference_range_high is None else float(row.reference_range_high)
                ),
                lab_name=row.lab_name,
            )
            for row in rows[:MAX_OBSERVATIONS]
        ], truncated

    async def trends_for_patient(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, audit: bool = True
    ) -> TrendReport:
        """Every marker on the chart as a series. Audited as ``lab_trends_viewed``.

        Audited because a trend response carries values, units and reference intervals — the
        clinical content of the chart's laboratory history, not a summary of it.
        """
        await self._patient(account_id, patient_id)
        observations, truncated = await self._observations(patient_id)
        trends = build_trends(observations)
        omitted = max(0, len(trends) - MAX_SERIES)
        if audit:
            await self.audit.record(
                action="lab_trends_viewed",
                account_id=account_id,
                patient_id=patient_id,
                entity_type="patient",
                entity_id=patient_id,
                # Counts only. ``audit_logs.payload`` is unencrypted and never pruned, so a
                # marker name — which is a statement about what this patient is being
                # investigated for — does not belong in it.
                payload={
                    "series": min(len(trends), MAX_SERIES),
                    "observations": len(observations),
                    "truncated": truncated,
                },
            )
        return TrendReport(
            trends=trends[:MAX_SERIES],
            observations_considered=len(observations),
            truncated=truncated,
            series_omitted=omitted,
        )

    async def trend_for_marker(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, marker_name: str, audit: bool = True
    ) -> tuple[LabTrend, bool]:
        """One marker's series, by the name as it appears on the chart.

        Returns ``(trend, truncated)``.

        The lookup is deliberately in two stages, and the second is the one that matters. An
        exact ``marker_name`` match finds only the rows spelled exactly that way, and a chart
        that has taken reports from three laboratories spells one analyte three ways — so an
        exact-match-only route would answer "S. Creatinine" with a one-point series and report
        insufficient data on a patient with four years of creatinines. When the requested name
        resolves to a curated marker, the whole chart is read and grouped by
        ``build_trends``, which is what unifies the spellings.

        :class:`LabMarkerNotFoundError` when no row on this chart matches either way, rather than
        an empty series: "this patient has no results for that marker" and "that marker does not
        exist" are different answers, and an empty 200 renders as the first when it may be the
        second.
        """
        await self._patient(account_id, patient_id)
        canonical = canonical_marker(marker_name)
        if canonical is None:
            observations, truncated = await self._observations(patient_id, marker_name=marker_name)
            if not observations:
                raise LabMarkerNotFoundError(
                    detail=f"no lab results named {marker_name!r} on patient {patient_id}"
                )
            trend = build_trend(observations)
        else:
            observations, truncated = await self._observations(patient_id)
            matching = [
                trend for trend in build_trends(observations) if trend.canonical_marker == canonical
            ]
            if not matching:
                raise LabMarkerNotFoundError(
                    detail=f"no lab results for canonical marker {canonical!r} on {patient_id}"
                )
            trend = matching[0]

        if audit:
            await self.audit.record(
                action="lab_trend_viewed",
                account_id=account_id,
                patient_id=patient_id,
                entity_type="patient",
                entity_id=patient_id,
                # The canonical key, not the requested spelling: it is a closed curated
                # vocabulary rather than free text off the wire, which is the distinction
                # ``tests/test_audit_payload_free_text.py`` holds this table to. Absent
                # altogether for an uncurated marker, where the name *is* free text.
                payload={
                    "canonical_marker": canonical,
                    "points": trend.analysis.point_count,
                    "direction": trend.analysis.direction,
                },
            )
        return trend, truncated
