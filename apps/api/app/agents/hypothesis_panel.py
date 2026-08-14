"""Agent 2: Hypothesis Panel — four role-primed specialists reason in parallel."""

from __future__ import annotations

import asyncio
from typing import Any

from app.agents.context import ReasoningContext
from app.agents.prompts import HYPOTHESIS_PANEL
from app.agents.state import CaseState, Evidence, Hypothesis
from app.agents.tools import active_condition_names, summarize_snapshot
from app.agents.util import as_text, call_llm, norm_band, objects, text_blob

AGENT = "hypothesis_panel"

SPECIALISTS = {
    "internal_medicine": "General Internal Medicine",
    "cardiology": "Cardiology",
    "infectious_disease": "Infectious Disease",
    "primary_care": "Primary Care",
}

# Deterministic fallback: complaint/condition keyword -> (specialty, diagnosis, icd, band).
_FALLBACK_MAP: list[tuple[tuple[str, ...], str, str, str | None, str]] = [
    (
        ("chest pain", "angina", "exertional"),
        "cardiology",
        "Stable/unstable angina",
        "I20",
        "moderate",
    ),
    (("palpitation", "irregular"), "cardiology", "Cardiac arrhythmia", "I49", "low"),
    (("breathless", "dyspnea", "edema", "swelling"), "cardiology", "Heart failure", "I50", "low"),
    (
        ("fever", "cough", "sputum"),
        "infectious_disease",
        "Community-acquired pneumonia",
        "J18",
        "moderate",
    ),
    (
        ("fever", "burning urine", "dysuria"),
        "infectious_disease",
        "Urinary tract infection",
        "N39.0",
        "moderate",
    ),
    (
        ("fever", "rigors", "headache"),
        "infectious_disease",
        "Acute febrile illness",
        "R50",
        "moderate",
    ),
    (
        ("diarrhea", "loose", "vomiting"),
        "infectious_disease",
        "Acute gastroenteritis",
        "A09",
        "moderate",
    ),
    (
        ("polyuria", "polydipsia", "weight loss", "diabetes"),
        "internal_medicine",
        "Type 2 diabetes mellitus",
        "E11",
        "moderate",
    ),
    (
        ("headache", "bp", "hypertension", "dizziness"),
        "internal_medicine",
        "Hypertension",
        "I10",
        "low",
    ),
    (
        ("joint pain", "swelling", "stiffness"),
        "internal_medicine",
        "Inflammatory arthritis",
        "M06",
        "low",
    ),
    (("fatigue", "tired", "weakness", "pallor"), "primary_care", "Anaemia", "D64", "low"),
    (
        ("cough", "weight loss", "night sweat"),
        "infectious_disease",
        "Pulmonary tuberculosis",
        "A15",
        "moderate",
    ),
]


async def run(state: CaseState, ctx: ReasoningContext) -> None:
    await ctx.emit("agent_start", {"agent": AGENT, "label": "Hypothesis Panel (4 specialists)"})
    summary = summarize_snapshot(state.patient_graph_snapshot)
    blob = text_blob(state.presenting_complaint, summary, state.intake_questions)

    produced: list[Hypothesis] = []
    if ctx.llm_available():
        results = await asyncio.gather(
            *[_run_specialist(state, ctx, key, name, summary) for key, name in SPECIALISTS.items()]
        )
        produced = [h for sub in results for h in sub]
    # A provider that holds a key is not a provider that answers. This used to fall back only
    # when no key was configured, so a live outage — the likeliest failure in production — left
    # the differential *empty*: four specialists returned nothing, the deterministic panel that
    # exists for exactly this was skipped, and the case was not marked degraded. The clinician
    # saw no hypotheses and no sign that anything had failed.
    if not produced:
        produced = _fallback(blob, state)
        state.degraded = True

    state.hypothesis_set.extend(_merge(produced))
    for key, name in SPECIALISTS.items():
        await ctx.emit(
            "specialist",
            {
                "agent": key,
                "label": name,
                "hypotheses": [
                    {"diagnosis_name": h.diagnosis_name, "probability_band": h.probability_band}
                    for h in state.hypothesis_set
                    if h.source_agent == key
                ],
            },
        )
    state.add_trace(AGENT, f"Generated {len(state.hypothesis_set)} hypotheses", {})
    await ctx.emit(
        "hypotheses",
        {"agent": AGENT, "hypotheses": [_hyp_summary(h) for h in state.leading_hypotheses(8)]},
    )
    await ctx.emit("agent_complete", {"agent": AGENT})


async def _run_specialist(
    state: CaseState, ctx: ReasoningContext, key: str, name: str, summary: str
) -> list[Hypothesis]:
    result = await call_llm(
        ctx,
        # .replace (not .format): the prompt embeds a literal JSON schema with { } braces.
        HYPOTHESIS_PANEL.replace("{specialty}", name),
        f"Presenting complaint: {state.presenting_complaint}\n\nPatient record:\n{summary}\n\n"
        f"Intake answers: {[{'q': q.text, 'a': q.answer} for q in state.intake_questions]}",
    )
    if not result:
        return []
    out: list[Hypothesis] = []
    # ``objects`` rather than the raw list: the four specialists run under ``asyncio.gather``, so
    # one of them reading a bare-string hypothesis list takes down the other three's work with it
    # — and with ``produced`` non-empty from nobody, the deterministic panel never runs either.
    for h in objects(result.get("hypotheses")):
        name_ = as_text(h.get("diagnosis_name"))
        if not name_:
            continue
        out.append(
            Hypothesis(
                diagnosis_name=name_,
                icd_code=h.get("icd_code") if isinstance(h.get("icd_code"), str) else None,
                probability_band=norm_band(h.get("probability_band")),
                evidence_for=_parse_evidence(h.get("evidence_for"), True),
                evidence_against=_parse_evidence(h.get("evidence_against"), False),
                rationale=as_text(h.get("rationale")) or None,
                source_agent=key,
            )
        )
    return out


def _parse_evidence(items: Any, supports: bool) -> list[Evidence]:
    """Parse evidence items defensively — tolerate a model returning non-string ``text``.

    A weak model may shape an evidence item as ``{"evidence": ..., "probability": ...}`` instead
    of ``{"text": ...}``; ``as_text`` flattens it so the item is shown rather than dropped or
    crashing the UI. The ``source_ref`` is only kept when it is a plain string.
    """
    out: list[Evidence] = []
    for e in items or []:
        if isinstance(e, dict):
            text = as_text(e.get("text") or e.get("evidence") or e.get("finding"))
            ref = e.get("source_ref") if isinstance(e.get("source_ref"), str) else None
        else:
            text, ref = as_text(e), None
        if text:
            out.append(Evidence(text, supports, source_ref=ref))
    return out


def _fallback(blob: str, state: CaseState) -> list[Hypothesis]:
    haystack = blob.lower()
    out: list[Hypothesis] = []
    for triggers, specialty, dx, icd, band in _FALLBACK_MAP:
        if sum(1 for t in triggers if t in haystack) >= 1:
            out.append(
                Hypothesis(
                    diagnosis_name=dx,
                    icd_code=icd,
                    probability_band=band,
                    evidence_for=[
                        Evidence(
                            f"Presentation includes features associated with {dx.lower()}.",
                            True,
                            source_ref="presenting_complaint",
                        )
                    ],
                    evidence_against=[
                        Evidence(
                            "Confirmatory testing not yet available; consider mimics.",
                            False,
                            source="clinical_knowledge",
                        )
                    ],
                    rationale="Pattern-matched from complaint and record (degraded mode).",
                    source_agent=specialty,
                )
            )
    # Surface known active conditions as continuing problems.
    for cond in active_condition_names(state.patient_graph_snapshot):
        out.append(
            Hypothesis(
                diagnosis_name=f"Known: {cond}",
                probability_band="moderate",
                evidence_for=[Evidence(f"{cond} documented in record.", True, "patient_data")],
                rationale="Existing diagnosis on the longitudinal record.",
                source_agent="primary_care",
            )
        )
    if not out:
        out.append(
            Hypothesis(
                diagnosis_name="Undifferentiated presentation — further assessment needed",
                probability_band="insufficient_data",
                rationale="Insufficient structured data to form a differential offline.",
                source_agent="primary_care",
            )
        )
    return out


def _merge(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    """Union + dedup by diagnosis name, keeping the most confident band and merging evidence."""
    order = ["high", "moderate", "low", "very_low", "insufficient_data"]
    by_name: dict[str, Hypothesis] = {}
    for h in hypotheses:
        key = h.diagnosis_name.strip().lower()
        if key not in by_name:
            by_name[key] = h
            continue
        kept = by_name[key]
        if order.index(h.probability_band) < order.index(kept.probability_band):
            kept.probability_band = h.probability_band
        kept.evidence_for.extend(h.evidence_for)
        kept.evidence_against.extend(h.evidence_against)
    return list(by_name.values())


def _hyp_summary(h: Hypothesis) -> dict:
    return {
        "diagnosis_name": h.diagnosis_name,
        "icd_code": h.icd_code,
        "probability_band": h.probability_band,
        "source_agent": h.source_agent,
        "cant_miss_flag": h.cant_miss_flag,
        "evidence_for": [e.text for e in h.evidence_for],
        "evidence_against": [e.text for e in h.evidence_against],
    }
