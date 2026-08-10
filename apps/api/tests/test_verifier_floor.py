"""Unit tests for the Verifier's deterministic conservative-wins floor (Critical Safety Rule
#2: on disagreement/uncertainty the MORE conservative autonomy tier always wins) and the
tier/band ordering helpers it depends on. This is the most safety-critical piece of logic in
the reasoning engine and previously had no direct unit coverage -- only indirect exercise via
full-pipeline integration tests in test_reasoning.py.
"""

from __future__ import annotations

import pytest

from app.agents.state import (
    CaseState,
    HardBlock,
    Hypothesis,
    ManagementOption,
    downgrade_band,
    more_conservative_tier,
)
from app.agents.verifier import _deterministic_floor


def _state(**overrides) -> CaseState:
    defaults = {"patient_id": "p1", "presenting_complaint": "fever"}
    defaults.update(overrides)
    return CaseState(**defaults)


# --------------------------------------------------------------------- more_conservative_tier


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("informational", "suggestive", "suggestive"),
        ("suggestive", "informational", "suggestive"),
        ("flag_for_review", "informational", "flag_for_review"),
        ("informational", "flag_for_review", "flag_for_review"),
        ("suggestive", "suggestive", "suggestive"),
        ("flag_for_review", "flag_for_review", "flag_for_review"),
    ],
)
def test_more_conservative_tier_never_downgrades(a, b, expected):
    assert more_conservative_tier(a, b) == expected


def test_downgrade_band_moves_one_step_toward_uncertainty():
    assert downgrade_band("high") == "moderate"
    assert downgrade_band("moderate") == "low"
    assert downgrade_band("low") == "very_low"


def test_downgrade_band_floors_at_insufficient_data():
    assert downgrade_band("very_low") == "insufficient_data"
    assert downgrade_band("insufficient_data") == "insufficient_data"


def test_downgrade_band_unknown_value_floors_conservatively():
    assert downgrade_band("not-a-real-band") == "insufficient_data"


# --------------------------------------------------------------------- _deterministic_floor


def test_empty_case_is_informational():
    tier, reasons = _deterministic_floor(_state())
    assert tier == "informational"
    assert reasons == []


def test_any_hypothesis_forces_at_least_suggestive():
    state = _state(hypothesis_set=[Hypothesis(diagnosis_name="Flu")])
    tier, _ = _deterministic_floor(state)
    assert tier == "suggestive"


def test_cant_miss_hypothesis_forces_flag_for_review():
    state = _state(
        hypothesis_set=[Hypothesis(diagnosis_name="MI", cant_miss_flag=True)],
    )
    tier, reasons = _deterministic_floor(state)
    assert tier == "flag_for_review"
    assert any("can't-miss" in r.lower() for r in reasons)


def test_hard_block_forces_flag_for_review_even_with_no_hypotheses():
    state = _state(hard_blocks=[HardBlock(summary="x", check_type="allergy_conflict")])
    tier, reasons = _deterministic_floor(state)
    assert tier == "flag_for_review"
    assert any("hard block" in r.lower() for r in reasons)


def test_drug_safety_warning_forces_flag_for_review():
    state = _state(drug_safety_flags=[{"severity": "warning"}])
    tier, reasons = _deterministic_floor(state)
    assert tier == "flag_for_review"


def test_drug_safety_info_severity_does_not_escalate():
    state = _state(
        hypothesis_set=[Hypothesis(diagnosis_name="Flu")],
        drug_safety_flags=[{"severity": "info"}],
    )
    tier, _ = _deterministic_floor(state)
    assert tier == "suggestive"


def test_management_option_without_citations_escalates():
    state = _state(management_options=[ManagementOption(text="Consider X", citations=[])])
    tier, reasons = _deterministic_floor(state)
    assert tier == "flag_for_review"
    assert any("guideline support" in r.lower() for r in reasons)


def test_degraded_mode_always_escalates():
    state = _state(hypothesis_set=[Hypothesis(diagnosis_name="Flu")], degraded=True)
    tier, reasons = _deterministic_floor(state)
    assert tier == "flag_for_review"
    assert any("degraded" in r.lower() for r in reasons)


def test_multiple_conservative_reasons_all_recorded_not_just_the_first():
    """Rule #2 -- disagreement/uncertainty should surface every reason, not silently pick one."""
    state = _state(
        hypothesis_set=[Hypothesis(diagnosis_name="MI", cant_miss_flag=True)],
        hard_blocks=[HardBlock(summary="x", check_type="allergy_conflict")],
        degraded=True,
    )
    tier, reasons = _deterministic_floor(state)
    assert tier == "flag_for_review"
    assert len(reasons) >= 3
