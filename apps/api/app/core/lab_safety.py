"""Deterministic critical/panic lab-value safety guard (P-XX). No LLM, no I/O.

A "can't-miss" backstop for lab interpretation: curated critical-value thresholds for common
markers, evaluated independently of whatever reference range (if any) the source document
supplied — OCR/vision extraction can miss or mis-transcribe a reference range entirely, so a
critical value must still be caught even when ``LabResult.is_abnormal`` was never set. Runs
fully offline and produces no certainty language (Critical Safety Rules #4 and #8): a flag here
is a prompt for the clinician to look, never a diagnosis or an instruction to act.

Panic values are the more severe tier nested inside critical (panic implies critical). Only
glucose and creatinine get unit-aware conversion (mg/dL <-> mmol/L / µmol/L) because Indian labs
occasionally report either; everything else assumes the near-universal unit noted in
``CriticalRange.unit`` and is skipped (never guessed) when the supplied unit looks incompatible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

LabSeverity = Literal["critical_low", "critical_high", "panic_low", "panic_high"]


@dataclass(frozen=True)
class CriticalRange:
    unit: str
    panic_low: float | None = None
    critical_low: float | None = None
    critical_high: float | None = None
    panic_high: float | None = None


@dataclass(frozen=True)
class CriticalLabFlag:
    marker_name: str
    canonical_marker: str
    value: float
    unit: str | None
    severity: LabSeverity
    summary: str
    details: dict


# Canonical marker key -> curated critical/panic thresholds. Sources: standard clinical
# laboratory "critical value" tables (e.g. CLSI/CAP-style panic-value lists), narrowed to the
# markers this product's extraction pipeline actually captures.
_RANGES: dict[str, CriticalRange] = {
    "potassium": CriticalRange(
        unit="mmol/l", panic_low=2.5, critical_low=3.0, critical_high=6.0, panic_high=6.5
    ),
    "sodium": CriticalRange(
        unit="mmol/l", panic_low=115, critical_low=120, critical_high=160, panic_high=165
    ),
    "glucose": CriticalRange(
        unit="mg/dl", panic_low=40, critical_low=54, critical_high=400, panic_high=500
    ),
    "hemoglobin": CriticalRange(unit="g/dl", panic_low=5.0, critical_low=7.0, critical_high=20.0),
    "platelets": CriticalRange(unit="10^3/ul", panic_low=10, critical_low=20, critical_high=1000),
    "creatinine": CriticalRange(unit="mg/dl", critical_high=4.0, panic_high=10.0),
    "inr": CriticalRange(unit="ratio", critical_high=5.0, panic_high=9.0),
    "wbc": CriticalRange(
        unit="10^3/ul", panic_low=1.0, critical_low=2.0, critical_high=30.0, panic_high=50.0
    ),
    "calcium": CriticalRange(unit="mg/dl", panic_low=6.0, critical_low=7.0, critical_high=13.0),
}

# Free-text marker names (as extracted from prescriptions/lab reports) -> canonical key.
_ALIASES: dict[str, str] = {
    "potassium": "potassium",
    "serum potassium": "potassium",
    "k": "potassium",
    "k+": "potassium",
    "sodium": "sodium",
    "serum sodium": "sodium",
    "na": "sodium",
    "na+": "sodium",
    "glucose": "glucose",
    "blood glucose": "glucose",
    "fasting blood glucose": "glucose",
    "fbs": "glucose",
    "rbs": "glucose",
    "random blood sugar": "glucose",
    "plasma glucose": "glucose",
    "hemoglobin": "hemoglobin",
    "haemoglobin": "hemoglobin",
    "hb": "hemoglobin",
    "hgb": "hemoglobin",
    "platelets": "platelets",
    "platelet count": "platelets",
    "plt": "platelets",
    "creatinine": "creatinine",
    "serum creatinine": "creatinine",
    "scr": "creatinine",
    "inr": "inr",
    "wbc": "wbc",
    "white blood cell count": "wbc",
    "total leukocyte count": "wbc",
    "tlc": "wbc",
    "calcium": "calcium",
    "serum calcium": "calcium",
    "ca": "calcium",
}

# Unit conversion factors to the canonical unit in CriticalRange.unit, keyed by canonical marker.
_UNIT_CONVERSIONS: dict[str, dict[str, float]] = {
    # mmol/L -> mg/dL
    "glucose": {"mmol/l": 18.0182},
    # µmol/L -> mg/dL
    "creatinine": {"umol/l": 1 / 88.42, "µmol/l": 1 / 88.42},
}

_NORM_RE = re.compile(r"[^a-z0-9+ ]")


def _normalize_marker(marker_name: str) -> str | None:
    key = _NORM_RE.sub("", (marker_name or "").strip().lower()).strip()
    return _ALIASES.get(key)


def _normalize_unit(unit: str | None) -> str:
    """Strip everything but letters/digits so equivalent unit spellings compare equal
    (``10^3/uL``, ``10^3/ul``, ``x10e3/ul`` -> ``103ul``)."""
    return re.sub(r"[^a-z0-9]", "", (unit or "").strip().lower())


def _to_canonical_value(canonical: str, value: float, unit: str | None) -> float | None:
    """Convert ``value`` into the canonical unit for this marker, or None if the supplied unit
    is unrecognised (in which case the caller must skip rather than guess)."""
    norm_unit = _normalize_unit(unit)
    expected = _normalize_unit(_RANGES[canonical].unit)
    if not norm_unit or norm_unit == expected:
        return value
    factor = _UNIT_CONVERSIONS.get(canonical, {}).get(_normalize_unit_key(unit))
    if factor is None:
        return None
    return value * factor


def _normalize_unit_key(unit: str | None) -> str:
    """Normalized-but-with-slash key used to look up _UNIT_CONVERSIONS (e.g. ``mmol/l``)."""
    return re.sub(r"[^a-z0-9/]", "", (unit or "").strip().lower())


def evaluate_critical_value(
    marker_name: str, value: float | None, unit: str | None = None
) -> CriticalLabFlag | None:
    """Return a CriticalLabFlag if ``value`` falls in a curated panic/critical band, else None.

    Deterministic and offline: no DB, no LLM. Unrecognised markers or units are skipped rather
    than guessed at, so this never produces a spurious flag from unit confusion.
    """
    if value is None:
        return None
    canonical = _normalize_marker(marker_name)
    if canonical is None:
        return None
    rng = _RANGES[canonical]
    canonical_value = _to_canonical_value(canonical, value, unit)
    if canonical_value is None:
        return None

    severity: LabSeverity | None = None
    if rng.panic_low is not None and canonical_value <= rng.panic_low:
        severity = "panic_low"
    elif rng.panic_high is not None and canonical_value >= rng.panic_high:
        severity = "panic_high"
    elif rng.critical_low is not None and canonical_value <= rng.critical_low:
        severity = "critical_low"
    elif rng.critical_high is not None and canonical_value >= rng.critical_high:
        severity = "critical_high"
    if severity is None:
        return None

    direction = "low" if severity.endswith("low") else "high"
    tier = "Panic" if severity.startswith("panic") else "Critical"
    return CriticalLabFlag(
        marker_name=marker_name,
        canonical_marker=canonical,
        value=value,
        unit=unit,
        severity=severity,
        summary=(
            f"{tier} {direction} value: {marker_name} = {value}{f' {unit}' if unit else ''}. "
            "This is outside the curated critical-value range and warrants prompt clinical "
            "attention regardless of the reference range on the source report."
        ),
        details={
            "marker_name": marker_name,
            "canonical_marker": canonical,
            "value": value,
            "unit": unit,
            "severity": severity,
            "direction": direction,
        },
    )


def evaluate_lab_results(labs: list[tuple[str, float | None, str | None]]) -> list[CriticalLabFlag]:
    """Evaluate a batch of (marker_name, value_numeric, unit) tuples; returns only the flags."""
    flags: list[CriticalLabFlag] = []
    for marker_name, value, unit in labs:
        flag = evaluate_critical_value(marker_name, value, unit)
        if flag is not None:
            flags.append(flag)
    return flags
