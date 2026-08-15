"""Agent 4: Devil's-Advocate — adversarially attacks the leading hypothesis (anti-bias).

The critique is ALWAYS produced and ALWAYS surfaced to the clinician; it is attached to the
leading hypothesis and emitted as its own event so the UI can render a non-collapsible section.
"""

from __future__ import annotations

from app.agents.context import ReasoningContext
from app.agents.prompts import DEVILS_ADVOCATE
from app.agents.state import CaseState
from app.agents.tools import summarize_snapshot
from app.agents.untrusted import fenced
from app.agents.util import as_text, call_llm, text_list

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
        # The hypothesis name is fenced too. It is not record text -- it is what the panel
        # returned -- but the panel wrote it after reading the record, so an injection that
        # reached the panel can arrive here inside a diagnosis name. Model output is untrusted
        # for the same reason record text is.
        "Leading hypothesis:\n"
        + fenced("leading hypothesis", leader.diagnosis_name)
        + "\n\nPresenting complaint:\n"
        + fenced("presenting complaint", state.presenting_complaint)
        + "\n\nRecord:\n"
        + fenced("patient record", summary),
    )
    if result:
        critique = _critique(leader.diagnosis_name, result)
    else:
        # Reaching this branch *is* the degradation — see the same fix in
        # ``cant_miss_sentinel``. ``ctx.llm_available()`` only reports whether a key is
        # configured, so a provider that was down left the case marked healthy while the
        # clinician read a template counter-argument in place of the dissent Rule #5 promises.
        state.degraded = True
        critique = _fallback(state, leader.diagnosis_name)

    leader.devil_advocate = critique
    state.add_trace(AGENT, f"Counter-argument to '{leader.diagnosis_name}'", critique)
    await ctx.emit("devils_advocate", {"agent": AGENT, "critique": critique})
    await ctx.emit("agent_complete", {"agent": AGENT})


def _critique(leading: str, result: dict) -> dict:
    """Read one Devil's-Advocate response, coercing every field to the shape the UI renders.

    This was the last agent passing model output straight through: the four fields below went
    from the response into ``CaseState``, into the persisted ``case_state`` snapshot, over SSE,
    and into ``SuggestionCard``'s ``DevilsAdvocate`` — uninspected at every step. Two of them are
    rendered by mapping over them, so a model that answered ``"disconfirming_evidence": "Nothing
    argues against it."`` — a string, not a list, and an entirely ordinary thing for a model to
    say — reached the browser as a ``.map is not a function`` and white-screened the card.

    That is a worse failure here than anywhere else in the engine. Critical Safety Rule #5 says
    the counter-argument is always shown to the clinician and cannot be hidden or collapsed; a
    malformed one took down not just the dissent but the whole suggestion it was attached to,
    leaving the leading hypothesis on screen with nothing arguing against it. Coercion keeps the
    dissent readable instead: a bare string becomes one item, an object is flattened to its text.
    """
    return {
        "leading_hypothesis": leading,
        "disconfirming_evidence": text_list(result.get("disconfirming_evidence")),
        "alternative_explanations": text_list(result.get("alternative_explanations")),
        "base_rate_caveat": as_text(result.get("base_rate_caveat")),
        "summary": as_text(result.get("summary")),
    }


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
