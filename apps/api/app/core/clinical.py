"""Deterministic clinical computations (no LLM, offline-safe).

These power DerivedMarker computation and renal-dosing safety checks. All formulas are
versioned for reproducibility per the audit-trail requirement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.lab_safety import creatinine_to_mg_dl


@dataclass(frozen=True)
class EgfrResult:
    value: float  # mL/min/1.73m^2
    formula_name: str
    formula_version: str
    inputs: dict


def age_from_dob(dob: date, on: date) -> int:
    years = on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
    return years


def ckd_epi_2021_egfr(*, creatinine_mg_dl: float, age_years: int, sex: str) -> EgfrResult:
    """CKD-EPI 2021 creatinine equation (race-free).

    eGFR = 142 * min(Scr/k, 1)^a * max(Scr/k, 1)^-1.200 * 0.9938^age * (1.012 if female).
    k = 0.7 (female) / 0.9 (male); a = -0.241 (female) / -0.302 (male).
    """
    if creatinine_mg_dl <= 0:
        raise ValueError("creatinine must be positive")
    if age_years <= 0:
        raise ValueError("age must be positive")

    is_female = sex.lower().startswith("f")
    k = 0.7 if is_female else 0.9
    a = -0.241 if is_female else -0.302
    scr_k = creatinine_mg_dl / k

    egfr = 142.0 * (min(scr_k, 1.0) ** a) * (max(scr_k, 1.0) ** -1.200) * (0.9938**age_years)
    if is_female:
        egfr *= 1.012

    return EgfrResult(
        value=round(egfr, 2),
        formula_name="CKD-EPI_2021",
        formula_version="2021",
        inputs={
            "creatinine_mg_dl": creatinine_mg_dl,
            "age_years": age_years,
            "sex": "female" if is_female else "male",
        },
    )


def egfr_reference_abnormal(egfr_value: float) -> bool:
    """eGFR < 90 mL/min/1.73m^2 is below the normal reference floor."""
    return egfr_value < 90.0


# Analytes whose names contain "creatinine" but which are not a serum creatinine. CKD-EPI takes a
# serum creatinine in mg/dL and nothing else: a urine creatinine is two orders of magnitude
# larger, a creatinine clearance is already a clearance in mL/min, and an albumin/creatinine ratio
# is a ratio. Feeding any of them to the equation produces a confident eGFR of about 0.4 and an
# unclearable renal alert on a patient whose kidneys were never measured.
_NOT_SERUM_CREATININE = (
    "urine",
    "urinary",
    "clearance",
    "crcl",
    "ratio",
    "kinase",
    "excretion",
    "spot",
    "24h",
    "24 h",
)

_MARKER_NAME_RE = re.compile(r"[^a-z0-9]+")


def is_serum_creatinine_marker(marker_name: str) -> bool:
    """True only for a *serum* creatinine, the one input CKD-EPI accepts.

    Substring-matching "creatinine" — which is what this did — also matched "Creatinine
    Clearance", "Urine Creatinine" and the "Creatinine Kinase" misspelling of creatine kinase,
    every one of which is a different analyte on a different scale. A lab panel carrying any of
    them wrote a fabricated eGFR into the chart and drove the renal hard block off it.
    """
    name = _MARKER_NAME_RE.sub(" ", (marker_name or "").lower()).strip()
    if any(term in name for term in _NOT_SERUM_CREATININE):
        return False
    return "creatinin" in name or bool({"creat", "scr"} & set(name.split()))


def serum_creatinine_mg_dl(
    marker_name: str, value: Decimal | float | None, unit: str | None
) -> float | None:
    """A lab row as a serum creatinine in mg/dL, or None if it is not one this can be sure of.

    The two ways a row fails to be usable — it is a different analyte, or it is in units that
    cannot be interpreted — are both answered with None, because the caller's response to each is
    the same and is the one this codebase applies everywhere else: skip, and let the renal check
    report that it had no eGFR to work with. A guess here is a number in the patient's chart.
    """
    if value is None or not is_serum_creatinine_marker(marker_name):
        return None
    return creatinine_to_mg_dl(float(value), unit)
