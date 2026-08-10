"""Agent 4: Devil's-Advocate — adversarially attacks the leading hypothesis (anti-bias).

The critique is ALWAYS produced and ALWAYS surfaced to the clinician; it is attached to the
leading hypothesis and emitted as its own event so the UI can render a non-collapsible section.
"""

from __future__ import annotations

from app.agents.context import ReasoningContext
from app.agents.prompts import DEVILS_ADVOCATE
from app.agents.state import CaseState
from app.agents.tools import summarize_snapshot
from app.agents.util import call_llm

AGENT = "devils_advocate"


async def run(state: CaseState, ctx: ReasoningContext) -> None:
    await ctx.emit("agent_start", {"agent": AGENT, "label": "Devil's Advocate"})
    leaders = state.leading_hypotheses(1)
    if not leaders:
        await ctx.emit("agent_complete", {"agent": AGENT})
        return
    leader = leaders[0]
    summary = summarize_snapshot(state.patient_graph_snapshot)

    result = await call_llm(
        ctx,
        DEVILS_ADVOCATE,
        f"Leading hypothesis: {leader.diagnosis_name}\n"
        f"Presenting complaint: {state.presenting_complaint}\nRecord:\n{summary}",
    )
    if result:
        critique = {
            "leading_hypothesis": leader.diagnosis_name,
            "disconfirming_evidence": result.get("disconfirming_evidence", []),
            "alternative_explanations": result.get("alternative_explanations", []),
            "base_rate_caveat": result.get("base_rate_caveat", ""),
            "summary": result.get("summary", ""),
        }
    else:
        state.degraded = state.degraded or not ctx.llm_available()
        critique = _fallback(state, leader.diagnosis_name)

    leader.devil_advocate = critique
    state.add_trace(AGENT, f"Counter-argument to '{leader.diagnosis_name}'", critique)
    await ctx.emit("devils_advocate", {"agent": AGENT, "critique": critique})
    await ctx.emit("agent_complete", {"agent": AGENT})


def _fallback(state: CaseState, leading: str) -> dict:
    alternatives = [
        h.diagnosis_name for h in state.leading_hypotheses(4) if h.diagnosis_name != leading
    ][:3]
    return {
        "leading_hypothesis": leading,
        "disconfirming_evidence": [
            "No confirmatory investigation has yet been performed for this hypothesis.",
            "Symptom overlap with other conditions limits specificity.",
        ],
        "alternative_explanations": alternatives
        or ["Benign self-limiting illness", "An unlisted mimic worth excluding"],
        "base_rate_caveat": (
            "Consider local base rates: common conditions are common, but can't-miss "
            "diagnoses must still be actively excluded before reassurance."
        ),
        "summary": (
            f"Findings are not specific for {leading.lower()}; the evidence remains "
            "compatible with alternatives until a discriminating test is performed."
        ),
    }
