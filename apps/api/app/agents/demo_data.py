"""Simulated clinical responses for DEMO MODE (final LLM safety net).

When neither OpenAI nor Anthropic can be reached (no key configured, offline, or every
provider call failed) AND ``settings.llm_demo_fallback`` is enabled, the LLM client serves the
structured payloads in this module instead of raising. The goal is that the product is always
demonstrable — the Reasoning Theatre, document extraction and clinical suggestions all render
realistic, internally-consistent sample data — without a single API key.

Everything produced here is clearly marked ``[DEMO MODE]`` in user-visible text, carries a
``_demo: true`` marker so callers/UI can surface a banner, and is medically plausible but
illustrative only. The deterministic safety floor (allergy/contraindication hard blocks,
can't-miss persistence, conservative tier escalation) is unaffected — it never depends on the
LLM and always runs on top of this data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEMO_TAG = "[DEMO MODE]"

# Canonical specialty labels used by the Hypothesis Panel (see hypothesis_panel.SPECIALISTS).
_SPECIALTIES = (
    "General Internal Medicine",
    "Cardiology",
    "Infectious Disease",
    "Primary Care",
)


@dataclass
class DemoScenario:
    """A coherent illustrative case: every agent's demo output is drawn from one scenario."""

    key: str
    triggers: tuple[str, ...]
    leading: str
    intake: list[dict[str, Any]] = field(default_factory=list)
    hypotheses_by_specialty: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    cant_miss: list[dict[str, Any]] = field(default_factory=list)
    investigations: list[dict[str, Any]] = field(default_factory=list)
    management: list[str] = field(default_factory=list)
    devils: dict[str, Any] = field(default_factory=dict)


def _ev(text: str, ref: str | None = None) -> dict[str, Any]:
    return {"text": text, "source_ref": ref}


# --------------------------------------------------------------------------- scenarios

_CARDIAC = DemoScenario(
    key="cardiac",
    triggers=("chest pain", "chest discomfort", "angina", "left arm", "exertional", "palpitation"),
    leading="Stable angina (suspected)",
    intake=[
        {
            "text": "Is the chest pain brought on by exertion and relieved by rest?",
            "question_type": "clarifying",
            "rationale": f"{DEMO_TAG} Exertional pattern strongly shifts the cardiac differential.",
            "info_gain_score": 0.85,
        },
        {
            "text": "Any associated breathlessness, sweating, or radiation to the jaw or left arm?",
            "question_type": "red_flag",
            "rationale": f"{DEMO_TAG} Screens for acute coronary syndrome red flags.",
            "info_gain_score": 0.9,
        },
    ],
    hypotheses_by_specialty={
        "Cardiology": [
            {
                "diagnosis_name": "Stable angina (suspected)",
                "icd_code": "I20.9",
                "probability_band": "moderate",
                "evidence_for": [
                    _ev(
                        "Exertional chest discomfort consistent with myocardial ischaemia.",
                        "presenting_complaint",
                    ),
                    _ev("Cardiovascular risk factors documented in the record.", "patient_data"),
                ],
                "evidence_against": [
                    _ev("No resting ECG or troponin available yet to confirm ischaemia."),
                ],
                "rationale": f"{DEMO_TAG} Findings are consistent with a demand-ischaemia pattern; confirmatory testing is required.",
            },
            {
                "diagnosis_name": "Acute coronary syndrome (to exclude)",
                "icd_code": "I24.9",
                "probability_band": "low",
                "evidence_for": [
                    _ev(
                        "Chest pain with potential cardiac features warrants active exclusion.",
                        "presenting_complaint",
                    ),
                ],
                "evidence_against": [_ev("Haemodynamically stable; symptoms not clearly at rest.")],
                "rationale": f"{DEMO_TAG} Must be actively excluded before reassurance.",
            },
        ],
        "General Internal Medicine": [
            {
                "diagnosis_name": "Gastro-oesophageal reflux disease",
                "icd_code": "K21.9",
                "probability_band": "low",
                "evidence_for": [
                    _ev("Retrosternal discomfort can mimic cardiac pain.", "presenting_complaint")
                ],
                "evidence_against": [_ev("Exertional trigger is atypical for reflux.")],
                "rationale": f"{DEMO_TAG} A common non-cardiac mimic worth considering.",
            }
        ],
        "Primary Care": [
            {
                "diagnosis_name": "Musculoskeletal chest wall pain",
                "icd_code": "M79.1",
                "probability_band": "low",
                "evidence_for": [_ev("Localised, reproducible chest wall pain is often benign.")],
                "evidence_against": [_ev("Cannot rely on this until cardiac causes are excluded.")],
                "rationale": f"{DEMO_TAG} Diagnosis of exclusion only.",
            }
        ],
        "Infectious Disease": [],
    },
    cant_miss=[
        {
            "diagnosis_name": "Acute myocardial infarction",
            "icd_code": "I21.9",
            "why_dangerous": f"{DEMO_TAG} Time-critical; missing it risks avoidable death.",
            "probability_band": "low",
            "evidence_for": [
                _ev("Chest pain mandates ruling out an evolving infarct.", "presenting_complaint")
            ],
        },
        {
            "diagnosis_name": "Pulmonary embolism",
            "icd_code": "I26.9",
            "why_dangerous": f"{DEMO_TAG} Can present as chest pain; high mortality if untreated.",
            "probability_band": "very_low",
            "evidence_for": [_ev("Chest pain with possible breathlessness keeps PE on the list.")],
        },
    ],
    investigations=[
        {
            "name": "12-lead ECG",
            "rationale": f"{DEMO_TAG} Single most discriminating immediate test for ischaemia.",
            "expected_information_gain": "high",
            "cost_estimate": "₹100–300",
            "availability_tier": "phc",
        },
        {
            "name": "High-sensitivity troponin",
            "rationale": f"{DEMO_TAG} Rules in/out myocardial injury when ECG is equivocal.",
            "expected_information_gain": "high",
            "cost_estimate": "₹500–900",
            "availability_tier": "chc",
        },
    ],
    management=[
        "Guidelines support considering an immediate 12-lead ECG and risk stratification for any patient with possible cardiac chest pain.",
        "Evidence suggests offering antiplatelet therapy only after acute coronary syndrome has been appropriately assessed and contraindications excluded.",
    ],
    devils={
        "disconfirming_evidence": [
            f"{DEMO_TAG} No objective ischaemia has been demonstrated on ECG or biomarkers.",
            "Symptom description may equally fit a non-cardiac cause.",
        ],
        "alternative_explanations": [
            "Gastro-oesophageal reflux disease",
            "Musculoskeletal chest wall pain",
            "Anxiety-related chest discomfort",
        ],
        "base_rate_caveat": f"{DEMO_TAG} In primary care most chest pain is non-cardiac, but can't-miss causes must still be excluded first.",
        "summary": f"{DEMO_TAG} Findings are not specific for angina; the evidence remains compatible with mimics until a discriminating test is performed.",
    },
)

_RESPIRATORY = DemoScenario(
    key="respiratory",
    triggers=("fever", "cough", "sputum", "breathless", "dyspnea", "cold", "sore throat"),
    leading="Community-acquired pneumonia (suspected)",
    intake=[
        {
            "text": "How many days has the fever and cough been present, and is it worsening?",
            "question_type": "history",
            "rationale": f"{DEMO_TAG} Onset and trajectory change urgency and the differential.",
            "info_gain_score": 0.8,
        },
        {
            "text": "Any breathlessness at rest, chest pain, or confusion?",
            "question_type": "red_flag",
            "rationale": f"{DEMO_TAG} Screens for severity markers and can't-miss complications.",
            "info_gain_score": 0.9,
        },
    ],
    hypotheses_by_specialty={
        "Infectious Disease": [
            {
                "diagnosis_name": "Community-acquired pneumonia (suspected)",
                "icd_code": "J18.9",
                "probability_band": "moderate",
                "evidence_for": [
                    _ev(
                        "Fever with productive cough is consistent with a lower respiratory tract infection.",
                        "presenting_complaint",
                    ),
                ],
                "evidence_against": [
                    _ev("Chest imaging not yet available to confirm consolidation.")
                ],
                "rationale": f"{DEMO_TAG} Pattern is consistent with CAP pending examination and imaging.",
            },
            {
                "diagnosis_name": "Pulmonary tuberculosis (to consider)",
                "icd_code": "A15.0",
                "probability_band": "low",
                "evidence_for": [
                    _ev(
                        "In the Indian setting, prolonged cough warrants TB consideration.",
                        "presenting_complaint",
                    )
                ],
                "evidence_against": [_ev("No documented weight loss or night sweats yet.")],
                "rationale": f"{DEMO_TAG} Endemic context keeps TB on the differential.",
            },
        ],
        "General Internal Medicine": [
            {
                "diagnosis_name": "Acute viral upper respiratory infection",
                "icd_code": "J06.9",
                "probability_band": "moderate",
                "evidence_for": [
                    _ev(
                        "Self-limiting febrile respiratory illness is common.",
                        "presenting_complaint",
                    )
                ],
                "evidence_against": [
                    _ev("Does not explain focal or severe lower-tract features if present.")
                ],
                "rationale": f"{DEMO_TAG} A frequent benign cause that often needs only supportive care.",
            }
        ],
        "Primary Care": [
            {
                "diagnosis_name": "Acute bronchitis",
                "icd_code": "J20.9",
                "probability_band": "low",
                "evidence_for": [_ev("Cough with low-grade fever can reflect bronchitis.")],
                "evidence_against": [
                    _ev("Antibiotics are usually not indicated without bacterial features.")
                ],
                "rationale": f"{DEMO_TAG} Consider supportive management if severity is low.",
            }
        ],
        "Cardiology": [],
    },
    cant_miss=[
        {
            "diagnosis_name": "Sepsis",
            "icd_code": "A41.9",
            "why_dangerous": f"{DEMO_TAG} Rapidly progressive; early recognition is life-saving.",
            "probability_band": "low",
            "evidence_for": [
                _ev(
                    "Febrile illness mandates screening for systemic deterioration.",
                    "presenting_complaint",
                )
            ],
        },
        {
            "diagnosis_name": "Pulmonary tuberculosis",
            "icd_code": "A15.0",
            "why_dangerous": f"{DEMO_TAG} Public-health critical and commonly missed in early presentations.",
            "probability_band": "low",
            "evidence_for": [
                _ev("Persistent cough in an endemic setting must keep TB on the list.")
            ],
        },
    ],
    investigations=[
        {
            "name": "Chest X-ray (PA view)",
            "rationale": f"{DEMO_TAG} Most discriminating test to confirm or exclude consolidation.",
            "expected_information_gain": "high",
            "cost_estimate": "₹200–500",
            "availability_tier": "chc",
        },
        {
            "name": "Sputum for AFB / CBNAAT",
            "rationale": f"{DEMO_TAG} Discriminates tuberculosis where cough is prolonged.",
            "expected_information_gain": "moderate",
            "cost_estimate": "₹0 (public programme)",
            "availability_tier": "district_hospital",
        },
    ],
    management=[
        "Guidelines support considering empirical antibiotics for community-acquired pneumonia only after assessing severity and local resistance patterns.",
        "Evidence suggests prioritising supportive care and safety-netting advice for self-limiting viral respiratory illness.",
    ],
    devils={
        "disconfirming_evidence": [
            f"{DEMO_TAG} No chest imaging has confirmed consolidation.",
            "Symptoms overlap substantially with a self-limiting viral illness.",
        ],
        "alternative_explanations": [
            "Acute viral upper respiratory infection",
            "Acute bronchitis",
            "Early pulmonary tuberculosis",
        ],
        "base_rate_caveat": f"{DEMO_TAG} Most acute cough/fever is viral and self-limiting; reserve antibiotics for clear bacterial features.",
        "summary": f"{DEMO_TAG} Findings are not specific for pneumonia; supportive care may suffice unless imaging or severity markers indicate otherwise.",
    },
)

_DEFAULT = DemoScenario(
    key="general",
    triggers=(),
    leading="Undifferentiated presentation (further assessment needed)",
    intake=[
        {
            "text": "How long has the complaint been present, and is it getting better or worse?",
            "question_type": "history",
            "rationale": f"{DEMO_TAG} Onset and trajectory most change the differential.",
            "info_gain_score": 0.75,
        },
        {
            "text": "Any fever, breathlessness, chest pain, or altered consciousness?",
            "question_type": "red_flag",
            "rationale": f"{DEMO_TAG} Screens for the common can't-miss red flags.",
            "info_gain_score": 0.85,
        },
    ],
    hypotheses_by_specialty={
        "General Internal Medicine": [
            {
                "diagnosis_name": "Non-specific febrile / viral illness",
                "icd_code": "R50.9",
                "probability_band": "moderate",
                "evidence_for": [
                    _ev(
                        "Presentation is most consistent with a common self-limiting illness.",
                        "presenting_complaint",
                    )
                ],
                "evidence_against": [
                    _ev("Insufficient structured data to confidently localise a cause.")
                ],
                "rationale": f"{DEMO_TAG} A frequent, usually benign presentation pending further history.",
            }
        ],
        "Primary Care": [
            {
                "diagnosis_name": "Symptom requiring further characterisation",
                "icd_code": None,
                "probability_band": "low",
                "evidence_for": [
                    _ev("Additional history and examination are needed to refine the differential.")
                ],
                "evidence_against": [_ev("No discriminating features documented yet.")],
                "rationale": f"{DEMO_TAG} Gather more information before narrowing.",
            }
        ],
        "Cardiology": [],
        "Infectious Disease": [],
    },
    cant_miss=[
        {
            "diagnosis_name": "Sepsis",
            "icd_code": "A41.9",
            "why_dangerous": f"{DEMO_TAG} Any unwell patient warrants screening for systemic deterioration.",
            "probability_band": "very_low",
            "evidence_for": [_ev("Maintain vigilance for red-flag features of serious illness.")],
        }
    ],
    investigations=[
        {
            "name": "Focused history and examination",
            "rationale": f"{DEMO_TAG} Highest-yield next step to localise the problem.",
            "expected_information_gain": "high",
            "cost_estimate": "₹0",
            "availability_tier": "phc",
        },
        {
            "name": "Basic bedside vitals and point-of-care tests",
            "rationale": f"{DEMO_TAG} Cheap, fast triage of severity.",
            "expected_information_gain": "moderate",
            "cost_estimate": "₹0–200",
            "availability_tier": "phc",
        },
    ],
    management=[
        "Guidelines support considering symptomatic supportive care with clear safety-netting advice while the diagnosis is refined.",
        "Evidence suggests arranging review or escalation if red-flag features develop.",
    ],
    devils={
        "disconfirming_evidence": [
            f"{DEMO_TAG} The leading impression rests on limited data.",
            "Several alternative explanations remain open.",
        ],
        "alternative_explanations": [
            "A self-limiting benign illness",
            "An early presentation of a serious condition",
        ],
        "base_rate_caveat": f"{DEMO_TAG} Common things are common, but can't-miss diagnoses must still be actively excluded.",
        "summary": f"{DEMO_TAG} The impression is provisional; a discriminating history, examination or test is needed before narrowing.",
    },
)

_SCENARIOS = (_CARDIAC, _RESPIRATORY, _DEFAULT)


def select_scenario(text: str) -> DemoScenario:
    """Pick the most relevant illustrative scenario from the case text (keyword match)."""
    haystack = (text or "").lower()
    best: tuple[int, DemoScenario] | None = None
    for scn in _SCENARIOS:
        hits = sum(1 for t in scn.triggers if t in haystack)
        if hits and (best is None or hits > best[0]):
            best = (hits, scn)
    return best[1] if best else _DEFAULT


# --------------------------------------------------------------------------- per-agent payloads


def _detect_specialty(system: str) -> str | None:
    for name in _SPECIALTIES:
        if name in system:
            return name
    return None


def _leading_from_user(user: str, fallback: str) -> str:
    for line in (user or "").splitlines():
        if line.lower().startswith("leading hypothesis:"):
            value = line.split(":", 1)[1].strip()
            if value:
                return value
    return fallback


def _triage_payload(scn: DemoScenario) -> dict[str, Any]:
    return {"intake_complete": False, "info_gain_score": 0.6, "questions": list(scn.intake)}


def _hypothesis_payload(scn: DemoScenario, system: str) -> dict[str, Any]:
    specialty = _detect_specialty(system)
    if specialty is not None:
        return {"hypotheses": list(scn.hypotheses_by_specialty.get(specialty, []))}
    # No specialty detected: return the union so callers still get a sensible differential.
    merged = [h for hs in scn.hypotheses_by_specialty.values() for h in hs]
    return {"hypotheses": merged}


def _cant_miss_payload(scn: DemoScenario) -> dict[str, Any]:
    return {"cant_miss": list(scn.cant_miss)}


def _devils_payload(scn: DemoScenario, user: str) -> dict[str, Any]:
    out = dict(scn.devils)
    out["leading_hypothesis"] = _leading_from_user(user, scn.leading)
    return out


def _investigation_payload(scn: DemoScenario) -> dict[str, Any]:
    return {"investigations": list(scn.investigations)}


def _guideline_payload(scn: DemoScenario) -> dict[str, Any]:
    return {
        "options": [
            {"text": text, "citation_section_ids": [], "sufficient_support": True}
            for text in scn.management
        ],
        "insufficient_support": False,
    }


def _verifier_payload(scn: DemoScenario) -> dict[str, Any]:
    return {
        "status": "agree",
        # Suggestive by default; the deterministic safety floor escalates to flag-for-review
        # whenever a can't-miss flag, hard block or degraded marker is present.
        "autonomy_tier": "suggestive",
        "verdicts": [
            {
                "target": "case",
                "status": "agree",
                "rationale": f"{DEMO_TAG} Simulated independent re-check; output is illustrative only.",
                "caveats": [
                    f"{DEMO_TAG} Generated without a live LLM — treat as a demonstration, not clinical advice."
                ],
            }
        ],
        "case_caveats": [
            f"{DEMO_TAG} Running on simulated reasoning because no AI provider is configured or reachable."
        ],
    }


def simulated_response(system: str, user: str) -> dict[str, Any]:
    """Return a simulated agent JSON payload selected from the system prompt + case text.

    The shape matches exactly what each agent parser expects (see ``app.agents.prompts``), so the
    agents produce realistic Reasoning-Theatre output. Every payload carries ``_demo: true``.
    """
    scn = select_scenario(f"{system}\n{user}")

    if "triage clinician" in system:
        payload = _triage_payload(scn)
    elif "multidisciplinary panel" in system:
        payload = _hypothesis_payload(scn, system)
    elif "can't-miss safety sentinel" in system:
        payload = _cant_miss_payload(scn)
    elif "devil's-advocate" in system:
        payload = _devils_payload(scn, user)
    elif "investigation strategist" in system:
        payload = _investigation_payload(scn)
    elif "guideline-grounded management" in system:
        payload = _guideline_payload(scn)
    elif "INDEPENDENT verifier" in system:
        payload = _verifier_payload(scn)
    else:
        payload = {}

    payload["_demo"] = True
    return payload


# --------------------------------------------------------------------------- extraction


def extraction_payload() -> dict[str, Any]:
    """A simulated document-extraction result (sample Indian primary-care prescription)."""
    return {
        "_demo": True,
        "document_type": "prescription",
        "entities": [
            {
                "entity_type": "medication",
                "fields": {
                    "brand_name_raw": f"Telma 40 {DEMO_TAG}",
                    "dose": "40",
                    "dose_unit": "mg",
                    "frequency": "OD",
                    "route": "oral",
                    "event_type": "continue",
                },
                "confidence": {"brand_name_raw": 0.6, "dose": 0.6, "frequency": 0.6},
            },
            {
                "entity_type": "medication",
                "fields": {
                    "brand_name_raw": f"Glycomet 500 {DEMO_TAG}",
                    "dose": "500",
                    "dose_unit": "mg",
                    "frequency": "BD",
                    "route": "oral",
                    "event_type": "continue",
                },
                "confidence": {"brand_name_raw": 0.6, "dose": 0.6, "frequency": 0.6},
            },
            {
                "entity_type": "lab_result",
                "fields": {
                    "marker_name": "HbA1c",
                    "value_numeric": 7.8,
                    "unit": "%",
                    "reference_range_low": 4.0,
                    "reference_range_high": 5.6,
                },
                "confidence": {"marker_name": 0.6, "value_numeric": 0.6},
            },
            {
                "entity_type": "condition",
                "fields": {"condition_name": "Type 2 diabetes mellitus", "status": "active"},
                "confidence": {"condition_name": 0.6},
            },
        ],
    }
