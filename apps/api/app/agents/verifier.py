"""Agent 7: Verifier (gatekeeper) — independent re-check; conservative output wins.

CANNOT be bypassed: the orchestrator routes every case through this node before synthesis.
Assigns the case autonomy tier. The deterministic safety floor here means a correct, cautious
tier is assigned even when the LLM is unavailable — escalation can only make the output MORE
conservative, never less.
"""

from __future__ import annotations

from app.agents.context import ReasoningContext
from app.agents.prompts import VERIFIER
from app.agents.state import (
    CaseState,
    VerifierVerdict,
    more_conservative_tier,
)
from app.agents.tools import summarize_snapshot
from app.agents.util import call_llm

AGENT = "verifier"
_VALID_STATUS = {"agree", "partial_disagreement", "major_disagreement"}
_VALID_TIER = {"informational", "suggestive", "flag_for_review"}


async def run(state: CaseState, ctx: ReasoningContext) -> None:
    await ctx.emit("agent_start", {"agent": AGENT, "label": "Verifier (gatekeeper)"})

    # --- Deterministic safety floor (always computed; cannot be lowered by the LLM). ---
    floor_tier, floor_reasons = _deterministic_floor(state)
    tier = floor_tier
    status = "agree"
    case_caveats = list(floor_reasons)
    verdicts: list[VerifierVerdict] = []

    summary = summarize_snapshot(state.patient_graph_snapshot)
    leaders = state.leading_hypotheses(5)
    result = await call_llm(
        ctx,
        VERIFIER,
        f"Presenting complaint: {state.presenting_complaint}\nRecord:\n{summary}\n\n"
        f"Hypotheses to verify: "
        + "; ".join(f"{h.diagnosis_name} ({h.probability_band})" for h in leaders)
        + f"\nManagement options: {len(state.management_options)}; "
        f"hard blocks: {len(state.hard_blocks)}",
        verifier=True,
    )
    if result:
        status = result.get("status", "agree")
        status = status if status in _VALID_STATUS else "agree"
        llm_tier = result.get("autonomy_tier", "suggestive")
        if llm_tier in _VALID_TIER:
            tier = more_conservative_tier(tier, llm_tier)  # conservative wins
        for v in result.get("verdicts", []):
            verdicts.append(
                VerifierVerdict(
                    target=v.get("target", ""),
                    status=v.get("status", "agree")
                    if v.get("status") in _VALID_STATUS
                    else "agree",
                    rationale=v.get("rationale", ""),
                    caveats=list(v.get("caveats", [])),
                )
            )
        case_caveats.extend(result.get("case_caveats", []))
    else:
        state.degraded = state.degraded or not ctx.llm_available()

    # Disagreement among source specialists also forces escalation (conservative).
    if _specialists_disagree(state):
        status = "partial_disagreement" if status == "agree" else status
        tier = more_conservative_tier(tier, "flag_for_review")
        case_caveats.append("Specialist agents disagreed on the leading hypothesis.")

    if not verdicts:
        verdicts.append(
            VerifierVerdict(
                target="case",
                status=status,
                rationale="Deterministic verification floor applied.",
                caveats=case_caveats,
            )
        )

    state.verifier_verdicts = verdicts
    state.verifier_status = status
    state.autonomy_tier = tier
    state.add_trace(
        AGENT,
        f"Verifier verdict: {status}; autonomy tier {tier}",
        {"caveats": case_caveats},
    )
    await ctx.emit(
        "verifier",
        {
            "agent": AGENT,
            "status": status,
            "autonomy_tier": tier,
            "case_caveats": case_caveats,
            "verdicts": [
                {"target": v.target, "status": v.status, "rationale": v.rationale} for v in verdicts
            ],
        },
    )
    await ctx.emit("agent_complete", {"agent": AGENT})


def _deterministic_floor(state: CaseState) -> tuple[str, list[str]]:
    """The minimum (most conservative) tier mandated by hard rules, with reasons."""
    reasons: list[str] = []
    tier = "informational"
    if state.management_options or state.hypothesis_set:
        tier = "suggestive"
    if any(h.cant_miss_flag for h in state.hypothesis_set):
        tier = "flag_for_review"
        reasons.append("A can't-miss diagnosis is on the differential.")
    if state.hard_blocks:
        tier = "flag_for_review"
        reasons.append("A drug-safety hard block was triggered.")
    if any(f.get("severity") in ("warning", "critical") for f in state.drug_safety_flags):
        tier = "flag_for_review"
        reasons.append("A drug-safety warning is present.")
    if state.management_options and not any(o.citations for o in state.management_options):
        tier = more_conservative_tier(tier, "flag_for_review")
        reasons.append("Management options lack adequate guideline support.")
    if state.degraded:
        reasons.append("AI reasoning ran in degraded mode; treat output with extra caution.")
        tier = more_conservative_tier(tier, "flag_for_review")
    return tier, reasons


def _specialists_disagree(state: CaseState) -> bool:
    leaders = state.leading_hypotheses(3)
    sources = {h.source_agent for h in leaders if h.source_agent not in ("sentinel",)}
    top_names = {h.diagnosis_name for h in leaders}
    return len(sources) >= 2 and len(top_names) >= 3
