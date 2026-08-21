"""Longitudinal lab trending: the series, the unit harmonisation, and the routes.

The bug this feature exists to *not* have is in ``test_a_series_reported_in_two_units_is_not_two
_scales_on_one_axis``. Everything else is scaffolding around keeping that true.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.core.lab_trend import (
    LabTrend,
    TrendObservation,
    build_trend,
    build_trends,
)
from app.models.audit_log import AuditLog
from app.models.lab_result import LabResult
from app.models.patient import Patient
from tests.conftest import create_patient


def _obs(
    ident: str,
    marker: str,
    value: float | None,
    unit: str | None,
    day: date | None,
    *,
    low: float | None = None,
    high: float | None = None,
    lab: str | None = None,
) -> TrendObservation:
    return TrendObservation(
        observation_id=ident,
        marker_name=marker,
        value=value,
        unit=unit,
        sample_date=day,
        reference_low=low,
        reference_high=high,
        lab_name=lab,
    )


# --- The unit problem ---------------------------------------------------------------------


async def test_a_series_reported_in_two_units_is_not_two_scales_on_one_axis():
    """The headline. A creatinine of 97 µmol/L *is* a creatinine of 1.1 mg/dL.

    Plotting the raw ``value_numeric`` column puts 1.1, 97 and 1.2 on one axis, which reads as
    an acute kidney injury that resolved between January and March — a fabricated event, and one
    that starts a workup. Every point must arrive in one unit.
    """
    trend = build_trend(
        [
            _obs("a", "Creatinine", 1.1, "mg/dL", date(2026, 1, 1)),
            _obs("b", "S. Creatinine", 97.0, "umol/L", date(2026, 2, 1)),
            _obs("c", "Creatinine", 1.2, "mg/dL", date(2026, 3, 1)),
        ]
    )
    assert trend.unit == "mg/dl"
    assert [round(p.value, 2) for p in trend.points] == [1.1, 1.1, 1.2]
    # And therefore no dramatic swing to report.
    assert trend.analysis.direction == "stable"
    assert trend.excluded == ()


async def test_a_unit_the_module_cannot_read_is_excluded_and_named_not_plotted():
    """The alternative to excluding it is plotting it as printed, which is the bug above."""
    trend = build_trend(
        [
            _obs("a", "Creatinine", 1.1, "mg/dL", date(2026, 1, 1)),
            _obs("b", "Creatinine", 4.2, "furlongs", date(2026, 2, 1)),
        ]
    )
    assert [p.observation_id for p in trend.points] == ["a"]
    assert [(e.observation_id, e.reason) for e in trend.excluded] == [("b", "unconvertible_unit")]
    assert "furlongs" in trend.excluded[0].summary


async def test_a_reference_interval_is_converted_with_the_value_it_came_with():
    """A band in µmol/L drawn behind mg/dL values is the same bug wearing the answer's clothes.

    Both ends travel through the same conversion as the value, using that row's own unit.
    """
    trend = build_trend(
        [
            _obs("a", "Creatinine", 97.0, "umol/L", date(2026, 1, 1), low=53.0, high=115.0),
        ]
    )
    point = trend.points[0]
    assert round(point.value, 2) == 1.1
    assert point.reference_low is not None and round(point.reference_low, 2) == 0.6
    assert point.reference_high is not None and round(point.reference_high, 2) == 1.3
    assert point.position == "within"


async def test_an_uncurated_marker_is_grouped_by_its_commonest_unit_and_never_converted():
    """No conversion table exists for an analyte nobody curated, so none is invented."""
    trend = build_trend(
        [
            _obs("a", "Anti-TPO", 12.0, "IU/mL", date(2026, 1, 1)),
            _obs("b", "Anti TPO", 15.0, "IU/mL", date(2026, 2, 1)),
            _obs("c", "Anti-TPO", 900.0, "pmol/L", date(2026, 3, 1)),
        ]
    )
    assert trend.canonical_marker is None
    assert [p.observation_id for p in trend.points] == ["a", "b"]
    assert [(e.observation_id, e.reason) for e in trend.excluded] == [("c", "unit_not_comparable")]


async def test_a_tie_on_unit_frequency_is_broken_towards_the_most_recent_sample():
    """What the patient's current laboratory prints is the series a clinician is reading."""
    trend = build_trend(
        [
            _obs("a", "Widget Index", 10.0, "old-units", date(2026, 1, 1)),
            _obs("b", "Widget Index", 11.0, "new-units", date(2026, 2, 1)),
        ]
    )
    assert trend.unit == "new-units"
    assert [p.observation_id for p in trend.points] == ["b"]


# --- Ordering and placement ---------------------------------------------------------------


async def test_rows_are_sorted_by_sample_date_not_taken_in_the_order_given():
    """ "Change" computed over rows in insertion order is the difference between two arbitrary
    results, and insertion order is not something a caller can be trusted to have."""
    trend = build_trend(
        [
            _obs("c", "Potassium", 5.4, "mmol/L", date(2026, 3, 1)),
            _obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1)),
            _obs("b", "Potassium", 4.7, "mmol/L", date(2026, 2, 1)),
        ]
    )
    assert [p.observation_id for p in trend.points] == ["a", "b", "c"]
    assert trend.analysis.first_value == 4.0
    assert trend.analysis.last_value == 5.4


async def test_two_samples_on_one_day_order_identically_on_every_call():
    """A point order that depends on result-set ordering gives a different "change since the
    previous result" per request, for one unchanged chart."""
    rows = [
        _obs("zz", "Potassium", 5.0, "mmol/L", date(2026, 1, 1)),
        _obs("aa", "Potassium", 4.0, "mmol/L", date(2026, 1, 1)),
    ]
    first = [p.observation_id for p in build_trend(rows).points]
    second = [p.observation_id for p in build_trend(list(reversed(rows))).points]
    assert first == second == ["aa", "zz"]


async def test_an_undated_result_is_excluded_rather_than_appended_to_the_end():
    """Appending it fabricates the most recent value — which is the number every rendering of a
    trend leads with."""
    trend = build_trend(
        [
            _obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1)),
            _obs("b", "Potassium", 6.9, "mmol/L", None),
        ]
    )
    assert [p.observation_id for p in trend.points] == ["a"]
    assert trend.excluded[0].reason == "no_sample_date"
    assert trend.analysis.last_value == 4.0


async def test_a_qualitative_result_with_no_number_is_excluded_as_such():
    trend = build_trend(
        [
            _obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1)),
            _obs("b", "Potassium", None, "mmol/L", date(2026, 2, 1)),
        ]
    )
    assert [(e.observation_id, e.reason) for e in trend.excluded] == [("b", "no_value")]


# --- The reference band -------------------------------------------------------------------


async def test_one_band_is_drawn_only_when_the_rows_agree_on_it():
    trend = build_trend(
        [
            _obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1), low=3.5, high=5.1),
            _obs("b", "Potassium", 4.2, "mmol/L", date(2026, 2, 1), low=3.5, high=5.1),
        ]
    )
    assert trend.reference is not None
    assert (trend.reference.low, trend.reference.high, trend.reference.varies) == (3.5, 5.1, False)


async def test_laboratories_that_quoted_different_intervals_produce_no_band():
    """Drawing one attributes one laboratory's normal range to another's result."""
    trend = build_trend(
        [
            _obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1), low=3.5, high=5.1),
            _obs("b", "Potassium", 4.2, "mmol/L", date(2026, 2, 1), low=3.3, high=5.5),
        ]
    )
    assert trend.reference is not None
    assert trend.reference.varies is True
    assert trend.reference.low is None and trend.reference.high is None
    # The per-point intervals survive, so "was *this* result normal" is still answerable.
    assert [(p.reference_low, p.reference_high) for p in trend.points] == [(3.5, 5.1), (3.3, 5.5)]


async def test_a_row_quoting_no_interval_is_not_counted_as_disagreement():
    """A laboratory that omitted the range has not contradicted one that supplied it."""
    trend = build_trend(
        [
            _obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1), low=3.5, high=5.1),
            _obs("b", "Potassium", 4.2, "mmol/L", date(2026, 2, 1)),
        ]
    )
    assert trend.reference is not None and trend.reference.varies is False


async def test_no_interval_anywhere_is_a_null_band_not_a_varying_one():
    """Different answers: "the laboratories disagreed" and "nobody said"."""
    trend = build_trend([_obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1))])
    assert trend.reference is None


async def test_a_one_ended_interval_is_honoured_on_the_side_it_names():
    """Plenty of reports print "< 200" and nothing else."""
    trend = build_trend(
        [_obs("a", "LDL Cholesterol", 240.0, "mg/dL", date(2026, 1, 1), high=100.0)]
    )
    assert trend.points[0].position == "above"


async def test_a_row_with_no_interval_is_position_unknown_and_not_within():
    """ "Inside the range" and "nobody said what the range is" must not render the same."""
    trend = build_trend([_obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1))])
    assert trend.points[0].position == "unknown"


# --- The analysis -------------------------------------------------------------------------


async def test_one_result_is_insufficient_data_not_a_flat_line():
    trend = build_trend([_obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1))])
    assert trend.analysis.direction == "insufficient_data"
    assert trend.analysis.point_count == 1
    assert trend.analysis.absolute_change is None


async def test_analytical_noise_inside_the_reference_interval_is_stable_not_rising():
    """A trend arrow that fires on assay scatter is one clinicians learn to ignore."""
    trend = build_trend(
        [
            _obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1), low=3.5, high=5.1),
            _obs("b", "Potassium", 4.1, "mmol/L", date(2026, 3, 1), low=3.5, high=5.1),
        ]
    )
    assert trend.analysis.direction == "stable"
    assert trend.analysis.significant is False


async def test_a_walk_across_the_reference_interval_is_rising_even_while_it_stays_normal():
    """The textbook early-CKD picture: every result "normal", the patient losing kidney.

    A threshold anchored on "is the latest value abnormal" reports nothing here, which is the
    whole reason to trend rather than screen.
    """
    trend = build_trend(
        [
            _obs("a", "Creatinine", 0.65, "mg/dL", date(2024, 1, 1), low=0.6, high=1.3),
            _obs("b", "Creatinine", 0.95, "mg/dL", date(2025, 1, 1), low=0.6, high=1.3),
            _obs("c", "Creatinine", 1.25, "mg/dL", date(2026, 1, 1), low=0.6, high=1.3),
        ]
    )
    assert trend.analysis.direction == "rising"
    assert trend.analysis.significant is True
    assert trend.analysis.monotonic is True
    assert all(p.position == "within" for p in trend.points)


async def test_a_sawtooth_that_ends_higher_is_reported_as_not_monotonic():
    """A clean slope and a series of unrelated results share a direction and a change; this is
    the one field that tells them apart."""
    trend = build_trend(
        [
            _obs("a", "Creatinine", 0.7, "mg/dL", date(2024, 1, 1), low=0.6, high=1.3),
            _obs("b", "Creatinine", 1.4, "mg/dL", date(2025, 1, 1), low=0.6, high=1.3),
            _obs("c", "Creatinine", 1.3, "mg/dL", date(2026, 1, 1), low=0.6, high=1.3),
        ]
    )
    assert trend.analysis.direction == "rising"
    assert trend.analysis.monotonic is False
    assert "variation between samples" in trend.analysis.summary


async def test_the_threshold_falls_back_to_a_relative_one_when_no_band_is_available():
    """A fifth of where the patient started, because there is no scale to be absolute against."""
    small = build_trend(
        [
            _obs("a", "Widget Index", 100.0, "u", date(2026, 1, 1)),
            _obs("b", "Widget Index", 110.0, "u", date(2026, 2, 1)),
        ]
    )
    large = build_trend(
        [
            _obs("a", "Widget Index", 100.0, "u", date(2026, 1, 1)),
            _obs("b", "Widget Index", 140.0, "u", date(2026, 2, 1)),
        ]
    )
    assert small.analysis.direction == "stable"
    assert large.analysis.direction == "rising"


async def test_a_varying_band_does_not_get_used_as_a_significance_anchor():
    """The band is null *and* meaningless there; anchoring on it would use one laboratory's
    width to judge another laboratory's result."""
    trend = build_trend(
        [
            _obs("a", "Widget Index", 100.0, "u", date(2026, 1, 1), low=0.0, high=1000.0),
            _obs("b", "Widget Index", 140.0, "u", date(2026, 2, 1), low=0.0, high=10.0),
        ]
    )
    assert trend.reference is not None and trend.reference.varies is True
    # Falls back to the relative rule: 40% of 100 clears a fifth.
    assert trend.analysis.direction == "rising"


async def test_several_draws_on_one_day_produce_no_rate_rather_than_a_division_by_zero():
    """An ordinary inpatient morning. An infinite rate is not a number a clinician can act on."""
    trend = build_trend(
        [
            _obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1)),
            _obs("b", "Potassium", 6.0, "mmol/L", date(2026, 1, 1)),
        ]
    )
    assert trend.analysis.interval_days == 0
    assert trend.analysis.change_per_30_days is None


async def test_the_rate_is_scaled_to_thirty_days():
    trend = build_trend(
        [
            _obs("a", "Creatinine", 1.0, "mg/dL", date(2026, 1, 1)),
            _obs("b", "Creatinine", 1.6, "mg/dL", date(2026, 3, 31)),
        ]
    )
    assert trend.analysis.interval_days == 89
    assert trend.analysis.change_per_30_days is not None
    assert round(trend.analysis.change_per_30_days, 3) == round(0.6 / 89 * 30, 3)


async def test_a_start_of_zero_reports_no_percentage_rather_than_dividing_by_it():
    trend = build_trend(
        [
            _obs("a", "Widget Index", 0.0, "u", date(2026, 1, 1)),
            _obs("b", "Widget Index", 5.0, "u", date(2026, 2, 1)),
        ]
    )
    assert trend.analysis.percent_change is None
    assert trend.analysis.direction == "rising"


async def test_the_summary_states_what_the_numbers_did_and_diagnoses_nothing():
    """Critical Safety Rule #4 — no certainty language, no clinical conclusion."""
    trend = build_trend(
        [
            _obs("a", "Creatinine", 0.7, "mg/dL", date(2024, 1, 1), low=0.6, high=1.3),
            _obs("b", "Creatinine", 1.6, "mg/dL", date(2026, 1, 1), low=0.6, high=1.3),
        ]
    )
    lowered = trend.analysis.summary.lower()
    for banned in ("the patient has", "diagnos", "give ", "administer", "start ", "renal failure"):
        assert banned not in lowered


# --- Crossings ----------------------------------------------------------------------------


async def test_leaving_and_returning_to_the_interval_are_both_reported_in_order():
    trend = build_trend(
        [
            _obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1), low=3.5, high=5.1),
            _obs("b", "Potassium", 5.9, "mmol/L", date(2026, 2, 1), low=3.5, high=5.1),
            _obs("c", "Potassium", 4.4, "mmol/L", date(2026, 3, 1), low=3.5, high=5.1),
            _obs("d", "Potassium", 3.1, "mmol/L", date(2026, 4, 1), low=3.5, high=5.1),
        ]
    )
    assert [(c.kind, c.sample_date.month) for c in trend.analysis.crossings] == [
        ("left_reference_high", 2),
        ("returned_to_reference", 3),
        ("left_reference_low", 4),
    ]


async def test_a_step_involving_a_point_with_no_interval_reports_no_crossing():
    """Reporting one would attribute the later row's range to the earlier row's result."""
    trend = build_trend(
        [
            _obs("a", "Potassium", 4.0, "mmol/L", date(2026, 1, 1)),
            _obs("b", "Potassium", 6.9, "mmol/L", date(2026, 2, 1), low=3.5, high=5.1),
        ]
    )
    assert trend.analysis.crossings == ()


# --- Grouping ---------------------------------------------------------------------------


async def test_three_spellings_of_one_analyte_are_one_series_not_three():
    """A chart that spells the marker three ways otherwise shows three trends of one point each,
    and a trend of one point is reported — correctly, and uselessly — as insufficient data."""
    trends = build_trends(
        [
            _obs("a", "S. Creatinine", 0.8, "mg/dL", date(2024, 1, 1)),
            _obs("b", "Serum Creatinine", 1.1, "mg/dL", date(2025, 1, 1)),
            _obs("c", "Creatinine", 1.5, "mg/dL", date(2026, 1, 1)),
        ]
    )
    assert len(trends) == 1
    assert trends[0].analysis.point_count == 3
    assert trends[0].analysis.direction == "rising"


async def test_two_uncurated_names_are_not_declared_to_be_the_same_test():
    """The conservative half of the rule: reduce punctuation, never decide two names mean one
    analyte nobody curated."""
    trends = build_trends(
        [
            _obs("a", "Urine Protein", 1.0, "g/L", date(2026, 1, 1)),
            _obs("b", "Protein", 70.0, "g/L", date(2026, 2, 1)),
        ]
    )
    assert len(trends) == 2


async def test_series_are_ordered_by_most_recent_activity():
    trends = build_trends(
        [
            _obs("a", "Potassium", 4.0, "mmol/L", date(2020, 1, 1)),
            _obs("b", "Creatinine", 1.0, "mg/dL", date(2026, 1, 1)),
        ]
    )
    assert [t.canonical_marker for t in trends] == ["creatinine", "potassium"]


async def test_a_series_with_no_dated_point_sorts_last_rather_than_crashing():
    trends = build_trends(
        [
            _obs("a", "Potassium", 4.0, "mmol/L", None),
            _obs("b", "Creatinine", 1.0, "mg/dL", date(2026, 1, 1)),
        ]
    )
    assert [t.canonical_marker for t in trends] == ["creatinine", "potassium"]
    assert trends[-1].points == ()


async def test_no_observations_is_an_empty_trend_not_an_exception():
    assert build_trends([]) == []
    empty = build_trend([])
    assert isinstance(empty, LabTrend)
    assert empty.analysis.direction == "insufficient_data"


# --- The routes ---------------------------------------------------------------------------


async def _chart_with_labs(auth_client, db, rows: list[dict]) -> str:
    patient = await create_patient(auth_client)
    record = await db.get(Patient, uuid.UUID(patient["id"]))
    assert record is not None
    for row in rows:
        db.add(LabResult(patient_id=record.id, **row))
    await db.commit()
    return patient["id"]


def _lab(marker: str, value: float, unit: str, day: int, **extra) -> dict:
    return {
        "marker_name": marker,
        "value_numeric": value,
        "unit": unit,
        "sample_date": datetime(2026, 1, day, tzinfo=UTC),
        **extra,
    }


async def test_the_trends_route_returns_one_series_per_marker(auth_client, db):
    pid = await _chart_with_labs(
        auth_client,
        db,
        [
            _lab("Creatinine", 0.9, "mg/dL", 1),
            _lab("Creatinine", 1.6, "mg/dL", 20),
            _lab("Potassium", 4.1, "mmol/L", 1),
        ],
    )
    resp = await auth_client.get(f"/api/v1/patients/{pid}/labs/trends")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert {t["canonical_marker"] for t in body["trends"]} == {"creatinine", "potassium"}
    creatinine = next(t for t in body["trends"] if t["canonical_marker"] == "creatinine")
    assert creatinine["analysis"]["direction"] == "rising"
    assert creatinine["unit"] == "mg/dl"
    assert body["truncated"] is False


async def test_the_route_harmonises_units_across_reports(auth_client, db):
    """The end-to-end form of the headline test, through the database's Decimal columns."""
    pid = await _chart_with_labs(
        auth_client,
        db,
        [
            _lab("Creatinine", 1.1, "mg/dL", 1),
            _lab("S. Creatinine", 97, "umol/L", 10),
        ],
    )
    resp = await auth_client.get(f"/api/v1/patients/{pid}/labs/trends/Creatinine")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [round(p["value"], 2) for p in body["points"]] == [1.1, 1.1]
    assert body["analysis"]["direction"] == "stable"


async def test_the_single_marker_route_trends_every_spelling_on_the_chart(auth_client, db):
    """Asking for "S. Creatinine" must not answer with a one-point series when the chart holds
    four years of creatinines spelled three ways."""
    pid = await _chart_with_labs(
        auth_client,
        db,
        [
            _lab("Creatinine", 0.8, "mg/dL", 1),
            _lab("Serum Creatinine", 1.2, "mg/dL", 10),
            _lab("S. Creatinine", 1.7, "mg/dL", 20),
        ],
    )
    resp = await auth_client.get(f"/api/v1/patients/{pid}/labs/trends/S. Creatinine")
    assert resp.status_code == 200, resp.text
    assert resp.json()["analysis"]["point_count"] == 3


async def test_an_unknown_marker_is_a_404_with_its_own_code(auth_client, db):
    pid = await _chart_with_labs(auth_client, db, [_lab("Potassium", 4.1, "mmol/L", 1)])
    resp = await auth_client.get(f"/api/v1/patients/{pid}/labs/trends/Ferritin")
    assert resp.status_code == 404
    assert resp.json()["code"] == "lab_marker_not_found"


async def test_a_withdrawn_result_never_appears_on_the_trend_line(auth_client, db):
    """A value withdrawn from the chart still being plotted is the withdrawal quietly failing."""
    pid = await _chart_with_labs(
        auth_client,
        db,
        [
            _lab("Potassium", 4.1, "mmol/L", 1),
            _lab("Potassium", 6.9, "mmol/L", 10, is_deleted=True),
        ],
    )
    resp = await auth_client.get(f"/api/v1/patients/{pid}/labs/trends/Potassium")
    assert resp.status_code == 200
    assert [p["value"] for p in resp.json()["points"]] == [4.1]


async def test_another_accounts_chart_is_a_404_on_both_trend_routes(
    auth_client, second_auth_client, db
):
    pid = await _chart_with_labs(auth_client, db, [_lab("Potassium", 4.1, "mmol/L", 1)])
    for path in (
        f"/api/v1/patients/{pid}/labs/trends",
        f"/api/v1/patients/{pid}/labs/trends/Potassium",
    ):
        resp = await second_auth_client.get(path)
        assert resp.status_code == 404, path


@pytest.mark.parametrize(
    ("suffix", "action"),
    [("", "lab_trends_viewed"), ("/Potassium", "lab_trend_viewed")],
)
async def test_reading_a_trend_is_audited(auth_client, db, suffix, action):
    """Values, units and reference intervals across years — a PHI read like any other."""
    pid = await _chart_with_labs(auth_client, db, [_lab("Potassium", 4.1, "mmol/L", 1)])
    resp = await auth_client.get(f"/api/v1/patients/{pid}/labs/trends{suffix}")
    assert resp.status_code == 200
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
    assert len(rows) == 1
    assert str(rows[0].patient_id) == pid


async def test_the_audit_payload_carries_no_marker_name_for_an_uncurated_analyte(auth_client, db):
    """``audit_logs.payload`` is unencrypted and never pruned; a free-text marker name is a
    statement about what this patient is being investigated for."""
    pid = await _chart_with_labs(auth_client, db, [_lab("Anti-Widget Antibody", 12.0, "u", 1)])
    resp = await auth_client.get(f"/api/v1/patients/{pid}/labs/trends/Anti-Widget Antibody")
    assert resp.status_code == 200
    row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "lab_trend_viewed")))
        .scalars()
        .one()
    )
    assert row.payload["canonical_marker"] is None
    assert "Widget" not in str(row.payload)
