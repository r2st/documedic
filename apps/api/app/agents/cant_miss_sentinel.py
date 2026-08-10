"""Agent 3: Can't-Miss Sentinel — forces dangerous, time-critical diagnoses onto the list.

Always runs. Combines the deterministic rule table (offline-capable) with an LLM scan when
available. Output is append-only and flagged ``cant_miss_flag=True`` so it can never be silently
dropped downstream (Critical Safety Rule: can't-miss persistence).
"""

from __future__ import annotations

from app.agents.cant_miss import match_cant_miss
from app.agents.context import ReasoningContext
from app.agents.prompts import CANT_MISS_SENTINEL
from app.agents.state import CaseState, Evidence, Hypothesis
from app.agents.tools import summarize_snapshot
from app.agents.util import call_llm, norm_band, text_blob

AGENT = "cant_miss_sentinel"


async def run(state: CaseState, ctx: ReasoningContext) -> None:
    await ctx.emit("agent_start", {"agent": AGENT, "label": "Can't-Miss Sentinel"})
    summary = summarize_snapshot(state.patient_graph_snapshot)
    blob = text_blob(state.presenting_complaint, summary, state.intake_questions)
    existing = {h.diagnosis_name.strip().lower() for h in state.hypothesis_set}
    added: list[Hypothesis] = []

    # 1) Deterministic rules — always applied (the safety floor).
    for rule in match_cant_miss(blob):
        key = rule.diagnosis.strip().lower()
        if key in existing:
            _flag_existing(state, key, rule.why)
            continue
        added.append(
            Hypothesis(
                diagnosis_name=rule.diagnosis,
                icd_code=rule.icd_code,
                probability_band="low",
                evidence_for=[Evidence(rule.why, True, source="clinical_knowledge")],
                rationale=f"Can't-miss: {rule.why}",
                cant_miss_flag=True,
                source_agent="sentinel",
            )
        )
        existing.add(key)

    # 2) LLM augmentation (additive only).
    result = await call_llm(ctx, CANT_MISS_SENTINEL, f"Case:\n{blob}")
    if result:
        for cm in result.get("cant_miss", []):
            name = (cm.get("diagnosis_name") or "").strip()
            if not name or name.strip().lower() in existing:
                continue
            added.append(
                Hypothesis(
                    diagnosis_name=name,
                    icd_code=cm.get("icd_code"),
                    probability_band=norm_band(cm.get("probability_band"), "very_low"),
                    evidence_for=[
                        Evidence(e.get("text", ""), True, source="clinical_knowledge")
                        for e in cm.get("evidence_for", [])
                        if e.get("text")
                    ]
                    or [Evidence(cm.get("why_dangerous", ""), True, source="clinical_knowledge")],
                    rationale=cm.get("why_dangerous"),
                    cant_miss_flag=True,
                    source_agent="sentinel",
                )
            )
            existing.add(name.strip().lower())
    else:
        state.degraded = state.degraded or not ctx.llm_available()

    state.hypothesis_set.extend(added)
    state.add_trace(AGENT, f"Forced {len(added)} can't-miss diagnoses onto the differential", {})
    await ctx.emit(
        "cant_miss",
        {
            "agent": AGENT,
            "items": [{"diagnosis_name": h.diagnosis_name, "why": h.rationale} for h in added],
        },
    )
    await ctx.emit("agent_complete", {"agent": AGENT})


def _flag_existing(state: CaseState, key: str, why: str) -> None:
    for h in state.hypothesis_set:
        if h.diagnosis_name.strip().lower() == key:
            h.cant_miss_flag = True
            if not h.rationale:
                h.rationale = f"Can't-miss: {why}"
