"""The reasoning StateGraph orchestrator (architecture §3.3–3.4).

A self-contained async state machine mirroring the LangGraph node/edge model: entry at triage,
an interactive intake loop (human-in-the-loop answers between rounds), then the linear
safety-enriched pipeline, a conditional verifier gate (PASS → synthesis, DISAGREE →
conservative resolution → synthesis). Nodes are the agent ``run`` functions; this module owns
the edges, the deterministic drug-safety node, and event sequencing for the Reasoning Theatre.

The Verifier node is wired unconditionally between the pipeline and synthesis — there is no
edge that reaches synthesis without passing through it (Critical Safety Rule #1).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.agents import (
    cant_miss_sentinel,
    conservative,
    devils_advocate,
    guideline_rag,
    hypothesis_panel,
    investigation_strategist,
    synthesis,
    triage_intake,
    verifier,
)
from app.agents.context import ReasoningContext, resolve_safety
from app.agents.llm import using_simulated_llm
from app.agents.state import CaseState, HardBlock
from app.core.logsafe import describe_exception

logger = logging.getLogger(__name__)

# Order in which the Reasoning Theatre should show the agent lanes.
AGENT_SEQUENCE = [
    "triage_intake",
    "hypothesis_panel",
    "cant_miss_sentinel",
    "devils_advocate",
    "investigation_strategist",
    "guideline_rag",
    "drug_safety_check",
    "verifier",
    "synthesis",
]


async def run_triage_round(state: CaseState, ctx: ReasoningContext) -> None:
    """One intake round. Caller persists questions and collects answers before the next call."""
    if using_simulated_llm():
        state.demo_mode = True
    await triage_intake.run(state, ctx)


async def drug_safety_check(state: CaseState, ctx: ReasoningContext) -> None:
    """Deterministic drug-safety node (no LLM). Populates flags + hard blocks.

    Two passes, both offline and rule-based (Critical Safety Rules #3 and #8):

    1. **The patient's current medications**, evaluated against each other — the allergy,
       interaction, contraindication and renal flags already live on the chart.
    2. **The drugs named in the management options this run just produced.** This pass is why
       the node is wired after ``guideline_rag`` rather than beside it. A retrieved guideline is
       written for a population and cannot know this patient: the shipped ICMR corpus says
       "Paracetamol is preferred for fever" in its dengue workflow and "Metformin is the
       preferred first-line pharmacotherapy" in its diabetes one, and both reach the clinician
       as cited management options. Nothing checked either against the documented allergy or
       the measured eGFR sitting in the same case state, so a patient allergic to paracetamol
       was shown a guideline-cited suggestion to give it — the exact conflict Rule #3 says must
       be a hard block rather than a warning.

    Hard blocks from either pass are always surfaced and force FLAG_FOR_REVIEW downstream.
    """
    await ctx.emit("agent_start", {"agent": "drug_safety_check", "label": "Drug Safety Check"})
    flags = await resolve_safety(ctx.evaluate_safety(""))
    state.drug_safety_flags = flags
    for f in flags:
        record_hard_block(state, f)

    option_flag_count = 0
    for option in state.management_options:
        # The option's own text is the only thing that names a drug; its citations are the
        # guideline sections it came from, which name the same drugs in the same words.
        option.safety_flags = await resolve_safety(ctx.evaluate_safety(option.text))
        option_flag_count += len(option.safety_flags)
        for f in option.safety_flags:
            record_hard_block(state, f)

    state.add_trace(
        "drug_safety_check",
        f"{len(flags)} active safety flags, {option_flag_count} on management options, "
        f"{len(state.hard_blocks)} hard blocks",
        {},
    )
    await ctx.emit(
        "drug_safety",
        {
            "agent": "drug_safety_check",
            "flags": flags,
            "management_flags": [
                {"option": o.text, "flags": o.safety_flags}
                for o in state.management_options
                if o.safety_flags
            ],
            "hard_blocks": len(state.hard_blocks),
        },
    )
    await ctx.emit("agent_complete", {"agent": "drug_safety_check"})


def record_hard_block(state: CaseState, flag: dict[str, Any]) -> None:
    """Promote a hard-blocking flag onto ``state.hard_blocks``, once.

    Deduplicated on the summary because the two passes legitimately overlap: a management
    option naming a drug the patient is already on raises the same conflict the current-
    medication pass just raised, and the clinician should see one hard block, not two
    identically-worded ones.

    Public because the same dedup has to govern the post-panel re-check in
    ``ReasoningService._recheck_chart_safety``, which promotes blocks onto a state this node has
    already written to. A second implementation of "once" would drift from this one.
    """
    if not flag.get("is_hard_block"):
        return
    summary = flag.get("summary", "Hard block")
    if any(b.summary == summary for b in state.hard_blocks):
        return
    state.hard_blocks.append(
        HardBlock(
            summary=summary,
            check_type=flag.get("check_type", "drug_interaction"),
            details=flag.get("details", {}),
        )
    )


AgentNode = Callable[[CaseState, ReasoningContext], Awaitable[None]]


async def _advisory(agent: str, node: AgentNode, state: CaseState, ctx: ReasoningContext) -> None:
    """Run one *advisory* node so that a crash costs its lane rather than the whole case.

    Every node in the pipeline already has a deterministic fallback for "the model gave us
    nothing usable", and the recurring defect in this engine has been that the fallback never
    gets to run: the response arrives, something in it is the wrong *shape*, and the exception
    escapes the node — past the point where "no usable answer" would have routed offline.
    Individual parse sites have been hardened one at a time (``objects``, ``_parse_evidence``,
    ``as_float``, ``_recognised``); each of those was found by a clinician losing a run. This is
    the same guarantee stated once, for the class rather than the instance: whatever an advisory
    node does, the case does not die with it.

    Failing the whole run instead is not the conservative choice it looks like. A crash in the
    Devil's-Advocate after the panel has produced a full differential currently throws that
    differential away and hands the clinician an error; nothing is safer about an empty screen.

    What makes this safe rather than merely lenient is that the degradation is *loud* — this is
    not a swallowed exception. ``record_agent_failure`` marks the case degraded and names the
    lane, the Verifier's deterministic floor escalates to flag-for-review and says which agent
    did not run (Critical Safety Rule #2), the Theatre paints the lane as failed rather than
    done, and the whole thing is in the immutable ``case_state`` snapshot. The clinician is told
    that part of the panel is missing; they are not quietly shown a thinner case as if it were a
    whole one.

    Only advisory nodes are run this way. The deterministic drug-safety node, the Verifier and
    synthesis are not: a case that reached the clinician without the allergy check, without the
    gate (Rule #1) or without an output is not a degraded case, it is a wrong one, so a failure
    in any of those still fails the run.
    """
    try:
        await node(state, ctx)
    except Exception as exc:  # noqa: BLE001 — the point of this function
        reason = describe_exception(exc)
        logger.exception(
            "Reasoning agent %r failed; the case continues without it, degraded and escalated.",
            agent,
        )
        state.record_agent_failure(agent, reason)
        try:
            await ctx.emit("agent_failed", {"agent": agent, "reason": reason})
        except Exception:  # noqa: BLE001 — a dead stream must not undo the recovery
            # The emitter is the clinician's SSE queue, and it fails when they have closed the
            # tab. Letting that escape here would turn a contained agent failure back into a
            # failed run, which is the exact thing this function exists to prevent.
            logger.warning("Could not stream the failure of agent %r to the Theatre.", agent)


async def run_reasoning(state: CaseState, ctx: ReasoningContext) -> dict[str, Any]:
    """Run the full pipeline after intake is complete; returns synthesis output.

    Edges (architecture §3.4):
        hypothesis_panel → cant_miss_sentinel → devils_advocate → investigation_strategist
        → guideline_rag → drug_safety_check → verifier
        → [conditional] synthesis | conservative_resolution → synthesis
    """
    if using_simulated_llm():
        state.demo_mode = True
    await ctx.emit("reasoning_start", {"sequence": AGENT_SEQUENCE, "demo_mode": state.demo_mode})

    # Advisory nodes: a crash costs the lane, not the case. See ``_advisory``.
    await _advisory("hypothesis_panel", hypothesis_panel.run, state, ctx)
    await _advisory("cant_miss_sentinel", cant_miss_sentinel.run, state, ctx)
    await _advisory("devils_advocate", devils_advocate.run, state, ctx)
    await _advisory("investigation_strategist", investigation_strategist.run, state, ctx)
    await _advisory("guideline_rag", guideline_rag.run, state, ctx)

    # Not advisory. The deterministic safety pass is what Rules #3 and #8 are about, and a run
    # that could not complete it has not checked this patient's allergies against the drugs it
    # is about to name. There is no degraded version of that.
    await drug_safety_check(state, ctx)

    # Verifier gate — mandatory, cannot be bypassed.
    await verifier.run(state, ctx)

    # Conditional edge: DISAGREE → conservative resolution.
    if state.verifier_status in ("partial_disagreement", "major_disagreement"):
        await conservative.run(state, ctx)

    output = await synthesis.run(state, ctx)
    await ctx.emit(
        "reasoning_complete",
        {
            "autonomy_tier": state.autonomy_tier,
            "verifier_status": state.verifier_status,
            "degraded": state.degraded,
            "demo_mode": state.demo_mode,
            "suggestion_count": len(output["suggestions"]),
        },
    )
    return output
