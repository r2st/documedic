"""One agent crashing used to throw away the whole case.

No node in ``graph.run_reasoning`` was wrapped, so an exception anywhere in the pipeline
escaped to ``ReasoningService.run``, which marked the session ``failed``. The recurring shape
of that defect is in this repository's history: the response arrives, something in it is the
wrong *shape*, and the exception happens while parsing it — past the point where "the model
gave us nothing usable" would have routed to the deterministic fallback each of these agents
already has. ``objects``, ``_parse_evidence``, ``as_float`` and ``_recognised`` were all added
after a clinician lost a run to one field.

Those were instances. This is the class: an advisory node that raises costs its own lane and
nothing else. Failing the whole run instead is not the conservative choice it looks like — a
crash in the Devil's-Advocate after the panel has produced a full differential throws that
differential away, and an empty screen is not safer than a marked-up one.

What makes it safe rather than merely lenient is that the degradation is loud, and that is
most of what is asserted here:

* the case is marked degraded and the lane is *named*, so "the Devil's-Advocate did not run on
  this case" reaches the clinician rather than a generic caveat;
* the Verifier escalates to flag-for-review, so nothing rides on the clinician noticing;
* the Theatre paints the lane as failed rather than sweeping it to a green tick;
* and the safety-critical nodes are **not** covered by any of this. A case that reached a
  clinician without the deterministic allergy check, without the gate (Rule #1), or without an
  output is not a degraded case, it is a wrong one.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents import graph
from app.agents.context import ReasoningContext
from app.agents.llm import LLMClient
from app.agents.state import CaseState, Hypothesis


class _OfflineLLM(LLMClient):
    """Unavailable, so every node runs its deterministic path and nothing calls a provider."""

    def available(self) -> bool:
        return False

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        raise AssertionError("no provider call is expected in these tests")


def _state() -> CaseState:
    return CaseState(
        patient_id="p1",
        presenting_complaint="Fever and cough for four days, worse at night",
        intake_complete=True,
    )


def _ctx(events: list[tuple[str, dict]] | None = None) -> ReasoningContext:
    async def emit(event: str, data: dict[str, Any]) -> None:
        if events is not None:
            events.append((event, data))

    return ReasoningContext(llm=_OfflineLLM(), verifier_llm=_OfflineLLM(), emit=emit)


def _exploding(agent_module, monkeypatch, exc: Exception | None = None) -> None:
    """Make one agent's ``run`` raise, the way a parse of a malformed response does."""

    async def boom(state: CaseState, ctx: ReasoningContext) -> None:
        raise exc or TypeError("'int' object is not iterable")

    monkeypatch.setattr(agent_module, "run", boom)


# --- The run survives ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_crashing_advisory_agent_does_not_fail_the_case(monkeypatch):
    """The regression. Before this, the exception left ``run_reasoning`` and the session was
    recorded as failed with nothing to show for it."""
    from app.agents import devils_advocate

    _exploding(devils_advocate, monkeypatch)
    state = _state()

    output = await graph.run_reasoning(state, _ctx())

    assert output["suggestions"], "the case produced no output at all"


@pytest.mark.asyncio
async def test_the_work_the_other_agents_did_survives(monkeypatch):
    """A differential the panel had already produced is not thrown away because a later node
    crashed while critiquing it."""
    from app.agents import investigation_strategist

    _exploding(investigation_strategist, monkeypatch)
    state = _state()

    await graph.run_reasoning(state, _ctx())

    assert state.hypothesis_set, "the panel's hypotheses were discarded with the failed node"


@pytest.mark.parametrize(
    "module_name",
    [
        "hypothesis_panel",
        "cant_miss_sentinel",
        "devils_advocate",
        "investigation_strategist",
        "guideline_rag",
    ],
)
@pytest.mark.asyncio
async def test_every_advisory_node_is_isolated(monkeypatch, module_name):
    """Stated for each node rather than for the one that happened to break, so a node added
    later to the advisory run is covered by the same guarantee."""
    import importlib

    module = importlib.import_module(f"app.agents.{module_name}")
    _exploding(module, monkeypatch)
    state = _state()

    await graph.run_reasoning(state, _ctx())

    assert state.failed_agents == [module_name]


# --- The degradation is loud --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_case_is_degraded_and_the_lane_is_named(monkeypatch):
    """Named, not counted: which cross-check is missing is the thing a clinician can act on."""
    from app.agents import devils_advocate

    _exploding(devils_advocate, monkeypatch)
    state = _state()

    await graph.run_reasoning(state, _ctx())

    assert state.degraded is True
    assert state.failed_agents == ["devils_advocate"]


@pytest.mark.asyncio
async def test_the_case_is_escalated_for_a_human(monkeypatch):
    """Nothing rides on the clinician noticing a caveat: a case the panel could not complete is
    flag-for-review whatever else it found (Critical Safety Rule #2)."""
    from app.agents import cant_miss_sentinel

    _exploding(cant_miss_sentinel, monkeypatch)
    state = _state()

    await graph.run_reasoning(state, _ctx())

    assert state.autonomy_tier == "flag_for_review"


@pytest.mark.asyncio
async def test_the_verifier_says_which_agent_did_not_run(monkeypatch):
    """The caveat reaches the clinician through the verifier verdict carried on every
    suggestion, so it is read beside the output rather than in a server log."""
    from app.agents import guideline_rag

    _exploding(guideline_rag, monkeypatch)
    state = _state()

    await graph.run_reasoning(state, _ctx())

    caveats = " ".join(c for v in state.verifier_verdicts for c in v.caveats)
    assert "guideline rag" in caveats
    assert "did not complete" in caveats


@pytest.mark.asyncio
async def test_the_failure_is_in_the_immutable_case_snapshot(monkeypatch):
    """``case_state`` is what the session keeps and what a reviewer reads months later. A
    failure that lived only in the process's logs would be gone by then."""
    from app.agents import devils_advocate

    _exploding(devils_advocate, monkeypatch)
    state = _state()

    output = await graph.run_reasoning(state, _ctx())

    assert output["case_state"]["failed_agents"] == ["devils_advocate"]
    assert any(m["role"] == "error" for m in output["case_state"]["agent_messages"])


@pytest.mark.asyncio
async def test_the_theatre_is_told_the_lane_failed(monkeypatch):
    """A lane that just stops emitting reads as "still working" on a screen where everything
    else has finished."""
    from app.agents import devils_advocate

    _exploding(devils_advocate, monkeypatch)
    events: list[tuple[str, dict]] = []

    await graph.run_reasoning(_state(), _ctx(events))

    failed = [data for name, data in events if name == "agent_failed"]
    assert [d["agent"] for d in failed] == ["devils_advocate"]
    # And no ``agent_complete`` for it, which is what would paint the tick.
    completed = [d["agent"] for name, d in events if name == "agent_complete"]
    assert "devils_advocate" not in completed


@pytest.mark.asyncio
async def test_a_dead_stream_does_not_undo_the_recovery(monkeypatch):
    """The emitter fails when the clinician has closed the tab. Letting that escape would turn
    a contained agent failure back into a failed run — the exact thing being prevented."""
    from app.agents import devils_advocate

    _exploding(devils_advocate, monkeypatch)

    async def dead_emit(event: str, data: dict[str, Any]) -> None:
        if event == "agent_failed":
            raise RuntimeError("stream closed")

    ctx = ReasoningContext(llm=_OfflineLLM(), verifier_llm=_OfflineLLM(), emit=dead_emit)
    state = _state()

    output = await graph.run_reasoning(state, ctx)

    assert output["suggestions"]
    assert state.failed_agents == ["devils_advocate"]


@pytest.mark.asyncio
async def test_two_failed_agents_are_both_named(monkeypatch):
    from app.agents import devils_advocate, investigation_strategist

    _exploding(devils_advocate, monkeypatch)
    _exploding(investigation_strategist, monkeypatch)
    state = _state()

    await graph.run_reasoning(state, _ctx())

    assert state.failed_agents == ["devils_advocate", "investigation_strategist"]


# --- What is deliberately NOT isolated ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_safety_check_fails_the_run():
    """There is no degraded version of "we did not check this patient's allergies". A case that
    reaches a clinician without the deterministic pass has not had Rules #3 and #8 applied to
    it, so the run fails and they are told so."""

    async def boom(_text: str) -> list[dict[str, Any]]:
        raise RuntimeError("safety engine unreachable")

    ctx = ReasoningContext(llm=_OfflineLLM(), verifier_llm=_OfflineLLM(), evaluate_safety=boom)

    with pytest.raises(RuntimeError):
        await graph.run_reasoning(_state(), ctx)


@pytest.mark.asyncio
async def test_a_failed_verifier_fails_the_run(monkeypatch):
    """The gate cannot be bypassed (Critical Safety Rule #1), and "it crashed" is not an
    exception to that. The Verifier's own handling of a provider that will not answer is
    inside the node; a crash *of* the node is a different thing and must not publish."""
    from app.agents import verifier

    _exploding(verifier, monkeypatch)

    with pytest.raises(TypeError):
        await graph.run_reasoning(_state(), _ctx())


@pytest.mark.asyncio
async def test_a_failed_synthesis_fails_the_run(monkeypatch):
    """Synthesis is the output. There is nothing to degrade to."""
    from app.agents import synthesis

    _exploding(synthesis, monkeypatch)

    with pytest.raises(TypeError):
        await graph.run_reasoning(_state(), _ctx())


# --- The panel itself ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_specialist_failing_does_not_discard_the_other_three(monkeypatch):
    """``asyncio.gather`` propagates the first exception and drops its siblings' completed
    work, so a single specialist returning something unparseable used to empty the panel — and
    with nothing produced, the deterministic fallback replaced four specialists' answers with a
    keyword match."""
    from app.agents import hypothesis_panel

    calls: list[str] = []

    async def flaky(state, ctx, key, name, summary):
        calls.append(key)
        if key == "cardiology":
            raise TypeError("'int' object is not iterable")
        return [
            Hypothesis(
                diagnosis_name=f"Dx from {key}", probability_band="moderate", source_agent=key
            )
        ]

    monkeypatch.setattr(hypothesis_panel, "_run_specialist", flaky)

    class _LiveLLM(LLMClient):
        def available(self) -> bool:
            return True

    ctx = ReasoningContext(llm=_LiveLLM(), verifier_llm=_OfflineLLM())
    state = _state()

    await hypothesis_panel.run(state, ctx)

    assert len(calls) == 4, "the panel stopped short of asking every specialist"
    names = {h.diagnosis_name for h in state.hypothesis_set}
    assert names == {
        "Dx from internal_medicine",
        "Dx from infectious_disease",
        "Dx from primary_care",
    }


@pytest.mark.asyncio
async def test_a_partially_failed_panel_still_marks_the_case_degraded(monkeypatch):
    """Three specialists out of four is not the panel the output claims to be."""
    from app.agents import hypothesis_panel

    async def flaky(state, ctx, key, name, summary):
        if key == "cardiology":
            raise TypeError("'int' object is not iterable")
        return [Hypothesis(diagnosis_name=f"Dx from {key}", source_agent=key)]

    monkeypatch.setattr(hypothesis_panel, "_run_specialist", flaky)

    class _LiveLLM(LLMClient):
        def available(self) -> bool:
            return True

    state = _state()
    await hypothesis_panel.run(state, ReasoningContext(llm=_LiveLLM(), verifier_llm=_OfflineLLM()))

    assert state.degraded is True
    # But the panel is not reported *missing*: three quarters of it answered.
    assert state.failed_agents == []
