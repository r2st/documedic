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

A row carrying *no* unit is the harder case, and it is ordinary: the unit lives in a column
header that columnar extraction does not always carry down to the row, or in a footnote OCR
dropped. Such a row is read in the canonical unit only when no other unit this marker is
reported in could produce the same number — see ``_UNITLESS_BANDS``.
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
    "bilirubin": CriticalRange(unit="mg/dl", critical_high=15.0),
    # Registered for their canonical unit, not for a threshold. There is no standard critical
    # value for a transaminase — the number that matters is a multiple of the assay's own upper
    # limit, in the context of the bilirubin and the clinical picture — so this table states
    # none, and ``evaluate_critical_value`` therefore never flags them. What the entry buys is
    # marker identification and unit normalisation for the hepatic dose-adjustment thresholds
    # in ``app.core.safety``, which need "this row is an ALT, in U/L" and nothing more.
    "alt": CriticalRange(unit="u/l"),
    "ast": CriticalRange(unit="u/l"),
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
    # Liver panel. Indian reports print the SGPT/SGOT names at least as often as ALT/AST.
    "bilirubin": "bilirubin",
    "total bilirubin": "bilirubin",
    "serum bilirubin": "bilirubin",
    "serum bilirubin total": "bilirubin",
    "bilirubin total": "bilirubin",
    "t bilirubin": "bilirubin",
    "tbili": "bilirubin",
    "alt": "alt",
    "sgpt": "alt",
    "alt sgpt": "alt",
    "sgpt alt": "alt",
    "alanine aminotransferase": "alt",
    "alanine transaminase": "alt",
    "ast": "ast",
    "sgot": "ast",
    "ast sgot": "ast",
    "sgot ast": "ast",
    "aspartate aminotransferase": "ast",
    "aspartate transaminase": "ast",
}

# Unit conversion factors to the canonical unit in CriticalRange.unit, keyed by canonical marker.
_UNIT_CONVERSIONS: dict[str, dict[str, float]] = {
    # mmol/L -> mg/dL
    "glucose": {"mmol/l": 18.0182},
    # µmol/L -> mg/dL. Keyed only on the ASCII spelling because ``_micro`` folds both Unicode
    # micro signs to "u" before lookup; a "µmol/l" key here could never have been reached, since
    # the key normaliser strips any character outside [a-z0-9/] and turned it into "mol/l".
    "creatinine": {"umol/l": 1 / 88.42},
    # per-µL -> 10^3/µL. 1 µL = 1 mm^3 = 1 "cumm", so this is a decimal shift, not a
    # measurement conversion. Indian CBC reports print the raw per-cumm count far more often
    # than the thousands-scaled one ("PLATELET COUNT 8,000 /cumm").
    "platelets": {"/cumm": 0.001, "cells/cumm": 0.001, "/mm3": 0.001, "cells/mm3": 0.001},
    "wbc": {"/cumm": 0.001, "cells/cumm": 0.001, "/mm3": 0.001, "cells/mm3": 0.001},
    # µmol/L -> mg/dL. ASCII key only, as for creatinine above.
    "bilirubin": {"umol/l": 1 / 17.104},
}

# Spellings that mean the marker's canonical unit exactly, keyed by canonical marker. These
# carry NO arithmetic — they are the same unit written the way an Indian lab report writes it.
#
# Scoped per marker rather than applied globally, because the equivalences are not universal:
# mEq/L equals mmol/L only for a monovalent ion, so it holds for potassium and sodium and would
# be wrong by a factor of two for calcium. "mg%" is mg per 100 mL, which is mg/dL by definition,
# and "gm/dL"/"g%" are g/dL — those are safe wherever the canonical unit already is that.
_UNIT_SYNONYMS: dict[str, frozenset[str]] = {
    "potassium": frozenset({"meq/l"}),
    "sodium": frozenset({"meq/l"}),
    "glucose": frozenset({"mg%"}),
    "creatinine": frozenset({"mg%"}),
    "calcium": frozenset({"mg%"}),
    "hemoglobin": frozenset({"gm/dl", "gms/dl", "gm%", "g%"}),
    "bilirubin": frozenset({"mg%"}),
    # An international unit of enzyme activity is the same unit as a unit of enzyme activity,
    # and the "units/L" long form is the same again — all three are printed interchangeably.
    # "U/mL" is deliberately absent: that is a thousandfold different, not a spelling.
    "alt": frozenset({"iu/l", "units/l"}),
    "ast": frozenset({"iu/l", "units/l"}),
}


@dataclass(frozen=True)
class _UnitlessBands:
    """The spans a real human result for one marker occupies, as a number on a page.

    ``canonical`` is that span in ``CriticalRange.unit``; ``others`` are the spans it occupies
    in each *other* unit the same marker is printed in around here — as printed in that unit,
    not converted. Both are deliberately generous: they are here to separate two unit scales
    from each other, not to decide whether a value is normal.
    """

    canonical: tuple[float, float]
    others: tuple[tuple[float, float], ...] = ()


# A bare number is read in the canonical unit only when it lands inside that unit's span and
# outside every other unit's. Anything else is two readings of one number, and the two disagree
# about the patient: 5.5 is a normal glucose in mmol/L and a value incompatible with
# consciousness in mg/dL; 88 is a normal creatinine in µmol/L and a dialysis-dependent one in
# mg/dL; 8000 is a normal white count per cumm and a leukemic one in 10^3/µL.
#
# Assuming the canonical unit — which is what this did — turns each of those into a confident
# panic flag on a patient whose labs are entirely normal, and in the creatinine case into a
# fabricated eGFR of 0.4 written into the chart (``creatinine_to_mg_dl`` feeds the CKD-EPI
# derivation) with the metformin hard block hanging off it. That is the alert-fatigue failure
# this module's own docstring warns about, manufactured by the module itself.
#
# So an ambiguous bare number is skipped, and — unlike before — reported as skipped, via
# ``unreadable_lab``. Note what is deliberately NOT done: a number outside the canonical span
# but inside exactly one other (a bare 88 creatinine) is not silently converted either. Which
# unit a report meant is the report's to say; inferring it would put a number in the chart that
# no document supports, which is the failure in the other direction.
_UNITLESS_BANDS: dict[str, _UnitlessBands] = {
    # mEq/L is numerically identical to mmol/L for a monovalent ion, so there is no second
    # scale for the electrolytes and any real value can be read as printed.
    "potassium": _UnitlessBands(canonical=(0.5, 15.0)),
    "sodium": _UnitlessBands(canonical=(60.0, 220.0)),
    # mg/dL vs mmol/L (x18).
    "glucose": _UnitlessBands(canonical=(20.0, 2000.0), others=((1.0, 85.0),)),
    # mg/dL vs µmol/L (x88.4).
    "creatinine": _UnitlessBands(canonical=(0.1, 25.0), others=((20.0, 3000.0),)),
    # g/dL vs g/L (x10) — "Hb 120" is an ordinary way to print a normal haemoglobin.
    "hemoglobin": _UnitlessBands(canonical=(1.0, 30.0), others=((30.0, 250.0),)),
    # 10^3/µL vs the raw per-cumm count Indian CBC reports print more often.
    "platelets": _UnitlessBands(canonical=(1.0, 3000.0), others=((1000.0, 3_000_000.0),)),
    "wbc": _UnitlessBands(canonical=(0.1, 500.0), others=((100.0, 500_000.0),)),
    # mg/dL vs mmol/L (x4). No conversion factor is registered for calcium — mEq/L would be
    # wrong for a divalent ion — but the mmol/L scale still exists on the page, and a bare 2.4
    # read as mg/dL is a panic-low flag on a normal corrected calcium.
    "calcium": _UnitlessBands(canonical=(2.0, 20.0), others=((0.5, 5.0),)),
    # A ratio has no unit to lose.
    "inr": _UnitlessBands(canonical=(0.1, 30.0)),
    # mg/dL vs µmol/L (x17.1).
    "bilirubin": _UnitlessBands(canonical=(0.05, 60.0), others=((70.0, 1000.0),)),
    # U/L is effectively the only scale these are printed in.
    "alt": _UnitlessBands(canonical=(1.0, 20000.0)),
    "ast": _UnitlessBands(canonical=(1.0, 20000.0)),
}


def _canonical_when_unitless(canonical: str, value: float) -> float | None:
    """``value`` read in the marker's canonical unit, or None if it could be another one."""
    bands = _UNITLESS_BANDS.get(canonical)
    if bands is None:
        return value
    low, high = bands.canonical
    if not low <= value <= high:
        return None
    return None if any(lo <= value <= hi for lo, hi in bands.others) else value


_NORM_RE = re.compile(r"[^a-z0-9+ ]")


def _normalize_marker(marker_name: str) -> str | None:
    key = _NORM_RE.sub("", (marker_name or "").strip().lower()).strip()
    return _ALIASES.get(key)


def _micro(unit: str) -> str:
    """Fold both Unicode micro signs to ASCII "u".

    U+00B5 MICRO SIGN and U+03BC GREEK SMALL LETTER MU are visually identical and both come off
    lab reports. Neither survives the character classes below, so ``10^3/µL`` normalised to
    ``103l`` -- which matches neither the canonical ``103ul`` nor any conversion key, and a
    platelet count printed with a real micro sign was skipped rather than evaluated.
    """
    return unit.replace("µ", "u").replace("μ", "u")


def _normalize_unit(unit: str | None) -> str:
    """Strip everything but letters/digits so equivalent unit spellings compare equal
    (``10^3/uL``, ``10^3/ul``, ``x10e3/ul`` -> ``103ul``)."""
    return re.sub(r"[^a-z0-9]", "", _micro((unit or "").strip().lower()))


def _to_canonical_value(canonical: str, value: float, unit: str | None) -> float | None:
    """Convert ``value`` into the canonical unit for this marker, or None if the supplied unit
    is unrecognised (in which case the caller must skip rather than guess)."""
    norm_unit = _normalize_unit(unit)
    expected = _normalize_unit(_RANGES[canonical].unit)
    if not norm_unit:
        # No unit on the row at all. See ``_UNITLESS_BANDS`` for why this is not simply the
        # canonical unit.
        return _canonical_when_unitless(canonical, value)
    if norm_unit == expected:
        return value
    # A synonym is the same unit spelled differently, so it carries no arithmetic. Checked
    # before the bail-out below, which otherwise treats "the unit written another way" the same
    # as "a unit I cannot interpret" and drops the flag entirely.
    if _unit_symbol(unit) in _UNIT_SYNONYMS.get(canonical, frozenset()):
        return value
    factor = _UNIT_CONVERSIONS.get(canonical, {}).get(_normalize_unit_key(unit))
    if factor is None:
        return None
    return value * factor


def creatinine_to_mg_dl(value: float, unit: str | None) -> float | None:
    """A creatinine reading in mg/dL, or None when the unit is not one this module can read.

    Exposed because the eGFR derivation needs exactly the conversion the critical-value guard
    already does — the same row was being converted here and taken at face value there, so a
    creatinine reported in µmol/L was read correctly by one and off by a factor of 88 by the
    other. Returning None rather than a guess is the contract every caller depends on: a
    creatinine in units this module cannot interpret has to be skipped, not assumed.
    """
    return _to_canonical_value("creatinine", value, unit)


def canonical_lab_value(
    marker_name: str, value: float | None, unit: str | None
) -> tuple[str, float] | None:
    """``(canonical marker, value in that marker's canonical unit)``, or None.

    The general form of ``creatinine_to_mg_dl``, for callers that need to identify the marker
    as well as convert it — the hepatic dose-adjustment thresholds in ``app.core.safety`` have
    to know a bilirubin from an ALT before they can compare either to anything.

    None for a marker this module does not curate, and for a value it cannot place on a scale.
    Both are the same answer to the caller for the same reason as everywhere else here: a guess
    about which analyte or which unit is a number in the patient's chart.
    """
    if value is None:
        return None
    canonical = _normalize_marker(marker_name)
    if canonical is None:
        return None
    converted = _to_canonical_value(canonical, value, unit)
    return None if converted is None else (canonical, converted)


def _normalize_unit_key(unit: str | None) -> str:
    """Normalized-but-with-slash key used to look up _UNIT_CONVERSIONS (e.g. ``mmol/l``)."""
    return re.sub(r"[^a-z0-9/]", "", _micro((unit or "").strip().lower()))


def _unit_symbol(unit: str | None) -> str:
    """Like ``_normalize_unit_key`` but keeps ``%``, which several synonyms depend on.

    "mg%" and "g%" are ordinary Indian lab notation for mg/dL and g/dL. Dropping the percent
    sign would collapse them to bare "mg"/"g" and make the synonym table match a mass unit.
    """
    return re.sub(r"[^a-z0-9/%]", "", _micro((unit or "").strip().lower()))


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


@dataclass(frozen=True)
class UnreadableLab:
    """A curated marker whose value could not be placed on a scale, so it was not evaluated."""

    marker_name: str
    canonical_marker: str
    value: float
    unit: str | None
    reason: Literal["unit_missing", "unit_unrecognised"]
    summary: str


def unreadable_lab(
    marker_name: str, value: float | None, unit: str | None = None
) -> UnreadableLab | None:
    """Say when a marker this module curates thresholds for was skipped, and why.

    ``evaluate_critical_value`` answers None both for "evaluated, within range" and for "could
    not be evaluated at all", and a screen built only from its flags renders the second as the
    first — a potassium in a unit this module cannot read, or a bare number that could be
    either of two scales, comes back looking exactly like a normal potassium.

    That is the shape of answer this codebase refuses everywhere it has been found: an
    unresolvable proposed drug is a 422 rather than an unchecked pass, a renal rule with no
    eGFR reports itself unevaluated, and an unreadable medication line is named rather than
    dropped. This is the same statement for a lab row, and it matters most for exactly the
    values the guard exists to catch — a glucose of 30 printed without a unit is a panic low in
    mg/dL and an emergency in the other direction in mmol/L, and the honest answer is that the
    document does not say which.

    Only for markers with curated thresholds: a row this module has no opinion about was never
    going to be evaluated, and reporting every one of those would bury the rows that were.
    """
    if value is None:
        return None
    canonical = _normalize_marker(marker_name)
    if canonical is None or _to_canonical_value(canonical, value, unit) is not None:
        return None

    expected = _RANGES[canonical].unit
    if not _normalize_unit(unit):
        reason: Literal["unit_missing", "unit_unrecognised"] = "unit_missing"
        why = (
            f"the result carries no unit and {value} is a value this marker takes in more than "
            f"one of the units it is reported in, so which one was meant cannot be read off the "
            f"document"
        )
    else:
        reason = "unit_unrecognised"
        why = f"the unit “{unit}” is not one this check can convert to {expected}"
    return UnreadableLab(
        marker_name=marker_name,
        canonical_marker=canonical,
        value=value,
        unit=unit,
        reason=reason,
        summary=(
            f"{marker_name} = {value}{f' {unit}' if unit else ''} was not checked against "
            f"critical-value thresholds: {why}. This is not the same as a normal result — "
            f"confirm the unit on the source report."
        ),
    )
