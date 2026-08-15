"""Agent 8: Synthesis/Orchestrator output node — reconcile, preserving disagreement.

Builds the final, immutable-ready ``ClinicalSuggestion`` payloads from the CaseState. It does
NOT hide disagreement: the devil's-advocate critique, dissenting specialists, can't-miss flags
and verifier caveats are all carried into the output. Evidence is ordered BEFORE conclusions in
the payload to support the anti-automation-bias UI contract.
"""

from __future__ import annotations

from typing import Any

from app.agents.context import ReasoningContext
from app.agents.state import CaseState
from app.agents.util import as_text
from app.core.clinical_language import prescriber_framed

AGENT = "synthesis"


def _framed(value: Any) -> tuple[str, bool]:
    """Model-written text, flattened and then forced into prescriber framing.

    The two coercions that every model-sourced string reaching a ``ClinicalSuggestion`` needs,
    in the order they have to happen: ``as_text`` first, because a value that arrived as an
    object or a list is not a sentence and cannot be pattern-matched as one, and
    :func:`~app.core.clinical_language.prescriber_framed` second, because Critical Safety Rule
    #4 is about the modality of the sentence that results.

    This is the chokepoint on purpose. Every clinical string the engine emits is built here, so
    a rule enforced at this one function cannot be bypassed by an agent added later — the same
    argument ``agents.util.call_llm`` makes for appending the untrusted-data framing at the one
    place a call leaves the process.
    """
    return prescriber_framed(as_text(value))


async def run(state: CaseState, ctx: ReasoningContext) -> dict[str, Any]:
    await ctx.emit("agent_start", {"agent": AGENT, "label": "Synthesis"})
    suggestions = build_suggestions(state)
    state.add_trace(AGENT, f"Synthesised {len(suggestions)} clinical suggestions", {})
    await ctx.emit(
        "synthesis",
        {
            "agent": AGENT,
            "autonomy_tier": state.autonomy_tier,
            "suggestion_count": len(suggestions),
        },
    )
    await ctx.emit("agent_complete", {"agent": AGENT})
    return {"suggestions": suggestions, "case_state": state.to_dict()}


def build_suggestions(state: CaseState) -> list[dict[str, Any]]:
    """Produce ClinicalSuggestion-shaped dicts (the service persists them immutably)."""
    out: list[dict[str, Any]] = []

    # --- Differential diagnoses (evidence before conclusion). ---
    for h in state.leading_hypotheses(8):
        tier = "flag_for_review" if h.cant_miss_flag else state.autonomy_tier
        # Coerced to text and then to prescriber framing: ``body`` is rendered as a string in
        # the UI, so a non-string rationale from a non-conforming model must never reach the
        # client — and neither must an imperative or certain one. The evidence lists below are
        # deliberately NOT reframed; see ``app.core.clinical_language``.
        title, title_reframed = _framed(h.diagnosis_name)
        body, body_reframed = _framed(h.rationale)
        out.append(
            {
                "output_type": "cant_miss" if h.cant_miss_flag else "differential",
                "autonomy_tier": tier,
                "confidence_band": h.probability_band,
                "title": title,
                "body": body or None,
                "cant_miss_flag": h.cant_miss_flag,
                "evidence": {
                    # Evidence intentionally listed first; the conclusion is the title.
                    "evidence_for": [
                        {"text": e.text, "source": e.source, "source_ref": e.source_ref}
                        for e in h.evidence_for
                    ],
                    "evidence_against": [
                        {"text": e.text, "source": e.source, "source_ref": e.source_ref}
                        for e in h.evidence_against
                    ],
                    "source_agent": h.source_agent,
                    "icd_code": h.icd_code,
                    "language_reframed": title_reframed or body_reframed,
                },
                "devils_advocate": h.devil_advocate or {},
                "verifier_verdict": _verdict_for(state, h.diagnosis_name),
                "citations": [],
            }
        )

    # --- Investigations. ---
    if state.recommended_investigations:
        out.append(
            {
                "output_type": "investigation",
                "autonomy_tier": "suggestive",
                "confidence_band": None,
                "title": "Suggested next investigations",
                "body": "Most discriminating tests to narrow the differential.",
                "evidence": {
                    "investigations": [
                        {
                            "name": i.name,
                            "rationale": i.rationale,
                            "expected_information_gain": i.expected_information_gain,
                            "cost_estimate": i.cost_estimate,
                            "availability_tier": i.availability_tier,
                        }
                        for i in state.recommended_investigations
                    ]
                },
                "verifier_verdict": {},
                "devils_advocate": {},
                "citations": [],
            }
        )

    # --- Cited management options. ---
    for opt in state.management_options:
        citations = [
            {
                "section_id": c.section_id,
                "source": c.source,
                "document_title": c.document_title,
                "heading": c.heading,
                "snippet": c.snippet,
                "score": c.score,
                "corpus_version": c.corpus_version,
                "page_range": c.page_range,
            }
            for c in opt.citations
        ]
        # A conflict with this patient's own record outranks how well the option is cited: a
        # perfectly-supported guideline recommendation for a drug they are allergic to is the
        # most dangerous thing this pipeline can emit, precisely because everything about its
        # presentation says it was checked. It is escalated, titled for what it is, and carries
        # the flags with it — the conservative reading wins (Critical Safety Rules #2 and #3).
        conflicted = bool(opt.safety_flags)
        blocked = opt.has_hard_block()
        # The management option is guideline text as the RAG agent summarised it, so it is
        # model-written and reframed like any other. This is the output type the rule is most
        # about: it is the one that names a drug and a dose.
        option_text, option_reframed = _framed(opt.text)
        out.append(
            {
                "output_type": "management",
                "autonomy_tier": "flag_for_review"
                if conflicted or not opt.sufficient_support
                else state.autonomy_tier,
                "confidence_band": None,
                "title": (
                    "Guideline-supported management option — conflicts with this patient's record"
                    if conflicted
                    else "Guideline-supported management option"
                ),
                "body": option_text or None,
                "is_hard_block": blocked,
                "evidence": {
                    "sufficient_support": opt.sufficient_support,
                    "safety_flags": opt.safety_flags,
                    "language_reframed": option_reframed,
                },
                "verifier_verdict": {},
                "devils_advocate": {},
                "citations": citations,
            }
        )

    # --- Safety hard blocks (always surfaced). ---
    for block in state.hard_blocks:
        out.append(
            {
                "output_type": "safety",
                "autonomy_tier": "flag_for_review",
                "confidence_band": None,
                "title": "Drug-safety hard block",
                "body": as_text(block.summary) or None,
                "is_hard_block": True,
                "evidence": block.details,
                "verifier_verdict": {},
                "devils_advocate": {},
                "citations": [],
            }
        )

    return out


def _verdict_for(state: CaseState, target: str) -> dict[str, Any]:
    for v in state.verifier_verdicts:
        if v.target.strip().lower() == target.strip().lower():
            return {"status": v.status, "rationale": v.rationale, "caveats": v.caveats}
    # Fall back to the case-level verdict.
    for v in state.verifier_verdicts:
        if v.target == "case":
            return {"status": v.status, "rationale": v.rationale, "caveats": v.caveats}
    return {"status": state.verifier_status, "caveats": []}
