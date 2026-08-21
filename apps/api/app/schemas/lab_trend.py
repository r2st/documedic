"""Lab-trending response schemas.

Response-only. Nothing here is written by a client — the series is computed from the chart's own
rows — so there is no free-text field to sanitise and no bound to enforce on the way in. The one
input the routes take is a marker name in the path, which the route bounds itself.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.core.lab_trend import CrossingKind, ExclusionReason, RangePosition, TrendDirection

# The marker name a client may ask for, as a path segment. Wide enough for the longest real
# analyte name a laboratory prints and narrow enough that the value is not a payload.
MAX_MARKER_NAME_CHARS = 255


class TrendPointResponse(BaseModel):
    """One comparable result, in the series' unit."""

    observation_id: str = Field(
        description="The `lab_results` row this point came from, so a chart can link back to it"
    )
    sample_date: date = Field(description="When the sample was drawn, not when it was reported")
    value: float = Field(description="The result, converted into the series unit")
    reference_low: float | None = Field(
        default=None, description="Lower end of the interval this row's own laboratory quoted"
    )
    reference_high: float | None = Field(
        default=None, description="Upper end of the interval this row's own laboratory quoted"
    )
    position: RangePosition = Field(
        description=(
            "Where this result sits against its own row's interval. `unknown` when the row "
            "quoted no interval — which is not the same as `within`."
        )
    )
    lab_name: str | None = Field(
        default=None,
        description=(
            "Which laboratory reported it, where the report said. Worth showing beside the "
            "point: a step in a series that coincides with a change of laboratory is as likely "
            "to be the assay as the patient."
        ),
    )


class ExcludedObservationResponse(BaseModel):
    """A result that could not join the series, and why not."""

    observation_id: str
    marker_name: str
    value: float | None = None
    unit: str | None = None
    sample_date: date | None = None
    reason: ExclusionReason
    summary: str


class ReferenceBandResponse(BaseModel):
    """The interval to draw behind the whole series, when the rows agree on one."""

    low: float | None = None
    high: float | None = None
    varies: bool = Field(
        description=(
            "True when the rows quoted different intervals, in which case no single band is "
            "honest and `low`/`high` are both null. Render this differently from a series with "
            "no interval at all: one means the laboratories disagreed, the other means nobody "
            "said."
        )
    )


class TrendCrossingResponse(BaseModel):
    """The sample at which the series moved across a boundary its own rows quoted."""

    kind: CrossingKind
    sample_date: date
    from_value: float
    to_value: float
    summary: str


class TrendAnalysisResponse(BaseModel):
    """The arithmetic of the series. Describes the numbers and interprets nothing."""

    direction: TrendDirection
    point_count: int
    first_value: float | None = None
    last_value: float | None = None
    first_sample_date: date | None = None
    last_sample_date: date | None = None
    absolute_change: float | None = None
    percent_change: float | None = None
    interval_days: int | None = None
    change_per_30_days: float | None = Field(
        default=None,
        description=(
            "Rate of change scaled to thirty days. Null when every comparable sample was drawn "
            "on one day — there is no interval to divide by, and an infinite rate is not a "
            "number a clinician can act on."
        ),
    )
    minimum: float | None = None
    maximum: float | None = None
    monotonic: bool = Field(
        default=False,
        description=(
            "True when every step moved the same way. A steady climb and a sawtooth that "
            "happens to end higher share a direction and a change; this is what separates them."
        ),
    )
    significant: bool = Field(
        default=False,
        description=(
            "Whether the change cleared the noise threshold — a quarter of the reference "
            "interval's width where one is known, otherwise a fifth of the starting value. "
            "`direction` is `stable` whenever this is false."
        ),
    )
    crossings: list[TrendCrossingResponse] = Field(default_factory=list)
    summary: str


class LabTrendResponse(BaseModel):
    """One marker's series."""

    marker_name: str = Field(description="The marker as the chart's rows spell it")
    canonical_marker: str | None = Field(
        default=None,
        description=(
            "The curated marker key the spellings resolved to, or null for an analyte this "
            "product does not curate. Null is what tells a client that no unit conversion was "
            "available and the series is grouped on the printed name alone."
        ),
    )
    unit: str | None = Field(
        default=None,
        description=(
            "The unit every `value` on this series is in — the marker's canonical unit where "
            "one is curated, and not necessarily the unit any individual report printed."
        ),
    )
    points: list[TrendPointResponse] = Field(default_factory=list)
    excluded: list[ExcludedObservationResponse] = Field(
        default_factory=list,
        description=(
            "Results the series could not use. Always shown: a silently shortened history is a "
            "chart that answers with fewer results than the record holds."
        ),
    )
    reference: ReferenceBandResponse | None = None
    analysis: TrendAnalysisResponse


class LabTrendListResponse(BaseModel):
    """Every marker on the chart as a series, most recently sampled first."""

    patient_id: str
    trends: list[LabTrendResponse] = Field(default_factory=list)
    observations_considered: int = Field(
        description="Lab rows read to build these series, after the per-read cap"
    )
    truncated: bool = Field(
        description=(
            "True when the chart holds more lab rows than one read considers. The oldest are "
            "the ones dropped, so a `first_value` on a truncated read is not the patient's "
            "first value and a percent change measured from it is measured from an arbitrary "
            "point."
        )
    )
    series_omitted: int = Field(
        description="Series beyond the per-read ceiling that this response does not carry"
    )


class SingleLabTrendResponse(LabTrendResponse):
    """One marker's series, with the same truncation warning the list read carries."""

    truncated: bool = False
