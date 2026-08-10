"""Agent snapshot tools — deterministic, offline, and pure (no DB, no LLM).

These feed every agent prompt, so a silent failure here corrupts clinical reasoning input
rather than raising. Malformed snapshot rows must degrade, never crash.
"""

from __future__ import annotations

from app.agents.state import Hypothesis
from app.agents.tools import (
    active_condition_names,
    compute_discriminating_test,
    get_lab_trend,
    latest_marker,
    summarize_snapshot,
)

SNAPSHOT = {
    "conditions": [
        {"condition_name": "Type 2 diabetes mellitus", "status": "active"},
        {"condition_name": "Hypertension", "status": "active"},
        {"condition_name": "Chickenpox", "status": "resolved"},
    ],
    "medications": [
        {"generic_name": "Metformin", "dose": "500mg", "is_current": True},
        {"brand_name_raw": "Crocin", "dose": "650mg", "is_current": True},
        {"generic_name": "Amoxicillin", "dose": "500mg", "is_current": False},
    ],
    "allergies": [{"allergen_name": "Penicillin"}],
    "lab_results": [
        {
            "marker_name": "HbA1c",
            "value_numeric": 9.1,
            "unit": "%",
            "is_abnormal": True,
            "sample_date": "2026-03-01",
        },
        {
            "marker_name": "HbA1c",
            "value_numeric": 7.8,
            "unit": "%",
            "is_abnormal": True,
            "sample_date": "2026-01-01",
        },
        {"marker_name": "Sodium", "value_numeric": 140, "unit": "mmol/L", "is_abnormal": False},
    ],
    "derived_markers": [{"marker_name": "eGFR", "value_numeric": 52, "unit": "mL/min/1.73m2"}],
}


# --- summarize_snapshot ----------------------------------------------------------------


def test_summary_includes_each_populated_section():
    summary = summarize_snapshot(SNAPSHOT)
    assert "Type 2 diabetes mellitus" in summary
    assert "Metformin 500mg" in summary
    assert "Penicillin" in summary
    assert "HbA1c=9.1%" in summary
    assert "eGFR=52mL/min/1.73m2" in summary


def test_summary_lists_only_current_medications():
    summary = summarize_snapshot(SNAPSHOT)
    assert "Amoxicillin" not in summary


def test_summary_falls_back_to_brand_name_when_generic_is_missing():
    assert "Crocin" in summarize_snapshot(SNAPSHOT)


def test_summary_lists_only_abnormal_labs():
    assert "Sodium" not in summarize_snapshot(SNAPSHOT)


def test_empty_snapshot_produces_an_explicit_no_history_line():
    assert summarize_snapshot({}) == "No structured history on file."


def test_summary_survives_rows_with_missing_keys():
    messy = {
        "conditions": [{}, {"condition_name": None}],
        "medications": [{"is_current": True}],
        "allergies": [{"allergen_name": None}],
        "lab_results": [{"is_abnormal": True}],
    }
    # Must not raise; the medication/lab rows still render, empty names are filtered out.
    assert isinstance(summarize_snapshot(messy), str)


def test_summary_caps_long_lab_and_marker_lists():
    """Prompt budget: the summary truncates rather than pasting an entire lab history."""
    snapshot = {
        "lab_results": [
            {"marker_name": f"M{i}", "value_numeric": i, "is_abnormal": True} for i in range(30)
        ],
        "derived_markers": [{"marker_name": f"D{i}", "value_numeric": i} for i in range(20)],
    }
    summary = summarize_snapshot(snapshot)
    assert "M11=11" in summary and "M12=12" not in summary
    assert "D5=5" in summary and "D6=6" not in summary


# --- get_lab_trend ---------------------------------------------------------------------


def test_lab_trend_is_chronological_most_recent_last():
    trend = get_lab_trend(SNAPSHOT, "HbA1c")
    assert [row["value_numeric"] for row in trend] == [7.8, 9.1]


def test_lab_trend_matching_is_case_insensitive():
    assert len(get_lab_trend(SNAPSHOT, "hba1c")) == 2


def test_lab_trend_for_an_absent_marker_is_empty():
    assert get_lab_trend(SNAPSHOT, "Troponin") == []


def test_lab_trend_tolerates_rows_without_a_sample_date():
    snapshot = {
        "lab_results": [
            {"marker_name": "K", "value_numeric": 5.0},
            {"marker_name": "K", "value_numeric": 4.0, "sample_date": "2026-02-01"},
        ]
    }
    assert len(get_lab_trend(snapshot, "K")) == 2


# --- active_condition_names ------------------------------------------------------------


def test_active_conditions_exclude_resolved_ones():
    assert active_condition_names(SNAPSHOT) == ["Type 2 diabetes mellitus", "Hypertension"]


def test_active_conditions_on_empty_snapshot():
    assert active_condition_names({}) == []


# --- latest_marker ---------------------------------------------------------------------


def test_latest_marker_reads_derived_markers_first():
    assert latest_marker(SNAPSHOT, "eGFR") == 52.0


def test_latest_marker_falls_through_to_lab_results():
    assert latest_marker(SNAPSHOT, "HbA1c") == 9.1


def test_latest_marker_is_case_insensitive():
    assert latest_marker(SNAPSHOT, "egfr") == 52.0


def test_latest_marker_returns_none_when_absent():
    assert latest_marker(SNAPSHOT, "Troponin") is None


def test_latest_marker_returns_none_for_a_non_numeric_value():
    """LLM-sourced snapshots can carry 'not detected' where a number belongs."""
    snapshot = {"derived_markers": [{"marker_name": "eGFR", "value_numeric": "not detected"}]}
    assert latest_marker(snapshot, "eGFR") is None


def test_latest_marker_returns_none_for_a_null_value():
    snapshot = {"derived_markers": [{"marker_name": "eGFR", "value_numeric": None}]}
    assert latest_marker(snapshot, "eGFR") is None


# --- compute_discriminating_test -------------------------------------------------------


def test_maps_a_hypothesis_family_to_its_highest_yield_test():
    investigations = compute_discriminating_test(
        [Hypothesis(diagnosis_name="Acute coronary syndrome", rationale="")]
    )
    assert any("ECG" in inv.name for inv in investigations)


def test_each_investigation_is_suggested_only_once():
    hypotheses = [
        Hypothesis(diagnosis_name="Coronary artery disease", rationale=""),
        Hypothesis(diagnosis_name="Acute coronary syndrome", rationale=""),
    ]
    names = [inv.name for inv in compute_discriminating_test(hypotheses)]
    assert len(names) == len(set(names)) == 1


def test_multiple_families_each_contribute_a_test():
    hypotheses = [
        Hypothesis(diagnosis_name="Dengue fever", rationale=""),
        Hypothesis(diagnosis_name="Falciparum malaria", rationale=""),
    ]
    names = [inv.name for inv in compute_discriminating_test(hypotheses)]
    assert len(names) == 2


def test_unmapped_hypotheses_still_yield_a_safe_default():
    """Never return nothing — an empty investigation list would read as 'no workup needed'."""
    investigations = compute_discriminating_test(
        [Hypothesis(diagnosis_name="Something unmapped", rationale="")]
    )
    assert len(investigations) == 1
    assert "history" in investigations[0].name.lower()


def test_no_hypotheses_still_yields_the_default():
    assert len(compute_discriminating_test([])) == 1


def test_every_investigation_carries_cost_and_facility_tier():
    """Indian facility-tier and cost framing is required for the suggestion to be actionable."""
    investigations = compute_discriminating_test(
        [Hypothesis(diagnosis_name="Sepsis", rationale="")]
    )
    for inv in investigations:
        assert inv.cost_estimate
        assert inv.availability_tier in {"phc", "chc", "district_hospital"}
