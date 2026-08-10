"""Agent tools that operate on the in-memory patient-graph snapshot (architecture §3.5).

All tools here are deterministic and offline-capable: they read the snapshot dict that the
ReasoningService assembles once at session start (medications, labs, conditions, allergies,
derived markers). Keeping them pure makes the agents testable without a database session.
"""

from __future__ import annotations

from typing import Any

from app.agents.state import Hypothesis, Investigation


def summarize_snapshot(snapshot: dict[str, Any]) -> str:
    """Render a compact clinician-style summary of the patient graph for agent prompts."""
    parts: list[str] = []
    conditions = [c.get("condition_name") for c in snapshot.get("conditions", [])]
    if conditions:
        parts.append("Active/past conditions: " + ", ".join(filter(None, conditions)))
    meds = [
        f"{m.get('generic_name') or m.get('brand_name_raw')} {m.get('dose') or ''}".strip()
        for m in snapshot.get("medications", [])
        if m.get("is_current")
    ]
    if meds:
        parts.append("Current medications: " + ", ".join(meds))
    allergies = [a.get("allergen_name") for a in snapshot.get("allergies", [])]
    if allergies:
        parts.append("Allergies: " + ", ".join(filter(None, allergies)))
    abnormal = [
        f"{lab.get('marker_name')}={lab.get('value_numeric')}{lab.get('unit') or ''}"
        for lab in snapshot.get("lab_results", [])
        if lab.get("is_abnormal")
    ]
    if abnormal:
        parts.append("Abnormal labs: " + ", ".join(abnormal[:12]))
    markers = [
        f"{m.get('marker_name')}={m.get('value_numeric')}{m.get('unit') or ''}"
        for m in snapshot.get("derived_markers", [])
    ]
    if markers:
        parts.append("Derived markers: " + ", ".join(markers[:6]))
    return "\n".join(parts) if parts else "No structured history on file."


def get_lab_trend(snapshot: dict[str, Any], marker: str) -> list[dict[str, Any]]:
    """Return chronological values for a lab marker (most recent last)."""
    rows = [
        lab
        for lab in snapshot.get("lab_results", [])
        if (lab.get("marker_name") or "").lower() == marker.lower()
    ]
    rows.sort(key=lambda r: str(r.get("sample_date") or ""))
    return rows


def active_condition_names(snapshot: dict[str, Any]) -> list[str]:
    return [
        c.get("condition_name", "")
        for c in snapshot.get("conditions", [])
        if c.get("status") == "active"
    ]


def latest_marker(snapshot: dict[str, Any], marker: str) -> float | None:
    rows = snapshot.get("derived_markers", []) + snapshot.get("lab_results", [])
    candidates = [r for r in rows if (r.get("marker_name") or "").lower() == marker.lower()]
    if not candidates:
        return None
    try:
        return float(candidates[0].get("value_numeric"))
    except (TypeError, ValueError):
        return None


def compute_discriminating_test(hypotheses: list[Hypothesis]) -> list[Investigation]:
    """Heuristic 'most discriminating next test' per leading hypothesis.

    Deterministic mapping from diagnosis family to its highest-yield, locally-available test,
    framed with cost and Indian facility-tier availability (architecture §3.2 Agent 5).
    """
    table: dict[str, Investigation] = {
        "coronary": Investigation(
            "12-lead ECG + troponin",
            "Discriminates acute coronary syndrome from non-cardiac chest pain.",
            "high",
            "low (ECG) / moderate (troponin)",
            "chc",
        ),
        "pulmonary embolism": Investigation(
            "D-dimer (if low pre-test) or CT pulmonary angiogram",
            "Rules out / confirms PE; D-dimer is a cheap rule-out.",
            "high",
            "moderate",
            "district_hospital",
        ),
        "dengue": Investigation(
            "Complete blood count (platelets, haematocrit) + NS1/serology",
            "Tracks plasma-leak phase and confirms dengue.",
            "high",
            "low",
            "phc",
        ),
        "malaria": Investigation(
            "Peripheral smear + rapid antigen test",
            "Confirms species and parasite density.",
            "high",
            "low",
            "phc",
        ),
        "ketoacidosis": Investigation(
            "Capillary blood glucose, blood ketones, venous blood gas",
            "Confirms DKA and grades severity.",
            "high",
            "low",
            "chc",
        ),
        "sepsis": Investigation(
            "Lactate + blood cultures + CBC",
            "Stratifies sepsis severity and guides source control.",
            "high",
            "moderate",
            "chc",
        ),
        "stroke": Investigation(
            "Non-contrast CT head",
            "Distinguishes ischaemic from haemorrhagic stroke before thrombolysis.",
            "high",
            "moderate",
            "district_hospital",
        ),
    }
    out: list[Investigation] = []
    seen: set[str] = set()
    for h in hypotheses:
        name = h.diagnosis_name.lower()
        for key, inv in table.items():
            if key in name and inv.name not in seen:
                out.append(inv)
                seen.add(inv.name)
    if not out:
        out.append(
            Investigation(
                "Focused history, examination and basic bedside vitals",
                "Reassess to discriminate the leading hypotheses before targeted testing.",
                "moderate",
                "low",
                "phc",
            )
        )
    return out
