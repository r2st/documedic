"""Decisions in the Hypothesis Panel that no test constrained.

Found by mutation testing ``app/agents/hypothesis_panel.py``: each mutant below was applied to
the module one at a time and the whole agent suite still passed, meaning nothing anywhere
depended on the decision being made the way it is written.

The theme is **evidence polarity**. ``Evidence.supports`` is the flag that decides whether a
finding is rendered under "evidence for" or "evidence against" a diagnosis, and every one of
the five places that sets it survived being flipped. The panel's whole output shape is built on
it -- Critical Safety Rule #6 orders the Reasoning Theatre evidence-before-conclusion, and rule
#5 keeps the counter-argument visible -- and a clinician reading a card cannot tell a mislabelled
polarity from a real one: swapped, the reasons *against* a diagnosis appear as the reasons the
system is proposing it, and every one of them is a true statement about the patient. Nothing in
the suite tested the flag's value, in either the LLM path or the deterministic fallback.

The rest are single decisions that were equally unheld: which specialist a hypothesis is
attributed to on the Theatre's per-specialist cards, the cap on the summary event, whether one
matched keyword is enough to raise a deterministic hypothesis, and whether the "undifferentiated"
catch-all is really a catch-all.

Two survivors are *equivalent* mutants -- the mutated code cannot behave differently, so no test
can kill them, and they are documented at the bottom rather than chased.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents import hypothesis_panel as panel
from app.agents.context import ReasoningContext
from app.agents.llm import LLMClient
from app.agents.state import CaseState

# --- doubles -------------------------------------------------------------------------------


class _OfflineLLM(LLMClient):
    """Unavailable, which is what routes ``run`` into the deterministic fallback panel."""

    def available(self) -> bool:
        return False

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        raise AssertionError("complete_json must not be called when available() is False")


class _CannedLLM(LLMClient):
    """Returns one fixed payload to whichever specialist asks."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        return self.payload


class _PerSpecialtyLLM(LLMClient):
    """Answers each specialist with hypotheses only that specialist could have produced.

    The four run under one ``asyncio.gather`` and are told apart only by the specialty spliced
    into their system prompt, so keying the response off that prompt is the one way to make
    "which specialist said this" observable from outside.
    """

    def __init__(self, count: int = 1) -> None:
        super().__init__()
        self.count = count

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        specialty = next(name for name in panel.SPECIALISTS.values() if name in system)
        return {
            "hypotheses": [
                {
                    "diagnosis_name": f"{specialty} finding {i}",
                    "probability_band": "moderate",
                    "evidence_for": [{"text": f"{specialty} supporting finding {i}"}],
                    "evidence_against": [{"text": f"{specialty} counter finding {i}"}],
                    "rationale": f"{specialty} reasoning {i}",
                }
                for i in range(self.count)
            ]
        }


def _ctx(llm: LLMClient) -> tuple[ReasoningContext, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    return ReasoningContext(llm=llm, verifier_llm=llm, emit=emit), events


def _state(complaint: str = "chest pain for two hours", **kwargs: Any) -> CaseState:
    return CaseState(patient_id="p1", presenting_complaint=complaint, **kwargs)


def _event(events: list[tuple[str, dict]], name: str) -> dict:
    return next(data for event, data in events if event == name)


# --- evidence polarity, LLM path -----------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_from_the_model_keeps_the_polarity_the_model_gave_it() -> None:
    """``evidence_for`` is parsed as supporting and ``evidence_against`` as opposing.

    Mutants: ``_parse_evidence(h.get("evidence_for"), True)`` -> ``False``, and
    ``_parse_evidence(h.get("evidence_against"), False)`` -> ``True``. Both survived. Either one
    alone relabels one side of every hypothesis card in the Reasoning Theatre; together they
    swap the two, so the model's reasons for doubting a diagnosis are displayed as the reasons
    it raised it.
    """
    ctx, _ = _ctx(
        _CannedLLM(
            {
                "hypotheses": [
                    {
                        "diagnosis_name": "Acute coronary syndrome",
                        "probability_band": "high",
                        "evidence_for": [{"text": "Crushing retrosternal pain on exertion"}],
                        "evidence_against": [{"text": "Troponin normal at presentation"}],
                        "rationale": "Exertional pattern with cardiac risk factors",
                    }
                ]
            }
        )
    )
    state = _state()

    await panel.run(state, ctx)

    hypothesis = next(
        h for h in state.hypothesis_set if h.diagnosis_name == "Acute coronary syndrome"
    )
    # All four specialists were handed the same canned payload, so ``_merge`` folds them into
    # one hypothesis carrying four copies of each item; the polarity is what is under test.
    assert {(e.text, e.supports) for e in hypothesis.evidence_for} == {
        ("Crushing retrosternal pain on exertion", True)
    }
    assert {(e.text, e.supports) for e in hypothesis.evidence_against} == {
        ("Troponin normal at presentation", False)
    }


@pytest.mark.asyncio
async def test_the_models_rationale_survives_onto_the_hypothesis() -> None:
    """Mutant: ``as_text(h.get("rationale")) or None`` -> ``and None``, which survived.

    ``and`` discards every rationale the model actually wrote and keeps only the empty ones, so
    the "why" line under each differential goes blank -- the part of the card that carries the
    reasoning the clinician is meant to weigh rather than accept.
    """
    ctx, _ = _ctx(
        _CannedLLM(
            {
                "hypotheses": [
                    {
                        "diagnosis_name": "Community-acquired pneumonia",
                        "probability_band": "moderate",
                        "rationale": "Focal crackles with fever and productive cough",
                    }
                ]
            }
        )
    )
    state = _state("fever and cough")

    await panel.run(state, ctx)

    hypothesis = next(
        h for h in state.hypothesis_set if h.diagnosis_name == "Community-acquired pneumonia"
    )
    assert hypothesis.rationale == "Focal crackles with fever and productive cough"


# --- specialist attribution ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_specialist_card_shows_only_that_specialists_own_hypotheses() -> None:
    """Mutant: ``if h.source_agent == key`` -> ``!=``, which survived.

    The per-specialist ``specialist`` events are what the Reasoning Theatre draws its four
    panellist cards from. Inverted, every card lists the *other three* specialists' hypotheses,
    so the differential is attributed to the specialties that did not raise it -- and "Cardiology
    put ACS on this list" is exactly the provenance a clinician weighs a suggestion by.
    """
    ctx, events = _ctx(_PerSpecialtyLLM())
    state = _state()

    await panel.run(state, ctx)

    cards = {data["agent"]: data for event, data in events if event == "specialist"}
    assert set(cards) == set(panel.SPECIALISTS)
    for key, name in panel.SPECIALISTS.items():
        listed = [h["diagnosis_name"] for h in cards[key]["hypotheses"]]
        assert listed == [f"{name} finding 0"], f"{key} card carried {listed}"


@pytest.mark.asyncio
async def test_the_hypotheses_event_is_capped_at_the_eight_leading_ones() -> None:
    """Mutant: ``state.leading_hypotheses(8)`` -> ``9``, which survived.

    Nothing pinned the cap, so the summary event's size was free to drift. Twelve distinct
    hypotheses are produced here and the event carries eight.
    """
    ctx, events = _ctx(_PerSpecialtyLLM(count=3))
    state = _state()

    await panel.run(state, ctx)

    assert len(state.hypothesis_set) == 12
    assert len(_event(events, "hypotheses")["hypotheses"]) == 8


# --- the deterministic fallback panel -------------------------------------------------------


@pytest.mark.asyncio
async def test_one_matched_keyword_is_enough_to_raise_a_fallback_hypothesis() -> None:
    """Mutant: ``sum(...) >= 1`` -> ``> 1`` (and the same threshold as ``>= 2``), both survived.

    The fallback panel is the entire differential when the provider is down, and its rows are
    keyword alternatives -- ``("palpitation", "irregular")`` is one presentation described two
    ways, not two findings that must both be present. Requiring two matches silently empties the
    differential for every single-symptom presentation, which then falls through to the
    "undifferentiated" catch-all: the degraded case looks like a case with nothing to say about
    it rather than one the pattern table matched.
    """
    ctx, _ = _ctx(_OfflineLLM())
    state = _state("palpitations since this morning")

    await panel.run(state, ctx)

    assert state.degraded is True
    assert "Cardiac arrhythmia" in {h.diagnosis_name for h in state.hypothesis_set}


@pytest.mark.asyncio
async def test_fallback_hypotheses_carry_their_evidence_with_the_right_polarity() -> None:
    """Mutants: the fallback's ``Evidence(..., True, ...)`` -> ``False`` and its
    ``Evidence(..., False, ...)`` -> ``True``. Both survived.

    Same defect as the LLM path, on the path that runs when the LLM path cannot: the pattern
    match is the evidence *for*, and the "confirmatory testing not yet available; consider
    mimics" caveat is the evidence *against*. Flipped, degraded mode presents its own caveat as
    the reason for the diagnosis and the reason for the diagnosis as a caveat.
    """
    ctx, _ = _ctx(_OfflineLLM())
    state = _state("palpitations since this morning")

    await panel.run(state, ctx)

    hypothesis = next(h for h in state.hypothesis_set if h.diagnosis_name == "Cardiac arrhythmia")
    assert [e.supports for e in hypothesis.evidence_for] == [True]
    assert [e.supports for e in hypothesis.evidence_against] == [False]
    assert "consider mimics" in hypothesis.evidence_against[0].text


@pytest.mark.asyncio
async def test_a_known_active_condition_is_carried_as_supporting_evidence() -> None:
    """Mutant: the known-condition ``Evidence(f"{cond} documented in record.", True, ...)`` ->
    ``False``, which survived.

    A condition already on the longitudinal record is surfaced as a continuing problem, and the
    record entry is what supports it. Flipped, the patient's own documented diagnosis is
    rendered as evidence *against* the problem being present.
    """
    ctx, _ = _ctx(_OfflineLLM())
    state = _state(
        "routine review",
        patient_graph_snapshot={
            "conditions": [{"condition_name": "Type 2 diabetes mellitus", "status": "active"}]
        },
    )

    await panel.run(state, ctx)

    known = next(
        h for h in state.hypothesis_set if h.diagnosis_name == "Known: Type 2 diabetes mellitus"
    )
    assert [(e.text, e.supports) for e in known.evidence_for] == [
        ("Type 2 diabetes mellitus documented in record.", True)
    ]


@pytest.mark.asyncio
async def test_the_undifferentiated_placeholder_appears_only_when_nothing_matched() -> None:
    """Mutant: ``if not out:`` -> ``if out:``, which survived.

    The placeholder is the "we have nothing" answer. Inverted, it is appended to every case that
    *did* match -- so a differential with real content also carries a line saying the
    presentation is undifferentiated and needs further assessment, which is the one statement on
    the card that argues against trusting the rest of it.
    """
    placeholder = "Undifferentiated presentation — further assessment needed"

    ctx, _ = _ctx(_OfflineLLM())
    matched = _state("palpitations since this morning")
    await panel.run(matched, ctx)
    assert {h.diagnosis_name for h in matched.hypothesis_set} != {placeholder}
    assert placeholder not in {h.diagnosis_name for h in matched.hypothesis_set}

    ctx, _ = _ctx(_OfflineLLM())
    unmatched = _state("annual paperwork review")
    await panel.run(unmatched, ctx)
    assert [h.diagnosis_name for h in unmatched.hypothesis_set] == [placeholder]


# --- merge ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merging_the_same_diagnosis_keeps_the_most_confident_band() -> None:
    """Two specialists naming one diagnosis produce one hypothesis at the higher band.

    ``_merge``'s ``order.index(h...) < order.index(kept...)`` guards the band upgrade. Flipping
    it to ``>=`` is killed by this; flipping it to ``<=`` is an *equivalent* mutant -- on a tie
    the two indices are equal, so the assignment writes the band that is already there. See the
    note at the bottom of this module.
    """

    class _TwoBands(LLMClient):
        def available(self) -> bool:
            return True

        def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
            band = "high" if panel.SPECIALISTS["cardiology"] in system else "low"
            return {
                "hypotheses": [
                    {
                        "diagnosis_name": "Acute coronary syndrome",
                        "probability_band": band,
                        "evidence_for": [{"text": f"{band} band finding"}],
                    }
                ]
            }

    ctx, _ = _ctx(_TwoBands())
    state = _state()

    await panel.run(state, ctx)

    assert [h.diagnosis_name for h in state.hypothesis_set] == ["Acute coronary syndrome"]
    merged = state.hypothesis_set[0]
    assert merged.probability_band == "high"
    # Evidence from every specialist that named it is kept, not just the winning band's.
    assert len(merged.evidence_for) == 4


# --- equivalent mutants ---------------------------------------------------------------------
#
# Two survivors from the sweep cannot be killed, because the mutated code is incapable of
# behaving differently. They are recorded here so a later sweep does not spend the effort again:
#
#   * ``_fallback``: ``sum(1 for t in triggers if t in haystack) >= 1`` -> ``sum(2 for ...)``.
#     The summand only has to be positive for the comparison to answer "at least one trigger
#     matched"; 2 and 1 give the same verdict for every possible number of matches.
#   * ``_merge``: ``order.index(h.probability_band) < order.index(kept.probability_band)`` ->
#     ``<=``. The extra case is the tie, where the assignment writes the value already stored.
