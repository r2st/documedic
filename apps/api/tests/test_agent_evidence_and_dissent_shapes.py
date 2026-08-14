"""Evidence lists and the Devil's-Advocate critique, when the model answers in the wrong shape.

``test_agent_malformed_llm_output`` covers the fields each agent reads as *a list of objects*.
These are the fields read as *a list of sentences* — evidence for and against a hypothesis, and
the four fields of the Devil's-Advocate critique — which were still trusted to be lists.

Two failure modes, both from ordinary model behaviour:

  * ``for e in items or []`` on a scalar raises TypeError. The four specialists run under
    ``asyncio.gather``, so one of them hitting it discards the other three's work and fails the
    whole case — past the point where the deterministic panel would have covered for the model.
  * ``for e in items or []`` on a *string* does not raise. It iterates into characters, and each
    character becomes a piece of evidence on the clinician's card.

The Devil's-Advocate had neither guard and is the worst place to lack them: Critical Safety Rule
#5 says the counter-argument is always shown and cannot be suppressed, and its two list fields
are rendered by mapping over them, so a string reached the browser as ``.map is not a function``
and white-screened the whole suggestion — leaving the leading hypothesis on screen with nothing
arguing against it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents import devils_advocate
from app.agents import hypothesis_panel as panel
from app.agents.context import ReasoningContext
from app.agents.llm import LLMClient
from app.agents.state import CaseState, Hypothesis
from app.agents.util import text_list


class _CannedLLM(LLMClient):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        return self.payload


def _ctx(payload: dict[str, Any]) -> tuple[ReasoningContext, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    client = _CannedLLM(payload)
    return ReasoningContext(llm=client, verifier_llm=client, emit=emit), events


def _state() -> CaseState:
    return CaseState(patient_id="p1", presenting_complaint="chest pain for two hours")


def _completed(events: list[tuple[str, dict]], agent: str) -> bool:
    return any(e == "agent_complete" and d.get("agent") == agent for e, d in events)


# --- text_list -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["a", "b"], ["a", "b"]),
        ("one sentence", ["one sentence"]),  # a string is one item, not its characters
        ("  padded  ", ["padded"]),
        ("", []),
        (None, []),
        (0, []),
        (42, []),
        (True, []),
        ({"text": "from an object"}, []),  # a bare object is not a list of sentences
        (["a", None, "", "b"], ["a", "b"]),  # empties dropped, neighbours kept
        ([{"text": "flattened"}, "plain"], ["flattened", "plain"]),
        (("a", "b"), ["a", "b"]),  # tuples too
    ],
)
def test_text_list_coerces_every_shape_a_model_produces(value: object, expected: list[str]):
    assert text_list(value) == expected


# --- evidence lists on a hypothesis ---------------------------------------------------------


@pytest.mark.parametrize("value", [0, 42, 1.5, True, None, {"unreadable": {}}])
def test_a_non_iterable_evidence_field_yields_no_evidence_rather_than_raising(value: object):
    assert panel._parse_evidence(value, True) == []


def test_a_string_evidence_field_is_one_piece_of_evidence_not_nineteen_letters():
    evidence = panel._parse_evidence("no relevant findings", True)

    assert [e.text for e in evidence] == ["no relevant findings"]


def test_an_object_evidence_field_is_one_piece_of_evidence():
    evidence = panel._parse_evidence({"text": "Troponin was normal.", "source_ref": "lab-9"}, False)

    assert [(e.text, e.source_ref, e.supports) for e in evidence] == [
        ("Troponin was normal.", "lab-9", False)
    ]


def test_well_formed_evidence_beside_malformed_entries_survives():
    """The point of dropping rather than raising: a partly-bad list is still worth reading.

    A bare scalar inside the list keeps ``as_text``'s stringification ("7") rather than being
    dropped — it is unhelpful on the card but it is not a crash, and ``as_text`` is shared with
    every other agent, several of which read fields where a number is a legitimate answer.
    """
    evidence = panel._parse_evidence(
        [{"text": "Exertional onset."}, None, 7, "Radiates to the jaw.", {"nothing": {}}],
        True,
    )

    assert [e.text for e in evidence] == ["Exertional onset.", "7", "Radiates to the jaw."]


def test_a_scalar_evidence_field_does_not_take_down_the_specialist_panel():
    """The crash path: one specialist raising inside ``asyncio.gather`` failed the whole case."""
    state = _state()
    ctx, events = _ctx(
        {
            "hypotheses": [
                {
                    "diagnosis_name": "Stable angina",
                    "probability_band": "moderate",
                    "evidence_for": 0,  # the shape that raised TypeError
                    "evidence_against": "None documented.",
                }
            ]
        }
    )

    asyncio.run(panel.run(state, ctx))

    assert _completed(events, panel.AGENT)
    names = [h.diagnosis_name for h in state.hypothesis_set]
    assert "Stable angina" in names
    # The hypothesis is kept, with the unusable field emptied and the readable one intact.
    angina = next(h for h in state.hypothesis_set if h.diagnosis_name == "Stable angina")
    assert angina.evidence_for == []
    # One entry per specialist: the canned response stands in for all four, and ``_merge``
    # unions the evidence of every specialist that named the same diagnosis.
    assert {e.text for e in angina.evidence_against} == {"None documented."}
    # A model that answered is not a degraded run: the deterministic panel must not have fired.
    assert not state.degraded


# --- the Devil's-Advocate critique ----------------------------------------------------------


def _run_devils_advocate(payload: dict[str, Any]) -> tuple[CaseState, list[tuple[str, dict]]]:
    state = _state()
    state.hypothesis_set.append(Hypothesis(diagnosis_name="Stable angina", probability_band="high"))
    ctx, events = _ctx(payload)
    asyncio.run(devils_advocate.run(state, ctx))
    return state, events


def test_a_string_where_the_dissent_list_belongs_becomes_a_one_item_list():
    """The shape that white-screened the card: rendered with ``.map``, delivered as a string."""
    state, events = _run_devils_advocate(
        {
            "disconfirming_evidence": "The ECG was normal throughout.",
            "alternative_explanations": "Gastro-oesophageal reflux",
            "base_rate_caveat": "Angina is uncommon at this age.",
            "summary": "The findings are not specific.",
        }
    )

    critique = state.hypothesis_set[0].devil_advocate
    assert critique is not None
    assert critique["disconfirming_evidence"] == ["The ECG was normal throughout."]
    assert critique["alternative_explanations"] == ["Gastro-oesophageal reflux"]
    assert critique["base_rate_caveat"] == "Angina is uncommon at this age."
    assert critique["summary"] == "The findings are not specific."
    assert _completed(events, devils_advocate.AGENT)


def test_objects_inside_the_dissent_list_are_flattened_to_readable_text():
    state, _ = _run_devils_advocate(
        {
            "disconfirming_evidence": [
                {"text": "No troponin rise."},
                {"evidence": "Pain reproduced on palpation."},
                None,
                "",
            ],
            "alternative_explanations": [{"summary": "Costochondritis"}],
            "base_rate_caveat": {"reason": "Low pre-test probability."},
            "summary": ["Two", "sentences"],
        }
    )

    critique = state.hypothesis_set[0].devil_advocate
    assert critique is not None
    assert critique["disconfirming_evidence"] == [
        "No troponin rise.",
        "Pain reproduced on palpation.",
    ]
    assert critique["alternative_explanations"] == ["Costochondritis"]
    assert critique["base_rate_caveat"] == "Low pre-test probability."
    assert critique["summary"] == "Two; sentences"


@pytest.mark.parametrize("value", [0, 42, True, {"a": 1}, None])
def test_an_unreadable_dissent_field_empties_rather_than_raising(value: object):
    state, events = _run_devils_advocate(
        {"disconfirming_evidence": value, "alternative_explanations": value}
    )

    critique = state.hypothesis_set[0].devil_advocate
    assert critique is not None
    assert critique["disconfirming_evidence"] == []
    assert critique["alternative_explanations"] == []
    assert _completed(events, devils_advocate.AGENT)


def test_the_emitted_critique_carries_the_same_coerced_shape():
    """The Reasoning Theatre streams this event; it must not be told a string is a list either."""
    _, events = _run_devils_advocate({"disconfirming_evidence": "A single objection."})

    critique = next(d["critique"] for e, d in events if e == "devils_advocate")
    assert isinstance(critique["disconfirming_evidence"], list)
    assert isinstance(critique["alternative_explanations"], list)
    assert isinstance(critique["base_rate_caveat"], str)
    assert isinstance(critique["summary"], str)


def test_the_dissent_still_reaches_the_clinician_when_every_field_is_malformed():
    """Safety Rule #5: the counter-argument is always shown. It may be thin, never absent."""
    state, events = _run_devils_advocate(dict.fromkeys(("summary", "base_rate_caveat"), 0))

    assert state.hypothesis_set[0].devil_advocate is not None
    assert any(e == "devils_advocate" for e, _ in events)
    assert state.hypothesis_set[0].devil_advocate["leading_hypothesis"] == "Stable angina"
