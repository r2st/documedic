"""Regression: a non-conforming model must not crash the UI with object-typed fields.

A weak provider (e.g. a free OpenRouter model) can return a hypothesis ``rationale`` or an
evidence item as a nested object like ``{"evidence": ..., "probability": ...}`` instead of the
requested string. That object used to flow into ``ClinicalSuggestion.body`` and white-screen the
React UI (error #31). These tests pin the defensive coercion at both the parse boundary and the
synthesis chokepoint.
"""

from __future__ import annotations

from app.agents.hypothesis_panel import _parse_evidence
from app.agents.state import CaseState, Hypothesis
from app.agents.synthesis import build_suggestions
from app.agents.util import as_text


def test_as_text_flattens_object_rationale():
    assert as_text({"evidence": "ST elevation", "probability": "high"}) == "ST elevation"
    assert as_text({"probability": 0.8}) == "0.8"
    assert as_text(["a", "b", {"text": "c"}]) == "a; b; c"
    assert as_text("  spaced  ") == "spaced"
    assert as_text(None) == ""


def test_parse_evidence_tolerates_objectified_items():
    # Model used {evidence, probability} instead of {text, source_ref}.
    parsed = _parse_evidence([{"evidence": "Crushing chest pain", "probability": "high"}], True)
    assert len(parsed) == 1
    assert parsed[0].text == "Crushing chest pain"
    assert parsed[0].supports is True
    # Empty/garbage items are dropped, not rendered.
    assert _parse_evidence([{"probability": "high"}, {}], True) == []


def test_build_suggestions_body_is_always_a_string():
    state = CaseState(presenting_complaint="chest pain", patient_id="p1")
    # Simulate a malformed hypothesis whose rationale is an object, not a string.
    h = Hypothesis(diagnosis_name="Acute coronary syndrome", probability_band="high")
    h.rationale = {"evidence": "ST elevation in II, III, aVF", "probability": "high"}  # type: ignore[assignment]
    state.hypothesis_set.append(h)

    suggestions = build_suggestions(state)
    body = suggestions[0]["body"]
    assert isinstance(body, str)
    assert body == "ST elevation in II, III, aVF"
