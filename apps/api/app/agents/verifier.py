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
from app.agents.untrusted import fenced
from app.agents.util import as_text, call_llm, text_list

AGENT = "verifier"
_VALID_STATUS = {"agree", "partial_disagreement", "major_disagreement"}
_VALID_TIER = {"informational", "suggestive", "flag_for_review"}


def _recognised(value: object, allowed: set[str], default: str | None) -> str | None:
    """Return `value` only if it is a string the gate recognises, else `default`.

    The isinstance check is not belt-and-braces: ``allowed`` is a set, so a bare ``value in
    allowed`` hashes the left operand and raises TypeError on the list or dict a
    non-conforming model returns where a string was asked for. That exception would escape
    ``run`` -- and the verifier is the one node the graph has no edge around, so a model
    answering ``{"status": ["agree"]}`` would fail the whole case rather than fall back.
    Everything unrecognised is treated as *no answer*, which leaves the deterministic floor
    standing; that is the safe direction, because the floor can only be escalated from.
    """
    return value if isinstance(value, str) and value in allowed else default


# Caveats are a model-supplied list of sentences, coerced the same way every other agent's is.
# This guard started here and now lives in ``util`` because the Devil's-Advocate needed it too.
_caveat_list = text_list


async def run(state: CaseState, ctx: ReasoningContext) -> None:
    await ctx.emit("agent_start", {"agent": AGENT, "label": "Verifier (gatekeeper)"})

    # --- Deterministic safety floor (always computed; cannot be lowered by the LLM). ---
    floor_tier, floor_reasons = _deterministic_floor(state)
    tier = floor_tier
    status = "agree"
    case_caveats = list(floor_reasons)
    verdicts: list[VerifierVerdict] = []
    unverified = False

    summary = summarize_snapshot(state.patient_graph_snapshot)
    leaders = state.leading_hypotheses(5)
    result = await call_llm(
        ctx,
        VERIFIER,
        # The gate is the agent an injection most wants to reach: everything the other seven
        # produce is re-checked here, and this is where the autonomy tier is set. So the record
        # text and the hypothesis names -- both of which the panel may have carried an
        # injection into -- are fenced, while the two counts below are ours and are not.
        "Presenting complaint:\n"
        + fenced("presenting complaint", state.presenting_complaint)
        + "\n\nRecord:\n"
        + fenced("patient record", summary)
        + "\n\nHypotheses to verify:\n"
        + fenced(
            "hypotheses to verify",
            "\n".join(f"{h.diagnosis_name} ({h.probability_band})" for h in leaders),
        )
        + f"\n\nManagement options: {len(state.management_options)}; "
        f"hard blocks: {len(state.hard_blocks)}",
        verifier=True,
    )
    if result:
        status = _recognised(result.get("status"), _VALID_STATUS, "agree") or "agree"
        llm_tier = _recognised(result.get("autonomy_tier"), _VALID_TIER, None)
        if llm_tier:
            tier = more_conservative_tier(tier, llm_tier)  # conservative wins
        raw_verdicts = result.get("verdicts")
        for v in raw_verdicts if isinstance(raw_verdicts, (list | tuple)) else []:
            # A verdict that is not an object carries no target to attach it to, so there is
            # nothing to keep -- and calling .get() on it would raise inside the ungated node.
            if not isinstance(v, dict):
                continue
            verdicts.append(
                VerifierVerdict(
                    target=as_text(v.get("target")),
                    status=_recognised(v.get("status"), _VALID_STATUS, "agree") or "agree",
                    rationale=as_text(v.get("rationale")),
                    caveats=_caveat_list(v.get("caveats")),
                )
            )
        case_caveats.extend(_caveat_list(result.get("case_caveats")))
    else:
        # The independent re-check did not happen. Until now that was silent: the node fell
        # through to its deterministic floor, reported ``status: agree`` with the neutral
        # rationale below, and the case reached the clinician looking exactly like one a working
        # gate had agreed with. The floor is a floor, not a verification -- it re-reads flags the
        # pipeline already raised and cannot notice anything the agents got wrong, which is the
        # entire job of this node (Critical Safety Rule #1). A gate that no-ops silently is a
        # bypassed gate, so the failure is stated and the case is escalated for a human.
        #
        # Reachable without the rest of the pipeline noticing, because ``verifier_llm`` is a
        # separate client: the agents' provider can be up and answering while the gate's is down,
        # and every other node reports itself perfectly healthy.
        unverified = True
        # Reaching this branch *is* the degradation. ``verifier_llm_available()`` answers "is the
        # gate's key configured", never "did the re-check happen", so the one failure this node
        # was written to notice — a keyed gate provider that is down, revoked or out of quota —
        # was the one it recorded as healthy. Worse here than at any other node: the separate
        # client means the gate can fail while all seven agents report themselves fine, so
        # ``degraded`` stayed false for the whole run and the escalation below was the only trace
        # left. See ``tests/test_agent_degraded_on_provider_failure.py``.
        state.degraded = True
        tier = more_conservative_tier(tier, "flag_for_review")
        case_caveats.append(
            "The independent verification re-check did not complete, so nothing has "
            "cross-checked the reasoning below; this case is escalated for active clinician "
            "review."
        )

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
                rationale=(
                    "Independent re-check unavailable; deterministic verification floor applied "
                    "on its own."
                    if unverified
                    else "Deterministic verification floor applied."
                ),
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
    if state.failed_agents:
        # Named, not counted. "Reasoning was degraded" is a caveat a clinician learns to read
        # past; "the Devil's-Advocate did not run on this case" tells them which specific
        # cross-check is missing from what they are looking at, which is the whole point of
        # showing dissent at all (Rule #5). The escalation is redundant with the ``degraded``
        # branch above by construction — ``record_agent_failure`` sets both — and is stated
        # anyway so that this reason can never appear on a case that was not escalated.
        tier = more_conservative_tier(tier, "flag_for_review")
        reasons.append(
            "These agents did not complete and contributed nothing to this case: "
            + ", ".join(agent.replace("_", " ") for agent in state.failed_agents)
            + "."
        )
    return tier, reasons


def _specialists_disagree(state: CaseState) -> bool:
    leaders = state.leading_hypotheses(3)
    sources = {h.source_agent for h in leaders if h.source_agent not in ("sentinel",)}
    top_names = {h.diagnosis_name for h in leaders}
    return len(sources) >= 2 and len(top_names) >= 3
