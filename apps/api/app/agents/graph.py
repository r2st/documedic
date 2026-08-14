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
from app.agents.context import ReasoningContext, resolve_safety
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
        _record_hard_block(state, f)

    option_flag_count = 0
    for option in state.management_options:
        # The option's own text is the only thing that names a drug; its citations are the
        # guideline sections it came from, which name the same drugs in the same words.
        option.safety_flags = await resolve_safety(ctx.evaluate_safety(option.text))
        option_flag_count += len(option.safety_flags)
        for f in option.safety_flags:
            _record_hard_block(state, f)

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


def _record_hard_block(state: CaseState, flag: dict[str, Any]) -> None:
    """Promote a hard-blocking flag onto ``state.hard_blocks``, once.

    Deduplicated on the summary because the two passes legitimately overlap: a management
    option naming a drug the patient is already on raises the same conflict the current-
    medication pass just raised, and the clinician should see one hard block, not two
    identically-worded ones.
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
