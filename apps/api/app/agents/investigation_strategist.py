"""Agent 5: Investigation Strategist — the single most discriminating next test."""

from __future__ import annotations

from app.agents.context import ReasoningContext
from app.agents.prompts import INVESTIGATION_STRATEGIST
from app.agents.state import CaseState, Investigation
from app.agents.tools import compute_discriminating_test
from app.agents.untrusted import fenced
from app.agents.util import as_text, call_llm, norm_band, objects
from app.core.clinical_language import prescriber_framed

AGENT = "investigation_strategist"
_TIERS = {"phc", "chc", "district_hospital", "referral"}


async def run(state: CaseState, ctx: ReasoningContext) -> None:
    await ctx.emit("agent_start", {"agent": AGENT, "label": "Investigation Strategist"})
    leaders = state.leading_hypotheses(5)
    result = await call_llm(
        ctx,
        INVESTIGATION_STRATEGIST,
        "Leading hypotheses:\n"
        + fenced(
            "leading hypotheses",
            "\n".join(f"{h.diagnosis_name} ({h.probability_band})" for h in leaders),
        ),
    )
    investigations: list[Investigation] = []
    if result:
        for inv in objects(result.get("investigations")):
            name = as_text(inv.get("name"))
            if not name:
                continue
            tier = inv.get("availability_tier")
            # Critical Safety Rule #4, applied here rather than in ``synthesis``: the rationale
            # is emitted on the ``investigations`` SSE event below and read in the Theatre long
            # before a suggestion is built, so reframing it downstream would leave the sentence
            # the clinician saw untouched. It is also the field most likely to carry an order —
            # "Start empirical antibiotics while awaiting the culture" is a natural way for a
            # model to justify a test, and it is a prescribing instruction. The test *name* is
            # left alone: it is a noun, not a claim.
            rationale, _ = prescriber_framed(as_text(inv.get("rationale")))
            investigations.append(
                Investigation(
                    name=name,
                    # Coerced, not passed through: these are rendered as strings on the
                    # suggestion card, and an object here reaches the client as one.
                    rationale=rationale,
                    expected_information_gain=norm_band(
                        inv.get("expected_information_gain"), "moderate"
                    ),
                    cost_estimate=as_text(inv.get("cost_estimate")) or None,
                    availability_tier=tier if tier in _TIERS else "phc",
                )
            )
    if not investigations:
        # Reaching this branch *is* the degradation — see the same fix in ``cant_miss_sentinel``.
        # ``ctx.llm_available()`` only reports whether a key is configured, never whether the
        # call worked, so a provider that was down left the case marked healthy. The condition is
        # "nothing usable came back" rather than "the call failed", for the reason
        # ``guideline_rag`` spells out at ``grounded_by_model``: a response nothing survived
        # parsing from is not an answer of "no investigations", and either way what the clinician
        # ends up reading is ``compute_discriminating_test``'s deterministic output.
        state.degraded = True
        investigations = compute_discriminating_test(leaders)

    state.recommended_investigations.extend(investigations)
    state.add_trace(AGENT, f"Recommended {len(investigations)} discriminating tests", {})
    await ctx.emit(
        "investigations",
        {
            "agent": AGENT,
            "investigations": [
                {
                    "name": i.name,
                    "rationale": i.rationale,
                    "expected_information_gain": i.expected_information_gain,
                    "cost_estimate": i.cost_estimate,
                    "availability_tier": i.availability_tier,
                }
                for i in investigations
            ],
        },
    )
    await ctx.emit("agent_complete", {"agent": AGENT})
