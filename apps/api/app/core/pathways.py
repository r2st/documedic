"""Deterministic, offline clinical-pathway definitions.

A "pathway" is a staged view of guideline-supported care for one condition: diagnostic
workup, first-line therapy, escalation criteria, monitoring, and can't-miss red flags. This
is reference content, not a reasoning-engine output -- it never goes through the Verifier
Agent, in the same way ``GET /guidelines/search`` doesn't, because it is a direct lookup
against curated/guideline data rather than an LLM-generated clinical claim about a specific
patient (see app/routers/pathways.py).

Every stage that is grounded in the real ICMR STW corpus (``data/guidelines/icmr_stw.json``)
carries the exact ``section_id``(s) it corresponds to, so the API layer can attach real
citations pulled from ``guideline_chunks`` (never fabricated). A pathway whose condition has
no matching corpus document is marked ``source="curated"`` and carries no section ids --
callers must not present curated-only content as if it were guideline-cited.

Per CLAUDE.md rule #4 (no certainty language): every item is phrased as "guidelines support
considering", "consider", or similar -- never an imperative ("give X", "start Y").
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PathwayStage:
    key: str
    title: str
    items: tuple[str, ...]
    guideline_section_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClinicalPathway:
    condition_name: str
    source: str  # "icmr" | "curated"
    stages: tuple[PathwayStage, ...]


_PATHWAYS: dict[str, ClinicalPathway] = {
    "hypertension": ClinicalPathway(
        condition_name="Hypertension",
        source="icmr",
        stages=(
            PathwayStage(
                key="diagnostic_workup",
                title="Diagnosis and staging",
                items=(
                    "Guidelines support confirming the diagnosis on the average of two or "
                    "more readings on two or more separate occasions before labelling a "
                    "patient hypertensive.",
                    "Consider baseline workup for target-organ damage and cardiovascular "
                    "risk before starting therapy: renal function, electrolytes, fasting "
                    "glucose/HbA1c, lipid profile, ECG, urinalysis.",
                ),
                guideline_section_ids=("ICMR-HTN-DX",),
            ),
            PathwayStage(
                key="first_line_therapy",
                title="First-line pharmacotherapy",
                items=(
                    "Guidelines support considering a calcium-channel blocker, an ACE "
                    "inhibitor/ARB, or a thiazide-like diuretic as initial monotherapy. "
                    "In adults under 55, an ACE inhibitor/ARB is often preferred; a CCB or "
                    "thiazide is reasonable to consider in older adults.",
                    "Lifestyle measures (sodium restriction, weight management, physical "
                    "activity) are supportive alongside pharmacotherapy, not a substitute "
                    "for it once drug therapy is indicated.",
                ),
                guideline_section_ids=("ICMR-HTN-MGMT",),
            ),
            PathwayStage(
                key="escalation",
                title="Escalation criteria",
                items=(
                    "If BP remains above target on a single first-line agent at an "
                    "optimised dose, evidence supports considering a second agent from a "
                    "different first-line class rather than titrating one agent "
                    "indefinitely.",
                    "Dual RAAS blockade (ACE inhibitor + ARB together) is not supported by "
                    "guidelines -- consider combining across different first-line classes "
                    "instead.",
                ),
                guideline_section_ids=("ICMR-HTN-MGMT",),
            ),
            PathwayStage(
                key="monitoring",
                title="Monitoring",
                items=(
                    "Consider rechecking BP within 2-4 weeks of starting or changing "
                    "therapy.",
                    "Consider monitoring renal function and serum potassium after starting "
                    "or up-titrating an ACE inhibitor, ARB, or potassium-sparing diuretic.",
                ),
            ),
            PathwayStage(
                key="cant_miss",
                title="Red flags / urgent referral",
                items=(
                    "Guidelines support urgent referral for accelerated hypertension "
                    "(>=180/120 mmHg with target-organ damage), suspected secondary "
                    "hypertension in a young patient, or resistant hypertension on three "
                    "agents including a diuretic.",
                ),
                guideline_section_ids=("ICMR-HTN-REFER",),
            ),
        ),
    ),
    "type 2 diabetes mellitus": ClinicalPathway(
        condition_name="Type 2 Diabetes Mellitus",
        source="icmr",
        stages=(
            PathwayStage(
                key="diagnostic_workup",
                title="Diagnosis",
                items=(
                    "Guidelines support diagnosis with fasting plasma glucose >=126 mg/dL, "
                    "2-hour post-load glucose >=200 mg/dL, HbA1c >=6.5%, or a random "
                    "glucose >=200 mg/dL with classic symptoms.",
                    "Consider a repeat confirmatory test unless the patient is symptomatic "
                    "with unequivocal hyperglycaemia.",
                ),
                guideline_section_ids=("ICMR-T2DM-DX",),
            ),
            PathwayStage(
                key="first_line_therapy",
                title="Glycaemic management",
                items=(
                    "Metformin alongside lifestyle modification is the guideline-preferred "
                    "first-line pharmacotherapy, unless contraindicated (e.g. eGFR below "
                    "30).",
                    "When glycaemic targets are not met on metformin, guidelines support "
                    "considering a second agent such as a sulfonylurea, DPP-4 inhibitor, or "
                    "SGLT2 inhibitor, individualised to comorbidity.",
                ),
                guideline_section_ids=("ICMR-T2DM-MGMT",),
            ),
            PathwayStage(
                key="escalation",
                title="Escalation criteria",
                items=(
                    "If HbA1c remains above target despite two oral agents at optimised "
                    "doses, guidelines support considering a third agent or, depending on "
                    "clinical context, insulin initiation.",
                ),
                guideline_section_ids=("ICMR-T2DM-MGMT",),
            ),
            PathwayStage(
                key="monitoring",
                title="Complication screening and monitoring",
                items=(
                    "Guidelines support screening at least annually for retinopathy, "
                    "nephropathy (urine albumin and eGFR), and diabetic foot.",
                    "Blood-pressure and lipid control are supportive of reducing "
                    "cardiovascular risk in diabetes and are worth reviewing alongside "
                    "glycaemic control.",
                ),
                guideline_section_ids=("ICMR-T2DM-COMPL",),
            ),
            PathwayStage(
                key="cant_miss",
                title="Red flags / urgent referral",
                items=(
                    "Consider urgent assessment for suspected diabetic ketoacidosis or "
                    "hyperosmolar hyperglycaemic state (marked hyperglycaemia with "
                    "vomiting, altered consciousness, or ketosis).",
                ),
            ),
        ),
    ),
    "community-acquired pneumonia": ClinicalPathway(
        condition_name="Community-Acquired Pneumonia",
        source="icmr",
        stages=(
            PathwayStage(
                key="diagnostic_workup",
                title="Severity assessment",
                items=(
                    "Guidelines support assessing severity with CRB-65 (confusion, "
                    "respiratory rate >=30, BP <90/60, age >=65) and pulse oximetry to "
                    "guide the need for supplemental oxygen.",
                    "Low-severity disease can often be considered for community "
                    "management; a higher score supports hospital assessment.",
                ),
                guideline_section_ids=("ICMR-CAP-ASSESS",),
            ),
            PathwayStage(
                key="first_line_therapy",
                title="Empirical antibiotic management",
                items=(
                    "For low-severity disease, guidelines support considering oral "
                    "amoxicillin (or amoxicillin-clavulanate) as first-line, with a "
                    "macrolide (e.g. azithromycin) or doxycycline as an alternative in "
                    "penicillin allergy.",
                    "Consider reassessment at 48-72 hours for clinical response.",
                ),
                guideline_section_ids=("ICMR-CAP-MGMT",),
            ),
            PathwayStage(
                key="escalation",
                title="Escalation criteria",
                items=(
                    "Higher-severity disease supports considering combination antibiotic "
                    "therapy and inpatient care rather than oral monotherapy.",
                ),
                guideline_section_ids=("ICMR-CAP-MGMT",),
            ),
            PathwayStage(
                key="monitoring",
                title="Monitoring",
                items=(
                    "Consider reassessing symptoms, oxygen saturation, and inflammatory "
                    "markers if response at 48-72 hours is inadequate.",
                ),
            ),
            PathwayStage(
                key="cant_miss",
                title="Red flags / urgent referral",
                items=(
                    "Consider urgent hospital assessment for hypoxia, hypotension, "
                    "confusion, or a high CRB-65 score.",
                ),
                guideline_section_ids=("ICMR-CAP-ASSESS",),
            ),
        ),
    ),
    "dyslipidemia": ClinicalPathway(
        condition_name="Dyslipidemia",
        source="curated",
        stages=(
            PathwayStage(
                key="diagnostic_workup",
                title="Diagnosis and risk stratification",
                items=(
                    "Consider a fasting or non-fasting lipid profile and an estimate of "
                    "overall cardiovascular risk before deciding on a treatment target.",
                ),
            ),
            PathwayStage(
                key="first_line_therapy",
                title="First-line pharmacotherapy",
                items=(
                    "Guidelines support considering a statin as first-line pharmacotherapy "
                    "when lifestyle measures alone do not reach target, individualised to "
                    "cardiovascular risk.",
                ),
            ),
            PathwayStage(
                key="escalation",
                title="Escalation criteria",
                items=(
                    "If LDL remains above target on a maximally tolerated statin dose, "
                    "consider adding ezetimibe or another cholesterol absorption inhibitor "
                    "before considering further intensification.",
                ),
            ),
            PathwayStage(
                key="monitoring",
                title="Monitoring",
                items=(
                    "Consider a follow-up lipid panel 4-12 weeks after starting or "
                    "changing therapy, then periodically thereafter.",
                    "Consider baseline and as-needed liver function and creatine kinase "
                    "monitoring if the patient reports muscle symptoms.",
                ),
            ),
        ),
    ),
}


def get_pathway(condition_name: str) -> ClinicalPathway | None:
    return _PATHWAYS.get((condition_name or "").strip().lower())


def available_conditions() -> list[str]:
    return sorted(p.condition_name for p in _PATHWAYS.values())
