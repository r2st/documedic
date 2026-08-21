"""Longitudinal lab trending — a marker's history as a comparable series, deterministically.

A single lab value answers "is this normal today". The question a chronic disease is actually
managed on is the other one: *which way is it going, and how fast.* A creatinine of 1.4 mg/dL is
unremarkable read alone and is the whole consultation if the same patient was 0.9 four months
ago. This module turns the rows on a chart into the series that question needs.

It is pure — no database, no clock, no network, no configuration (Critical Safety Rule #8, and
``tests/test_offline_engine_purity.py`` pins it). The service layer reads rows and hands them in.

Three things a naive implementation gets wrong, and why each is a patient-safety problem rather
than a cosmetic one.

**1. The units are not the same.** ``lab_results.value_numeric`` is whatever the report printed,
and a chart accumulates reports from several laboratories. One prints creatinine in mg/dL and the
next in µmol/L; one prints glucose in mg/dL and the next in mmol/L. Plotting the raw column puts
``1.1``, ``97`` and ``1.2`` on one axis, which reads as an acute kidney injury that resolved —
a fabricated event, in the direction that starts a workup. Every point here is converted into the
marker's canonical unit through ``app.core.lab_safety``, which is the same conversion the
critical-value guard and the eGFR derivation already trust, and a row whose unit that module
cannot read is **excluded and reported as excluded** rather than plotted as printed. For a marker
the module does not curate there is no conversion factor to be had, so the series keeps the
rows sharing the most common unit and excludes the rest; it never invents an arithmetic.

**2. The reference interval is not the same either.** Each row carries the interval its own
laboratory quoted, and those differ — genuinely, because they differ by assay. Drawing one band
behind the whole series attributes one laboratory's normal range to another's result. So the
band is emitted only when the rows that carry an interval agree on it (after the same unit
conversion the values get — a band in µmol/L drawn behind mg/dL values is bug #1 again), and
otherwise the series says ``varies`` and the client draws none. Per-point intervals are always
returned, so "was *this* result normal" stays answerable either way.

**3. A number that moved is not a trend.** Assay imprecision, a different analyser and the time
of day all move a result without anything happening to the patient. Calling that "rising" is how
a trend arrow becomes something clinicians learn to ignore. A direction is reported only when the
change clears a threshold scaled to the reference interval where one is known — see
:func:`_is_significant` — and everything else is ``stable``, which is a finding and not a
failure to find one.

Nothing here interprets. It says which way the number moved, by how much, over how long, and
when it crossed a boundary somebody else drew. What that means about the patient is the
clinician's, and the wording of every summary keeps it that way (Critical Safety Rule #4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from app.core.lab_safety import (
    canonical_lab_value,
    canonical_marker,
    canonical_unit,
    fold_marker_name,
)

# Fewer than two comparable points is not a trend, it is a result. Stated as a constant because
# two independent places below have to agree about it.
MIN_POINTS_FOR_TREND = 2

# What fraction of the reference interval's width a change must clear before it is called a
# direction rather than noise. A quarter of the normal range is a deliberately unfussy
# threshold: it is wide enough that analytical scatter and a change of analyser do not trip it,
# and narrow enough that a creatinine walking from the bottom of the range to the top — the
# textbook early-CKD picture, entirely inside "normal" — is still reported as rising. The
# alternative anchor, a fixed percentage, is wrong for exactly the markers that matter most:
# 20% of a sodium is a corpse and 20% of a bilirubin is nothing.
REFERENCE_WIDTH_SIGNIFICANCE = 0.25

# The fallback threshold, used only when no usable reference interval is available: a fifth of
# where the patient started. Relative rather than absolute because there is no scale to be
# absolute against — this branch is reached precisely when nobody has told us what normal is.
RELATIVE_SIGNIFICANCE = 0.20

TrendDirection = Literal["rising", "falling", "stable", "insufficient_data"]
RangePosition = Literal["below", "within", "above", "unknown"]
ExclusionReason = Literal["no_value", "no_sample_date", "unconvertible_unit", "unit_not_comparable"]
CrossingKind = Literal["left_reference_high", "left_reference_low", "returned_to_reference"]


@dataclass(frozen=True)
class TrendObservation:
    """One lab row, as the trend engine sees it.

    ``observation_id`` is opaque here — the service passes the ``lab_results`` row id as text so
    that a client can go from a point on a chart back to the report it came from, and this module
    never looks at it. Everything else is the row's own columns, unconverted.
    """

    observation_id: str
    marker_name: str
    value: float | None
    unit: str | None
    sample_date: date | None
    reference_low: float | None = None
    reference_high: float | None = None
    lab_name: str | None = None


@dataclass(frozen=True)
class TrendPoint:
    """One observation placed on the series, in the series' unit."""

    observation_id: str
    sample_date: date
    value: float
    reference_low: float | None
    reference_high: float | None
    position: RangePosition
    lab_name: str | None = None


@dataclass(frozen=True)
class ExcludedObservation:
    """An observation that could not join the series, and the reason it could not.

    Returned rather than dropped. A silently shortened series is a chart that answers the
    question with fewer results than the record holds and says so nowhere — and the excluded row
    is disproportionately the *interesting* one, because the commonest reason to be excluded is
    having come from a different laboratory, which is what happens when a patient is admitted.
    """

    observation_id: str
    marker_name: str
    value: float | None
    unit: str | None
    sample_date: date | None
    reason: ExclusionReason
    summary: str


@dataclass(frozen=True)
class ReferenceBand:
    """The interval to draw behind the whole series, when the rows agree on one.

    ``varies`` and a ``None`` pair are different answers and a client must render them
    differently: the first means the laboratories quoted different intervals and no single band
    is honest, the second means nobody quoted one at all.
    """

    low: float | None
    high: float | None
    varies: bool


@dataclass(frozen=True)
class TrendCrossing:
    """The moment the series moved across a boundary its own rows quoted."""

    kind: CrossingKind
    sample_date: date
    from_value: float
    to_value: float
    summary: str


@dataclass(frozen=True)
class TrendAnalysis:
    """What the series does, in numbers. No clinical interpretation — see the module docstring."""

    direction: TrendDirection
    point_count: int
    first_value: float | None = None
    last_value: float | None = None
    first_sample_date: date | None = None
    last_sample_date: date | None = None
    absolute_change: float | None = None
    percent_change: float | None = None
    interval_days: int | None = None
    change_per_30_days: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    # True when every consecutive step moved the same way (or not at all). A clean slope and a
    # sawtooth that happens to end higher than it started produce the same ``direction`` and the
    # same ``absolute_change``; this is the one field that tells them apart, and it is the
    # difference between a trend and a series of unrelated results.
    monotonic: bool = False
    significant: bool = False
    crossings: tuple[TrendCrossing, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class LabTrend:
    """One marker's series: the comparable points, what was left out, and the arithmetic."""

    marker_name: str
    canonical_marker: str | None
    unit: str | None
    points: tuple[TrendPoint, ...] = ()
    excluded: tuple[ExcludedObservation, ...] = ()
    reference: ReferenceBand | None = None
    analysis: TrendAnalysis = field(
        default_factory=lambda: TrendAnalysis(direction="insufficient_data", point_count=0)
    )


def _normalized_unit_key(unit: str | None) -> str:
    """A loose fold of a printed unit, for grouping rows of an *uncurated* marker.

    Deliberately cruder than ``lab_safety``'s own unit handling and used for nothing but
    equality: this branch has no conversion table to consult, so all it may ever conclude is
    "these two rows are printed in the same unit" or "they are not".
    """
    return "".join(ch for ch in (unit or "").strip().lower() if ch.isalnum() or ch == "/")


def _position(value: float, low: float | None, high: float | None) -> RangePosition:
    """Where one value sits against the interval its own row quoted.

    ``unknown`` when the row quoted neither end. A one-ended interval is a real, usable bound —
    plenty of reports print "< 200" and nothing else — so it is honoured on the side it names and
    cannot conclude anything about the other.
    """
    if low is None and high is None:
        return "unknown"
    if high is not None and value > high:
        return "above"
    if low is not None and value < low:
        return "below"
    return "within"


def _convert(
    observation: TrendObservation, canonical: str
) -> tuple[float, float | None, float | None] | None:
    """``(value, reference low, reference high)`` in the canonical unit, or None if unreadable.

    The reference ends are converted through the same function as the value, using the row's own
    unit, because a laboratory prints its interval in the unit it printed the result in. An end
    that will not convert is dropped rather than carried over unconverted — a bound in the wrong
    unit does not make the point "unknown", it makes it *wrong*, and confidently so.

    The value itself failing to convert takes the whole observation out; there is no series
    position for a number in a unit nobody can read.
    """
    if observation.value is None:
        return None
    converted = canonical_lab_value(observation.marker_name, observation.value, observation.unit)
    if converted is None:
        return None
    _marker, value = converted

    def _bound(raw: float | None) -> float | None:
        if raw is None:
            return None
        pair = canonical_lab_value(observation.marker_name, raw, observation.unit)
        return None if pair is None else pair[1]

    return value, _bound(observation.reference_low), _bound(observation.reference_high)


def _exclusion(
    observation: TrendObservation, reason: ExclusionReason, summary: str
) -> ExcludedObservation:
    return ExcludedObservation(
        observation_id=observation.observation_id,
        marker_name=observation.marker_name,
        value=observation.value,
        unit=observation.unit,
        sample_date=observation.sample_date,
        reason=reason,
        summary=summary,
    )


def _reference_band(points: tuple[TrendPoint, ...]) -> ReferenceBand | None:
    """The one interval every quoting point agrees on, or a ``varies`` band, or None.

    Points that quote no interval at all are not disagreement — a laboratory that omitted the
    range has not contradicted one that supplied it — so they are ignored here rather than
    counted as a third opinion that forces ``varies``.
    """
    quoted = {
        (point.reference_low, point.reference_high)
        for point in points
        if point.reference_low is not None or point.reference_high is not None
    }
    if not quoted:
        return None
    if len(quoted) > 1:
        return ReferenceBand(low=None, high=None, varies=True)
    low, high = next(iter(quoted))
    return ReferenceBand(low=low, high=high, varies=False)


def _is_significant(change: float, first: float, band: ReferenceBand | None) -> bool:
    """Whether a change is large enough to be called a direction. See the module docstring.

    The reference-width anchor is preferred wherever a two-ended band is available and has a
    width; it is the only one of the two that carries any information about the marker's own
    scale. The relative fallback is anchored on the *first* value rather than the mean, so that
    the threshold does not move as later results arrive: a series must not change its mind about
    whether the first two points were a trend because a third one turned up.
    """
    magnitude = abs(change)
    if band is not None and not band.varies and band.low is not None and band.high is not None:
        width = band.high - band.low
        if width > 0:
            return magnitude >= width * REFERENCE_WIDTH_SIGNIFICANCE
    if first == 0:
        # No scale to be relative to, and no band. Any movement off zero is all the signal
        # there is; calling it stable would be a claim, not a default.
        return magnitude > 0
    return magnitude / abs(first) >= RELATIVE_SIGNIFICANCE


def _crossings(points: tuple[TrendPoint, ...]) -> tuple[TrendCrossing, ...]:
    """Boundary crossings between consecutive points, in order.

    Only between two points that both know where they stand: a step from a point whose row
    quoted no interval says nothing about a boundary, and reporting one would attribute the
    later row's range to the earlier row's result.
    """
    found: list[TrendCrossing] = []
    for previous, current in zip(points, points[1:], strict=False):
        if previous.position == "unknown" or current.position == "unknown":
            continue
        if previous.position == current.position:
            continue
        if current.position == "above":
            kind: CrossingKind = "left_reference_high"
            summary = "rose above the quoted reference interval at this sample"
        elif current.position == "below":
            kind = "left_reference_low"
            summary = "fell below the quoted reference interval at this sample"
        else:
            kind = "returned_to_reference"
            summary = "returned inside the quoted reference interval at this sample"
        found.append(
            TrendCrossing(
                kind=kind,
                sample_date=current.sample_date,
                from_value=previous.value,
                to_value=current.value,
                summary=summary,
            )
        )
    return tuple(found)


def _analyse(points: tuple[TrendPoint, ...], band: ReferenceBand | None) -> TrendAnalysis:
    if len(points) < MIN_POINTS_FOR_TREND:
        return TrendAnalysis(
            direction="insufficient_data",
            point_count=len(points),
            first_value=points[0].value if points else None,
            last_value=points[-1].value if points else None,
            first_sample_date=points[0].sample_date if points else None,
            last_sample_date=points[-1].sample_date if points else None,
            minimum=min((p.value for p in points), default=None),
            maximum=max((p.value for p in points), default=None),
            summary=(
                "One comparable result — a second is needed before any direction can be read."
                if points
                else "No comparable results for this marker."
            ),
        )

    first, last = points[0], points[-1]
    change = last.value - first.value
    interval_days = (last.sample_date - first.sample_date).days
    significant = _is_significant(change, first.value, band)
    direction: TrendDirection = "stable"
    if significant and change > 0:
        direction = "rising"
    elif significant and change < 0:
        direction = "falling"

    steps = [b.value - a.value for a, b in zip(points, points[1:], strict=False)]
    monotonic = all(step >= 0 for step in steps) or all(step <= 0 for step in steps)

    # A rate needs a denominator. Several samples drawn on one day is an ordinary inpatient
    # morning, and dividing by that zero would either raise or produce an infinity that renders
    # as a rate of change nobody can act on.
    rate = (change / interval_days) * 30 if interval_days > 0 else None

    return TrendAnalysis(
        direction=direction,
        point_count=len(points),
        first_value=first.value,
        last_value=last.value,
        first_sample_date=first.sample_date,
        last_sample_date=last.sample_date,
        absolute_change=change,
        percent_change=(change / abs(first.value) * 100) if first.value != 0 else None,
        interval_days=interval_days,
        change_per_30_days=rate,
        minimum=min(p.value for p in points),
        maximum=max(p.value for p in points),
        monotonic=monotonic,
        significant=significant,
        crossings=_crossings(points),
        summary=_summarise(direction, len(points), interval_days, monotonic),
    )


def _summarise(direction: TrendDirection, count: int, interval_days: int, monotonic: bool) -> str:
    """One line of prescriber-framed prose. Describes the numbers; concludes nothing.

    "Rising" is a property of the series and is safe to state. What a rising creatinine means for
    this patient is not, and no sentence built here goes near it (Critical Safety Rule #4).
    """
    span = f"{count} results over {interval_days} days" if interval_days else f"{count} results"
    if direction == "stable":
        return f"No material change across {span} against the quoted reference interval."
    shape = "steadily" if monotonic else "with variation between samples"
    return f"Values have been {direction} {shape} across {span}."


def build_trend(observations: list[TrendObservation]) -> LabTrend:
    """One marker's series from its rows. The caller has already grouped them; see
    :func:`build_trends` for the grouping, which is not as obvious as it looks.

    Rows are sorted here rather than trusted from the caller. The order of a series is what every
    number below means — a "change" computed over rows in insertion order is the difference
    between two arbitrary results — and it is not the order a database returns them in.
    """
    if not observations:
        return LabTrend(marker_name="", canonical_marker=None, unit=None)

    marker_name = observations[0].marker_name
    canonical = canonical_marker(marker_name)
    excluded: list[ExcludedObservation] = []
    placed: list[TrendPoint] = []

    if canonical is not None:
        unit = canonical_unit(canonical)
        for observation in observations:
            if observation.value is None:
                excluded.append(
                    _exclusion(
                        observation,
                        "no_value",
                        "Reported without a numeric value, so it cannot be placed on a series.",
                    )
                )
                continue
            if observation.sample_date is None:
                excluded.append(
                    _exclusion(
                        observation,
                        "no_sample_date",
                        "No sample date, so there is no point on the time axis to place it at.",
                    )
                )
                continue
            converted = _convert(observation, canonical)
            if converted is None:
                excluded.append(
                    _exclusion(
                        observation,
                        "unconvertible_unit",
                        (
                            f"Reported in {observation.unit or 'no stated unit'}, which cannot be "
                            f"converted to {unit} — plotting it beside the others would compare "
                            "two different scales."
                        ),
                    )
                )
                continue
            value, low, high = converted
            placed.append(
                TrendPoint(
                    observation_id=observation.observation_id,
                    sample_date=observation.sample_date,
                    value=value,
                    reference_low=low,
                    reference_high=high,
                    position=_position(value, low, high),
                    lab_name=observation.lab_name,
                )
            )
    else:
        unit = _dominant_unit(observations)
        for observation in observations:
            if observation.value is None:
                excluded.append(
                    _exclusion(
                        observation,
                        "no_value",
                        "Reported without a numeric value, so it cannot be placed on a series.",
                    )
                )
                continue
            if observation.sample_date is None:
                excluded.append(
                    _exclusion(
                        observation,
                        "no_sample_date",
                        "No sample date, so there is no point on the time axis to place it at.",
                    )
                )
                continue
            if _normalized_unit_key(observation.unit) != _normalized_unit_key(unit):
                excluded.append(
                    _exclusion(
                        observation,
                        "unit_not_comparable",
                        (
                            f"Reported in {observation.unit or 'no stated unit'} where the rest of "
                            f"this series is in {unit or 'no stated unit'}. This marker is not one "
                            "with a curated conversion, so the two cannot be placed on one axis."
                        ),
                    )
                )
                continue
            placed.append(
                TrendPoint(
                    observation_id=observation.observation_id,
                    sample_date=observation.sample_date,
                    value=observation.value,
                    reference_low=observation.reference_low,
                    reference_high=observation.reference_high,
                    position=_position(
                        observation.value, observation.reference_low, observation.reference_high
                    ),
                    lab_name=observation.lab_name,
                )
            )

    # Oldest first, and ties broken by the observation id so that two samples drawn on one day
    # order the same way on every call. A series whose point order depends on dictionary or
    # result-set ordering produces a different "change since the previous result" per request.
    points = tuple(sorted(placed, key=lambda p: (p.sample_date, p.observation_id)))
    band = _reference_band(points)
    return LabTrend(
        marker_name=marker_name,
        canonical_marker=canonical,
        unit=unit,
        points=points,
        excluded=tuple(excluded),
        reference=band,
        analysis=_analyse(points, band),
    )


def _dominant_unit(observations: list[TrendObservation]) -> str | None:
    """The unit most of an uncurated marker's rows are printed in.

    Ties are broken towards the unit of the most recent dated row rather than by count alone or
    by first-seen: what the patient's current laboratory prints is the series a clinician is
    reading, and the older spelling is the one that should be shown as excluded. A row with no
    date cannot break the tie and does not try to.
    """
    counts: dict[str, int] = {}
    display: dict[str, str | None] = {}
    for observation in observations:
        if observation.value is None or observation.sample_date is None:
            continue
        key = _normalized_unit_key(observation.unit)
        counts[key] = counts.get(key, 0) + 1
        display.setdefault(key, observation.unit)
    if not counts:
        return None
    best = max(counts.values())
    contenders = {key for key, count in counts.items() if count == best}
    if len(contenders) > 1:
        dated = [
            o
            for o in observations
            if o.value is not None
            and o.sample_date is not None
            and _normalized_unit_key(o.unit) in contenders
        ]
        newest = max(dated, key=lambda o: (o.sample_date, o.observation_id))
        return newest.unit
    return display[next(iter(contenders))]


def build_trends(observations: list[TrendObservation]) -> list[LabTrend]:
    """Every marker's series, grouped, most-recently-sampled series first.

    The grouping is where the value is. Rows are grouped by the *canonical* marker where one is
    known, which is what makes "S. Creatinine" from one laboratory and "Serum Creatinine" from
    the next a single four-year series rather than two unrelated results; a chart that spells the
    marker three ways otherwise shows three trends of one point each, and a trend of one point is
    reported — correctly, and uselessly — as insufficient data.

    An uncurated marker groups on its folded spelling only. That is the conservative half of the
    same rule: this module may reduce punctuation and case, and may not decide that two names
    nobody curated are the same test.
    """
    groups: dict[str, list[TrendObservation]] = {}
    for observation in observations:
        canonical = canonical_marker(observation.marker_name)
        key = (
            canonical
            if canonical is not None
            else f"raw:{fold_marker_name(observation.marker_name)}"
        )
        groups.setdefault(key, []).append(observation)

    trends = [build_trend(rows) for rows in groups.values()]
    # Most recent activity first: a clinician opening a chart wants the marker something is
    # happening to, not the alphabet. A series with no dated point sorts last rather than
    # crashing the comparison.
    return sorted(
        trends,
        key=lambda t: (
            t.analysis.last_sample_date is not None,
            t.analysis.last_sample_date or date.min,
            t.marker_name,
        ),
        reverse=True,
    )
