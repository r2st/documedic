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
from app.agents.context import ReasoningContext
from app.agents.llm import using_simulated_llm
from app.agents.state import CaseState, HardBlock

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

    The injected evaluator returns the patient's current active safety flags (allergy /
    interaction / contraindication / renal). Hard blocks are always surfaced and force
    FLAG_FOR_REVIEW downstream.
    """
    await ctx.emit("agent_start", {"agent": "drug_safety_check", "label": "Drug Safety Check"})
    flags = ctx.evaluate_safety("")
    state.drug_safety_flags = flags
    for f in flags:
        if f.get("is_hard_block"):
            state.hard_blocks.append(
                HardBlock(
                    summary=f.get("summary", "Hard block"),
                    check_type=f.get("check_type", "drug_interaction"),
                    details=f.get("details", {}),
                )
            )
    state.add_trace(
        "drug_safety_check",
        f"{len(flags)} active safety flags, {len(state.hard_blocks)} hard blocks",
        {},
    )
    await ctx.emit(
        "drug_safety",
        {"agent": "drug_safety_check", "flags": flags, "hard_blocks": len(state.hard_blocks)},
    )
    await ctx.emit("agent_complete", {"agent": "drug_safety_check"})


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

    await hypothesis_panel.run(state, ctx)
    await cant_miss_sentinel.run(state, ctx)
    await devils_advocate.run(state, ctx)
    await investigation_strategist.run(state, ctx)
    await guideline_rag.run(state, ctx)
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
