"""Deterministic critical/panic lab-value safety guard (P-XX). No LLM, no I/O.

A "can't-miss" backstop for lab interpretation: curated critical-value thresholds for common
markers, evaluated independently of whatever reference range (if any) the source document
supplied — OCR/vision extraction can miss or mis-transcribe a reference range entirely, so a
critical value must still be caught even when ``LabResult.is_abnormal`` was never set. Runs
fully offline and produces no certainty language (Critical Safety Rules #4 and #8): a flag here
is a prompt for the clinician to look, never a diagnosis or an instruction to act.

Panic values are the more severe tier nested inside critical (panic implies critical). A marker
is read in whichever of the unit systems it is reported in — mg/dL against mmol/L or µmol/L,
g/dL against g/L, 10^3/µL against 10^9/L — because Indian reports carry both, sometimes on the
same page. A supplied unit that is neither the canonical one, a registered spelling of it
(``_UNIT_SYNONYMS``) nor a registered conversion (``_UNIT_CONVERSIONS``) is skipped, never
guessed at, and reported skipped via ``unreadable_lab``.

The rule those three tables exist to keep is that **supplying the unit must never be worse than
omitting it**. A unit the module has not been told about is skipped, and a skip renders exactly
like a normal result — so every unit a real report prints has to be registered, or naming it
disables the guard. That is not hypothetical: a haemoglobin of 55 g/L, a calcium of 1.6 mmol/L
and a platelet count of 8 x10^9/L are all can't-miss values in the ordinary SI spelling, and all
three were skipped in silence until their scales were registered here.

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
    # Urea, twice, because a report means one of two different quantities by it and the two are
    # a factor of 2.14 apart. BUN is the *nitrogen* in the urea (28 g of N per 60.06 g of urea);
    # "Blood Urea" is the whole molecule. Anglo-American labs print BUN, Indian labs mostly print
    # Blood Urea, and both spellings turn up on reports this product ingests.
    #
    # Holding them as one marker would mean picking a threshold that is wrong for the other
    # spelling by that factor in one direction or the other: a "Blood Urea 90 mg/dL" — high but
    # ordinary in CKD, a BUN of 42 — read against a BUN threshold is most of the way to a
    # critical flag, and a genuinely critical BUN read against a urea threshold is silence. So
    # they are two canonical markers with their own thresholds and their own alias sets, and no
    # arithmetic anywhere converts between them: which quantity was measured is the report's to
    # say, exactly as which unit was used is.
    #
    # No panic tier on either. The critical value is the standard >100 mg/dL BUN (and its urea
    # equivalent); above that the number stops discriminating — what escalates a uraemic patient
    # is the clinical picture and the potassium, both of which this module already carries.
    "bun": CriticalRange(unit="mg/dl", critical_high=100.0),
    "urea": CriticalRange(unit="mg/dl", critical_high=214.0),
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
    # Likewise registered for its unit and not for a threshold. A low albumin is a finding over
    # weeks, not a value anyone is telephoned about, so there is no panic band to state. What the
    # entry buys is "this row is a serum albumin, in g/dL" for the Child-Pugh score in
    # ``app.core.hepatic``, which cannot be computed without it.
    "albumin": CriticalRange(unit="g/dl"),
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
    # A glucose row is labelled by when it was drawn as often as by what it is, and the timing
    # word lands on either side of the analyte name depending on the analyser. All of these are
    # the same measurement against the same critical bands: a glucose of 32 is a panic low
    # whether the column header called it fasting, random or post-prandial.
    "fasting blood sugar": "glucose",
    "fasting plasma glucose": "glucose",
    "glucose fasting": "glucose",
    "glucose random": "glucose",
    "glucose pp": "glucose",
    "glucose postprandial": "glucose",
    "glucose post prandial": "glucose",
    "postprandial blood sugar": "glucose",
    "post prandial blood sugar": "glucose",
    "ppbs": "glucose",
    "random blood glucose": "glucose",
    "blood sugar": "glucose",
    "sugar": "glucose",
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
    # "Cr" and "Creat" are how a creatinine is printed on a renal-panel line that has run out of
    # column width, and this is the marker it costs most to miss: it is the input to the eGFR
    # that the metformin hard block hangs off. "Cr Cl"/"Creatinine Clearance" is a different
    # quantity and is fenced above, by "clearance" and "crcl".
    "cr": "creatinine",
    "creat": "creatinine",
    # The two urea quantities. Kept rigorously apart — see the _RANGES entries. Every "nitrogen"
    # spelling is a BUN and every bare-urea spelling is a whole-molecule urea; a name that says
    # neither is not resolved to either.
    "bun": "bun",
    "blood urea nitrogen": "bun",
    "serum urea nitrogen": "bun",
    "urea nitrogen": "bun",
    "urea": "urea",
    "blood urea": "urea",
    "serum urea": "urea",
    "inr": "inr",
    "international normalized ratio": "inr",
    "international normalised ratio": "inr",
    "prothrombin time inr": "inr",
    "wbc": "wbc",
    "wbc count": "wbc",
    "white blood cell count": "wbc",
    "white cell count": "wbc",
    "white blood cells": "wbc",
    # Indian reports print the "-cyte" spellings with a "c" at least as often as with a "k", and
    # abbreviate the whole line to TLC more often than either.
    "total leukocyte count": "wbc",
    "total leucocyte count": "wbc",
    "leukocyte count": "wbc",
    "leucocyte count": "wbc",
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
    # Serum albumin, and only serum albumin. The lookup is exact against this table rather than
    # a substring, which is what keeps "Urine Albumin", "Microalbumin" and "Albumin/Creatinine
    # Ratio" out — three different analytes on three different scales, and the urine ones are
    # the R44 trap ("a urine creatinine was computed into an eGFR") wearing a different name.
    # Nothing is added here for them on purpose: an unrecognised marker is skipped, which is the
    # answer, and the unit check below is a second guard rather than the only one.
    "albumin": "albumin",
    "serum albumin": "albumin",
    "s albumin": "albumin",
    "alb": "albumin",
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
    # mmol/L -> mg/dL for the whole urea molecule (MW 60.06, so mmol/L x 6.006).
    #
    # There is deliberately no mmol/L entry for "bun". The SI convention reports the urea
    # molecule, not its nitrogen, so a row labelled BUN and carrying mmol/L is either mislabelled
    # or using a convention this module cannot identify from the document — and the two readings
    # are a factor of 2.14 apart. Such a row is skipped and reported unreadable, which is the
    # same answer this module gives every other unit it cannot place.
    "urea": {"mmol/l": 6.006},
    # g/L -> g/dL. Indian biochemistry panels print albumin in g/dL far more often, but the SI
    # form turns up on machine-generated reports and is a plain factor of ten.
    "albumin": {"g/l": 0.1},
    # g/L -> g/dL, the same factor of ten, for the marker it turns up on most. ``_UNITLESS_BANDS``
    # below already knew this scale existed — it reads a bare "Hb 120" as ambiguous *because* of
    # it — but with no conversion registered, naming the unit was worse than omitting it: an
    # explicit "Haemoglobin 55 g/L" is a panic-low value at 5.5 g/dL and was skipped outright.
    "hemoglobin": {"g/l": 0.1},
    # mmol/L -> mg/dL for calcium (MW 40.08). This is registered where mEq/L deliberately is not,
    # and the distinction is the point: mEq/L depends on valence and is wrong by two for a
    # divalent ion, whereas mmol/L is a plain molar quantity and converts cleanly whatever the
    # valence. A calcium of 1.6 mmol/L is 6.4 mg/dL — a panic low — and was being skipped.
    "calcium": {"mmol/l": 4.008},
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
    "bun": frozenset({"mg%"}),
    "urea": frozenset({"mg%"}),
    "hemoglobin": frozenset({"gm/dl", "gms/dl", "gm%", "g%"}),
    "bilirubin": frozenset({"mg%"}),
    # An international unit of enzyme activity is the same unit as a unit of enzyme activity,
    # and the "units/L" long form is the same again — all three are printed interchangeably.
    # "U/mL" is deliberately absent: that is a thousandfold different, not a spelling.
    "alt": frozenset({"iu/l", "units/l"}),
    "ast": frozenset({"iu/l", "units/l"}),
    # 10^9/L is the SI cell-count unit and is numerically identical to 10^3/µL — 10^9 per litre
    # is 10^3 per microlitre, the same count written against a different power of ten on both
    # sides. So it is a spelling, not a conversion, and carries no arithmetic. Registering it as
    # a synonym rather than a factor of 1 keeps that visible.
    "platelets": frozenset({"109/l", "x109/l", "10e9/l", "109/liter", "109/litre"}),
    "wbc": frozenset({"109/l", "x109/l", "10e9/l", "109/liter", "109/litre"}),
    # Same g/dL spellings as haemoglobin, which is printed by the same analyser on the same page.
    "albumin": frozenset({"gm/dl", "gms/dl", "gm%", "g%"}),
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
    # mg/dL vs mmol/L. Unlike every other marker here, these two spans overlap across the whole
    # of the normal range — a urea of 5 is 5 mg/dL (low) or 5 mmol/L (30 mg/dL, normal), and no
    # band can separate them because the scales genuinely coincide there. The consequence is
    # deliberate and worth stating: a urea or BUN printed with no unit is reported unreadable
    # rather than evaluated, unless it is high enough that only the mg/dL reading is physically
    # possible — which is precisely the region the critical threshold sits in. So the guard still
    # fires on the values it exists to catch, and stays quiet, and says it is quiet, on the rest.
    "bun": _UnitlessBands(canonical=(1.0, 400.0), others=((0.5, 60.0),)),
    "urea": _UnitlessBands(canonical=(2.0, 800.0), others=((0.5, 60.0),)),
    # g/dL vs g/L (x10), exactly as for haemoglobin. The two spans do not overlap, so a bare
    # albumin is readable either way — but a bare *urine* albumin in mg/L lands squarely in the
    # g/L span, which is a second reason the marker table above admits no urine spelling.
    "albumin": _UnitlessBands(canonical=(0.5, 7.0), others=((10.0, 80.0),)),
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


# --- Marker identification ---------------------------------------------------------------------
#
# The alias table above is matched against a normalised form of whatever a lab report printed,
# and normalising by deleting every character outside [a-z0-9+ ] — which is what this did — is
# only right for the punctuation that is noise. It is wrong for the punctuation that separates,
# and Indian lab reports separate constantly:
#
#   "Potassium (K+)"          -> "potassium k+"   -- no such alias
#   "ALT/SGPT"                -> "altsgpt"        -- two names welded into one word
#   "Bilirubin - Total"       -> "bilirubin  total" (two spaces) -- no such alias
#   "Platelet Count (PLT)"    -> "platelet count plt"
#
# Every one of those is a marker this module holds a critical-value threshold for, and every one
# came back unidentified — which is not reported as unreadable either, because ``unreadable_lab``
# only speaks for markers it could identify. So a potassium of 7.2 printed as "Potassium (K+)"
# was silently dropped by the guard whose entire purpose is to catch it, and the screen rendered
# it exactly as it renders a normal result.
#
# So the name is resolved against several candidate spellings rather than one, in order:
# punctuation deleted (a "T.Bili" is one word), punctuation as a separator (an "ALT/SGPT" is
# two), — for a name qualified in brackets — the part before the bracket and the part inside it,
# since a parenthetical is a synonym or a qualifier and either half may be the name the alias
# table knows, and finally the name with a leading specimen word removed.
#
# That last one is what "S." is. Indian reports label the specimen in front of nearly every
# biochemistry line — "S. Creatinine", "S.Bilirubin", "P. Glucose", "B. Urea", "Total Calcium" —
# and the alias table cannot list the cross-product of every prefix with every marker. It was
# carrying exactly one such spelling by hand ("s albumin"), which is the tell: the prefix is a
# rule, not a synonym. Without it "S.Creatinine" was an unidentified row, and a creatinine is the
# input to the eGFR that the metformin hard block hangs off, so the chart lost the block and the
# renal check reported itself unevaluated on a chart that did have a creatinine on it.
#
# Widening a match is exactly how a different analyte gets read as this one, so the widening is
# fenced by ``_NOT_SERUM_SPECIMEN`` and ``_DIFFERENT_ANALYTE`` below rather than by the narrowness
# of the old comparison. "Albumin (Urine)" must not become a serum albumin by taking the part
# before the bracket, and a urine albumin lands squarely in the g/L band a serum albumin is also
# read in.

# Wordings that mean the row is a different specimen or a derived quantity, not the serum analyte
# these thresholds are written for. Checked against the separator-folded name before any alias
# lookup. The same judgement as ``app.core.clinical._NOT_SERUM_CREATININE``, which exists because
# a urine creatinine fed to CKD-EPI produced a confident eGFR of 0.4.
#
# Deliberately NOT a bare "ratio": the "R" in INR is Ratio, and "INR (International Normalized
# Ratio)" is an ordinary way for an analyser to print it. The albumin/creatinine and
# protein/creatinine ratios are excluded by their own two-word phrases instead.
_NOT_SERUM_SPECIMEN: tuple[str, ...] = (
    "urine",
    "urinary",
    "csf",
    "ascitic",
    "pleural",
    "synovial",
    "dialysate",
    "stool",
    "saliva",
    "clearance",
    "crcl",
    "excretion",
    "spot",
    "microalbumin",
    "albumin creatinine",
    "protein creatinine",
    "creatinine ratio",
    "24h",
    "24 h",
)

# Qualifiers that make the row a *different measurement* on a different scale from the curated
# one, even though the specimen is the same serum. A direct (conjugated) bilirubin is not the
# total bilirubin the 15 mg/dL threshold is written for; an ionised calcium runs about 1.2 mmol/L
# where a total calcium runs about 9.5 mg/dL, so reading one as the other is a panic-low flag on
# a normal result.
#
# Every one of these is already unmatched by the alias table, which lists no spelling for them.
# They are fenced explicitly anyway, because the prefix rule below strips leading words and the
# fence is what stops it stripping a word that carried the analyte's identity: without it,
# "Total Calcium" correctly becoming "calcium" would come with "Ionised Calcium" doing the same.
#
# "free" is here for the thyroid and hormone panels an extraction pass will hand this module
# alongside the rows it does curate; none of them resolve today, and none should start to by
# way of a prefix strip.
_DIFFERENT_ANALYTE: tuple[str, ...] = (
    # Also catches "indirect", which is the other half of the same split and equally not total.
    "direct",
    "conjugated",
    "unconjugated",
    "ionized",
    "ionised",
    "free",
)

# Leading words that name the specimen or say "the whole of this analyte", rather than naming a
# different analyte. Stripped one at a time from the front of the folded name — see the block
# comment above. Order is irrelevant; each is matched as a whole token, never as a substring, so
# the "b" that means blood cannot eat the "b" of "bilirubin".
_SPECIMEN_PREFIXES: frozenset[str] = frozenset({"s", "p", "b", "serum", "plasma", "blood", "total"})

# Punctuation deleted, so "T.Bili" stays one word.
_NORM_DELETE_RE = re.compile(r"[^a-z0-9+ ]")
# Punctuation folded to a space, so "ALT/SGPT" becomes two.
_NORM_SEPARATE_RE = re.compile(r"[^a-z0-9+]+")
# A trailing or embedded parenthetical: "Potassium (K+)" -> "Potassium" + "K+".
_PARENTHETICAL_RE = re.compile(r"\(([^)]*)\)")


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _without_specimen_prefix(folded: str) -> str:
    """``folded`` with any run of leading specimen words removed ("s creatinine" -> "creatinine").

    Never strips the last remaining token: "S" alone, or "Total" alone, is not a marker name, and
    reducing it to "" would then be looked up as one. Stripping stops there rather than at one
    word so that "S. Total Bilirubin", which is one line on a real liver panel, reaches
    "bilirubin".
    """
    tokens = folded.split()
    index = 0
    while index < len(tokens) - 1 and tokens[index] in _SPECIMEN_PREFIXES:
        index += 1
    return " ".join(tokens[index:])


def _marker_candidates(marker_name: str) -> tuple[str, ...]:
    """The spellings of ``marker_name`` to try against the alias table, most literal first.

    Most literal first is what keeps the prefix rule from changing an answer the table already
    had: "total leucocyte count" is looked up whole, and matches, before anything considers
    stripping its "total" down to a "leucocyte count" the table does not list.
    """
    lowered = (marker_name or "").strip().lower()
    if not lowered:
        return ()
    deleted = _collapse(_NORM_DELETE_RE.sub("", lowered))
    folded = _collapse(_NORM_SEPARATE_RE.sub(" ", lowered))
    candidates = [deleted, folded]
    inner = _PARENTHETICAL_RE.findall(lowered)
    if inner:
        outer = _PARENTHETICAL_RE.sub(" ", lowered)
        candidates.append(_collapse(_NORM_SEPARATE_RE.sub(" ", outer)))
        candidates.extend(_collapse(_NORM_SEPARATE_RE.sub(" ", part)) for part in inner)
    # Appended last, and applied to every candidate above rather than only to the whole name, so
    # that "S. Creatinine (Serum)" and "S.Cr" reduce as readily as "S. Creatinine".
    candidates.extend(_without_specimen_prefix(candidate) for candidate in list(candidates))
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return tuple(unique)


def _normalize_marker(marker_name: str) -> str | None:
    """The canonical marker this row is, or None for one this module does not curate.

    None rather than a best guess, for the reason stated throughout this module: which analyte a
    row is, is the report's to say, and inferring it puts a number in the patient's chart that no
    document supports.
    """
    # The fence is tested against the separator-folded *whole* name, computed here rather than
    # taken from the candidate list: the list drops duplicates, so its second entry is not
    # reliably the folded form — for "Albumin (Urine)", whose two folded spellings coincide, the
    # second entry is "albumin" and the fence would have read a urine albumin as a serum one.
    folded = _collapse(_NORM_SEPARATE_RE.sub(" ", (marker_name or "").strip().lower()))
    if not folded:
        return None
    if any(term in folded for term in (*_NOT_SERUM_SPECIMEN, *_DIFFERENT_ANALYTE)):
        return None
    for candidate in _marker_candidates(marker_name):
        canonical = _ALIASES.get(candidate)
        if canonical is not None:
            return canonical
    return None


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
