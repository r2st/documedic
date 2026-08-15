"""A provider that is configured but not answering must still mark the case degraded.

``degraded`` is the flag that says "the LLM did not contribute to this case; the deterministic
floor is all you are reading". The Verifier turns it into a clinician-facing caveat and an
escalation to flag-for-review (``verifier._deterministic_floor``), and the theatre renders it as
a badge. It is the only signal that separates a full-strength run from one the engine produced
on its own.

Four nodes decided that question by asking ``ctx.llm_available()``, which is a *configuration*
check — ``llm.is_available()`` returns True whenever any provider has an API key set. It cannot
report a provider that is down, revoked, out of quota, rate-limiting, timing out, or answering
with something that is not JSON. Those are exactly the runtime failures ``call_llm`` swallows
into a ``None`` return, and they are the ordinary way an LLM stops working mid-run.

So the reachable case was: OpenRouter goes down, every provider in the chain is exhausted,
``call_llm`` returns ``None`` at each of the seven nodes, each falls back to its deterministic
path — and the run reports ``degraded: false``. No caveat, no escalation, no badge. A case
assembled entirely from pattern-matching, presented as one eight agents deliberated over.

The same key-is-not-a-working-provider confusion was fixed on the health endpoints in this round
("a revoked API key reported the engine as live"); this is the same mistake one layer in, where
it reaches a clinician rather than an operator.

Three nodes — ``triage_intake``, ``hypothesis_panel`` and ``guideline_rag`` — already set
``degraded`` unconditionally on their fallback path, which is the correct reading and what these
tests pin for the other four: reaching the fallback branch *is* the degradation, whatever
``available()`` says about the key.
"""

from __future__ import annotations

from typing import Any

from app.agents import (
    cant_miss_sentinel,
    devils_advocate,
    guideline_rag,
    hypothesis_panel,
    investigation_strategist,
    triage_intake,
    verifier,
)
from app.agents.context import ReasoningContext
from app.agents.llm import LLMClient, LLMUnavailable
from app.agents.state import CaseState, Hypothesis


class _ConfiguredButFailingLLM(LLMClient):
    """A key is set, so ``available()`` is True — and every call fails anyway.

    This is a provider outage, a revoked key, an exhausted quota, a timeout, or a model that
    answered with prose where JSON was asked for. ``LLMClient.complete_json`` raises
    ``LLMUnavailable`` for all of them (it is what is left after every provider in the chain has
    been retried), and ``util.call_llm`` turns it into a ``None`` return.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        self.calls += 1
        raise LLMUnavailable("provider returned 503")


def _ctx() -> tuple[ReasoningContext, _ConfiguredButFailingLLM]:
    async def emit(event: str, data: dict) -> None:
        return None

    client = _ConfiguredButFailingLLM()
    return ReasoningContext(llm=client, verifier_llm=client, emit=emit), client


def _state() -> CaseState:
    return CaseState(
        patient_id="p1",
        presenting_complaint="Crushing central chest pain radiating to the jaw, 40 minutes.",
        patient_graph_snapshot={},
    )


# --- The four nodes that asked the configuration question ---------------------------------------


async def test_the_cant_miss_sentinel_marks_a_failed_scan_degraded() -> None:
    """The sentinel's LLM augmentation sits on top of the deterministic rule table.

    The rules still fire, so the case looks scanned. What is missing is everything the model
    would have added beyond the table — and the sentinel is the node whose output is meant to be
    exhaustive about danger.
    """
    state, (ctx, client) = _state(), _ctx()
    await cant_miss_sentinel.run(state, ctx)

    assert client.calls == 1
    assert state.degraded is True


async def test_the_devils_advocate_marks_a_failed_critique_degraded() -> None:
    """Critical Safety Rule #5: the dissent is always shown. Here there is none to show.

    A case that reaches the clinician with no counter-argument, and no marker saying the
    counter-argument was attempted and lost, reads as a case nothing could be said against.
    """
    state, (ctx, client) = _state(), _ctx()
    state.hypothesis_set.append(
        Hypothesis(diagnosis_name="Acute coronary syndrome", probability_band="high")
    )
    await devils_advocate.run(state, ctx)

    assert client.calls == 1
    assert state.degraded is True


async def test_the_investigation_strategist_marks_a_failed_plan_degraded() -> None:
    state, (ctx, client) = _state(), _ctx()
    state.hypothesis_set.append(
        Hypothesis(diagnosis_name="Acute coronary syndrome", probability_band="high")
    )
    await investigation_strategist.run(state, ctx)

    assert client.calls == 1
    assert state.degraded is True


async def test_the_verifier_marks_its_own_failed_re_check_degraded() -> None:
    """The worst of the four, because this node is the gate (Critical Safety Rule #1).

    It already escalates and states the failure when its re-check does not complete — that part
    was right. What it did not do was mark the case degraded, so ``degraded`` said the engine had
    run at full strength on a case nothing had cross-checked. ``verifier_llm`` is a separate
    client precisely so the gate can be pointed elsewhere, which means it can fail while every
    other node is healthy: without this the run carried no degraded marker at all.
    """
    state, (ctx, client) = _state(), _ctx()
    state.hypothesis_set.append(
        Hypothesis(diagnosis_name="Acute coronary syndrome", probability_band="high")
    )
    await verifier.run(state, ctx)

    assert client.calls == 1
    assert state.degraded is True
    # The escalation and the stated failure, which were already correct, must survive the fix.
    assert state.autonomy_tier == "flag_for_review"
    assert any("did not complete" in c for v in state.verifier_verdicts for c in v.caveats)


# --- The three that already read it correctly, pinned so they stay that way ----------------------


async def test_the_triage_agent_marks_a_failed_round_degraded() -> None:
    state, (ctx, client) = _state(), _ctx()
    await triage_intake.run(state, ctx)

    assert client.calls == 1
    assert state.degraded is True


async def test_the_hypothesis_panel_marks_a_failed_panel_degraded() -> None:
    state, (ctx, client) = _state(), _ctx()
    await hypothesis_panel.run(state, ctx)

    assert client.calls >= 1
    assert state.degraded is True


async def test_the_guideline_agent_marks_a_failed_grounding_degraded() -> None:
    """The node the other four should have been copying.

    It already gates on whether the model *answered* rather than on whether a key exists, and
    falls back to presenting the retrieved chunks as cited options. A corpus is required to reach
    that path at all: with nothing retrieved there is no call to fail and nothing to ground, so
    the node is not the reason the case is degraded.
    """
    state, (ctx, client) = _state(), _ctx()
    ctx.retrieve = lambda query, k: [
        {
            "section_id": "icmr-acs-1",
            "source": "ICMR STW",
            "document_title": "Acute Coronary Syndrome",
            "heading": "Initial management",
            "content": "Aspirin 325 mg chewed at first medical contact.",
            "score": 0.91,
            "corpus_version": "2024.1",
            "page_range": "12",
        }
    ]
    state.hypothesis_set.append(
        Hypothesis(diagnosis_name="Acute coronary syndrome", probability_band="high")
    )
    await guideline_rag.run(state, ctx)

    assert client.calls == 1
    assert state.degraded is True
    # The chunks still reach the clinician — the fallback grounds them deterministically.
    assert state.management_options


# --- The property, stated once over the whole engine --------------------------------------------


async def test_a_run_where_every_provider_call_fails_is_degraded_end_to_end() -> None:
    """The shape an outage actually takes: not one node failing, but all of them.

    Pinned as a property of the pipeline rather than of any one node, so an agent added later
    (the module pattern in CLAUDE.md invites exactly that) cannot reintroduce the configuration
    check and go unnoticed. The Verifier is what converts the flag into something the clinician
    sees, so the assertions run through it.
    """
    from app.agents import graph

    state, (ctx, client) = _state(), _ctx()
    state.intake_complete = True
    await graph.run_reasoning(state, ctx)

    assert client.calls > 0
    assert state.degraded is True
    # What ``degraded`` buys the clinician, per ``verifier._deterministic_floor``.
    assert state.autonomy_tier == "flag_for_review"


async def test_a_healthy_run_is_not_marked_degraded() -> None:
    """The fence: ``degraded`` has to stay meaningful, or the badge is noise on every case.

    A model that answers is not degradation, and neither is a model that answers "nothing to
    add" — an empty *list* inside a well-formed response is a real answer.
    """

    class _AnsweringLLM(LLMClient):
        def available(self) -> bool:
            return True

        def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
            return {
                "cant_miss": [],
                "critique": {"summary": "The presentation may be musculoskeletal."},
                "status": "agree",
                "autonomy_tier": "suggestive",
            }

    async def emit(event: str, data: dict) -> None:
        return None

    client = _AnsweringLLM()
    ctx = ReasoningContext(llm=client, verifier_llm=client, emit=emit)
    state = _state()
    state.hypothesis_set.append(
        Hypothesis(diagnosis_name="Acute coronary syndrome", probability_band="high")
    )

    await cant_miss_sentinel.run(state, ctx)
    await devils_advocate.run(state, ctx)
    await verifier.run(state, ctx)

    assert state.degraded is False
