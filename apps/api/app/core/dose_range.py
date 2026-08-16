"""Is the dose written on this chart a dose of this drug, for this patient?

The question :mod:`app.core.dose_text` explicitly declines
------------------------------------------------------------
That module judges whether a number in model-written prose could be a dose of the named product
*at all*, measured against the strengths the vocabulary lists it in, at a deliberately enormous
100x ceiling. It says in its own docstring that it attempts no judgement about whether a dose is
right for a patient, "because that needs indication, weight, renal function and a curated
per-drug maximum".

Three of those four now exist. ``SafetyContext`` carries ``age_years``, ``egfr`` and (since the
migration that accompanies this module) ``weight_kg``; what was missing was the fourth, the
curated per-drug maximum, and it is the table below. Indication is still absent and always will
be — nothing in this record says *why* a drug was started — so every range here is the widest
one across the indications the drug is licensed for in this market. A range narrowed to one
indication would fire on correct prescribing for another, which is how a dose warning stops
being read.

What this catches that nothing else did
---------------------------------------
The three prescribing errors this table exists for, in the order they kill people:

* **The wrong unit.** Levothyroxine and digoxin are dosed in micrograms and dispensed in
  micrograms; "Levothyroxine 100 mg" is a fluent line on a prescription and 1000x the dose.
  ``dose_text`` catches this only for text *this system generated*. Nothing checked what a
  clinician typed, or what an OCR pass read off a scan. Detected here by a test that is more
  specific than "the number is large": the same number read in the drug's own dosing unit lands
  inside its therapeutic range, which is what a slipped unit looks like and what a genuine
  overdose does not.
* **The wrong interval.** Methotrexate for rheumatoid disease is a *weekly* dose. Charted daily
  it is a well-documented fatal error, and every individual number in "7.5 mg" is unremarkable —
  the danger is entirely in the frequency, which no ceiling on a single dose can see.
* **The right dose for the wrong body.** An adult maximum applied to a nine-year-old, or a
  metformin dose that was correct before this patient's eGFR fell to 38.

Deliberately not a hard block
-----------------------------
Every finding here is a warning or a critical flag, never a hard block, for the reason
``check_dose_integrity`` gives: a hard block is Critical Safety Rule #3's instrument for a
documented conflict between a real drug and a real chart, it demands an override with written
reasoning, and spending it on a dose that is *probably* wrong is how the blocks that are never
wrong stop being read. Dose ranges are the check most likely to be legitimately exceeded —
specialists exceed them daily, on purpose, with reasons this record does not hold.

Pure, offline, and a pure function of its arguments (Critical Safety Rule #8): no clock, no
vocabulary lookup, no LLM. The service resolves the drug and hands the resolved identity here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.core.dose_text import (
    NUMBER,
    TO_MILLIGRAMS,
    UNIT_ALTERNATION,
    canonical_unit,
    to_float,
)

# Under this age a dose is a function of body size, not of adulthood, and the adult ceilings
# below are the wrong comparison in both directions — they clear a dangerous dose for a
# four-year-old and they say nothing about a sub-therapeutic one. Twelve rather than eighteen
# because adolescent dosing converges on adult dosing well before adulthood: most paediatric
# formularies switch to adult doses at 12, or at 40 kg, whichever comes first.
PAEDIATRIC_MAX_AGE_YEARS = 12

# The age from which the curated older-adult ceilings apply, matching
# ``check_geriatric_cautions``' own threshold so one chart cannot be "elderly" for one check and
# not for the other.
GERIATRIC_MIN_AGE_YEARS = 65

# How far past the applicable ceiling a dose has to be before the flag stops being a warning and
# becomes a critical finding. A dose 30% over a maximum is the ordinary shape of specialist
# prescribing and of a rounded tablet count; double the maximum is not.
CRITICAL_EXCESS_MULTIPLE = 2.0

DoseInterval = Literal["daily", "weekly"]

AssessmentKind = Literal[
    # The charted amount is above the ceiling that applies to this patient.
    "above_maximum",
    # Below the smallest dose at which the drug is expected to do anything.
    "below_minimum",
    # The number looks like a dose of this drug in the unit the drug is *actually* dosed in,
    # and was charted in a different one. The sharpest finding here.
    "unit_mismatch",
    # A weekly drug charted at a daily frequency.
    "interval_mismatch",
    # A child, a drug with a curated weight-based range, and no weight on the chart.
    "not_evaluated",
]


@dataclass(frozen=True)
class DoseRange:
    """The curated therapeutic range for one molecule, in this market, for adults.

    ``dosing_unit`` is the unit the product is prescribed and dispensed in — micrograms for
    levothyroxine, milligrams for metformin. It is not derivable from the vocabulary's
    ``strength`` field, which is a product label and carries whatever the manufacturer printed;
    it is what the unit-slip test is measured against.

    All the milligram figures are milligrams even for a microgram-dosed drug, so one arithmetic
    path serves both. ``daily_min_mg`` is None for a drug with no meaningful floor — an as-needed
    analgesic has no sub-therapeutic daily total, because one tablet in a day is the intended
    use.
    """

    dosing_unit: str
    single_max_mg: float
    daily_max_mg: float
    daily_min_mg: float | None = None
    interval: DoseInterval = "daily"
    # (eGFR strictly below this, ceiling that then applies), tightest last. Only the curated
    # renally-cleared drugs carry these. A chart with no eGFR takes the adult ceiling and the
    # flag says the renal adjustment was not applied, rather than silently using a ceiling that
    # may not be this patient's.
    renal_daily_max_mg: tuple[tuple[float, float], ...] = ()
    # The reduced ceiling for an older adult, where one is standard rather than a judgement.
    geriatric_daily_max_mg: float | None = None
    # (min, max) mg per kilogram per day for a child. Absent means this module says nothing
    # about a child on this drug: an uncurated paediatric dose is a gap, and the honest
    # response to a gap is silence about the dose plus whatever ``check_paediatric_cautions``
    # has to say about the drug.
    paediatric_mg_per_kg_per_day: tuple[float, float] | None = None
    note: str | None = None


# Keyed on the normalised INN, not on the reference id: ``AML-5`` and ``AML-10`` are one drug
# with one therapeutic range, and a table keyed on the id would have to repeat every range once
# per marketed strength and would silently miss the next strength seeded.
#
# **Single-ingredient products only.** A fixed-dose combination is dosed as a product — "one
# tablet twice daily" — and its two molecules have two ranges that a single charted number
# cannot be apportioned between. Charting 625 mg of co-amoxiclav says nothing checkable about
# how much amoxicillin that is. Combinations are therefore absent, and ``assess_dose`` reports
# nothing for them rather than measuring the whole tablet against one ingredient's ceiling.
#
# Figures are the widest licensed adult range across indications, from the ICMR Standard
# Treatment Workflows where they specify one, and from the product literature otherwise.
THERAPEUTIC_RANGES: dict[str, DoseRange] = {
    # --- diabetes ---
    "metformin": DoseRange(
        dosing_unit="mg",
        single_max_mg=1000,
        daily_max_mg=2550,
        daily_min_mg=500,
        # eGFR < 30 is an absolute contraindication and is already a hard block from the
        # curated contraindication rule; this is the 30-45 band, where the drug is continued at
        # half dose rather than stopped.
        renal_daily_max_mg=((45, 1000),),
    ),
    "glimepiride": DoseRange(dosing_unit="mg", single_max_mg=8, daily_max_mg=8, daily_min_mg=1),
    "sitagliptin": DoseRange(
        dosing_unit="mg",
        single_max_mg=100,
        daily_max_mg=100,
        daily_min_mg=25,
        renal_daily_max_mg=((45, 50), (30, 25)),
    ),
    "vildagliptin": DoseRange(
        dosing_unit="mg", single_max_mg=50, daily_max_mg=100, daily_min_mg=50
    ),
    # --- cardiovascular ---
    "telmisartan": DoseRange(dosing_unit="mg", single_max_mg=80, daily_max_mg=80, daily_min_mg=20),
    "amlodipine": DoseRange(dosing_unit="mg", single_max_mg=10, daily_max_mg=10, daily_min_mg=2.5),
    "atenolol": DoseRange(
        dosing_unit="mg",
        single_max_mg=100,
        daily_max_mg=100,
        daily_min_mg=25,
        renal_daily_max_mg=((35, 50),),
    ),
    "enalapril": DoseRange(dosing_unit="mg", single_max_mg=20, daily_max_mg=40, daily_min_mg=2.5),
    "ramipril": DoseRange(dosing_unit="mg", single_max_mg=10, daily_max_mg=10, daily_min_mg=1.25),
    "atorvastatin": DoseRange(dosing_unit="mg", single_max_mg=80, daily_max_mg=80, daily_min_mg=10),
    "rosuvastatin": DoseRange(
        dosing_unit="mg",
        single_max_mg=40,
        daily_max_mg=40,
        daily_min_mg=5,
        renal_daily_max_mg=((30, 10),),
    ),
    "clopidogrel": DoseRange(
        dosing_unit="mg",
        # 300-600 mg is the loading dose, given once; 75 mg is maintenance. The ceiling is the
        # loading dose because a chart cannot distinguish the two.
        single_max_mg=600,
        daily_max_mg=600,
        daily_min_mg=75,
    ),
    "isosorbide mononitrate": DoseRange(
        dosing_unit="mg", single_max_mg=120, daily_max_mg=120, daily_min_mg=20
    ),
    "furosemide": DoseRange(dosing_unit="mg", single_max_mg=250, daily_max_mg=600, daily_min_mg=20),
    "spironolactone": DoseRange(
        dosing_unit="mg",
        # 400 mg/day is ascites dosing; heart failure uses 12.5-50.
        single_max_mg=200,
        daily_max_mg=400,
        daily_min_mg=12.5,
        geriatric_daily_max_mg=25,
        note="Beers criteria cap spironolactone at 25 mg/day in adults 65 and over.",
    ),
    "digoxin": DoseRange(
        dosing_unit="mcg",
        single_max_mg=0.5,
        daily_max_mg=0.5,
        daily_min_mg=0.0625,
        renal_daily_max_mg=((60, 0.25), (30, 0.125)),
        geriatric_daily_max_mg=0.125,
        note="Digoxin is dosed in micrograms; 0.25 mg is 250 mcg.",
    ),
    "warfarin": DoseRange(
        dosing_unit="mg",
        single_max_mg=15,
        daily_max_mg=15,
        daily_min_mg=0.5,
        note="Warfarin dosing is titrated to INR, so this range is a plausibility bound only.",
    ),
    # --- analgesia and inflammation ---
    "paracetamol": DoseRange(
        dosing_unit="mg",
        single_max_mg=1000,
        daily_max_mg=4000,
        # 10-15 mg/kg per dose, four doses a day. Expressed per day rather than per dose
        # because the frequency is what the record actually holds, and because the daily total
        # is the number hepatotoxicity turns on.
        paediatric_mg_per_kg_per_day=(40, 60),
    ),
    "ibuprofen": DoseRange(
        dosing_unit="mg",
        single_max_mg=800,
        daily_max_mg=2400,
        paediatric_mg_per_kg_per_day=(20, 40),
    ),
    "diclofenac": DoseRange(dosing_unit="mg", single_max_mg=75, daily_max_mg=150),
    "aspirin": DoseRange(
        dosing_unit="mg",
        # Antiplatelet aspirin is 75-150 mg and analgesic aspirin runs to 4 g/day. Without an
        # indication the record does not hold, the ceiling has to be the analgesic one — a
        # 300 mg tablet for pain is correct prescribing and must not be flagged as ten times
        # the antiplatelet dose.
        single_max_mg=1000,
        daily_max_mg=4000,
    ),
    "prednisolone": DoseRange(
        dosing_unit="mg", single_max_mg=100, daily_max_mg=100, daily_min_mg=1
    ),
    "methotrexate": DoseRange(
        dosing_unit="mg",
        single_max_mg=25,
        daily_max_mg=25,
        daily_min_mg=7.5,
        interval="weekly",
        note=(
            "Methotrexate for inflammatory disease is a once-weekly dose. Daily administration "
            "of a weekly dose is a documented cause of fatal marrow suppression."
        ),
    ),
    # --- anti-infectives ---
    "azithromycin": DoseRange(
        dosing_unit="mg",
        # 1 g and 2 g single doses are both in use for specific indications.
        single_max_mg=2000,
        daily_max_mg=2000,
        daily_min_mg=250,
    ),
    "ciprofloxacin": DoseRange(
        dosing_unit="mg",
        single_max_mg=750,
        daily_max_mg=1500,
        daily_min_mg=500,
        renal_daily_max_mg=((30, 500),),
    ),
    "ceftriaxone": DoseRange(
        dosing_unit="mg", single_max_mg=4000, daily_max_mg=4000, daily_min_mg=250
    ),
    # --- gastrointestinal ---
    "pantoprazole": DoseRange(dosing_unit="mg", single_max_mg=80, daily_max_mg=80, daily_min_mg=20),
    "omeprazole": DoseRange(dosing_unit="mg", single_max_mg=40, daily_max_mg=80, daily_min_mg=10),
    "ranitidine": DoseRange(
        dosing_unit="mg", single_max_mg=300, daily_max_mg=300, daily_min_mg=150
    ),
    # --- respiratory ---
    "salbutamol": DoseRange(
        dosing_unit="mcg",
        single_max_mg=0.8,
        daily_max_mg=1.6,
        note="Inhaled salbutamol is dosed in micrograms per actuation (100 mcg per puff).",
    ),
    "theophylline": DoseRange(
        dosing_unit="mg", single_max_mg=400, daily_max_mg=900, daily_min_mg=200
    ),
    # --- endocrine and neuro ---
    "levothyroxine": DoseRange(
        dosing_unit="mcg",
        single_max_mg=0.3,
        daily_max_mg=0.3,
        daily_min_mg=0.0125,
        note="Levothyroxine is dosed in micrograms; 100 mcg is 0.1 mg.",
    ),
    "phenytoin": DoseRange(
        dosing_unit="mg",
        # An oral loading dose is 1 g given in divided doses over one day; maintenance is
        # 200-400 mg/day.
        single_max_mg=400,
        daily_max_mg=1000,
        daily_min_mg=100,
    ),
    "lithium carbonate": DoseRange(
        dosing_unit="mg",
        single_max_mg=900,
        daily_max_mg=1800,
        daily_min_mg=300,
        renal_daily_max_mg=((60, 900),),
    ),
    "sildenafil": DoseRange(dosing_unit="mg", single_max_mg=100, daily_max_mg=100, daily_min_mg=20),
}


def range_for(generic_name: str | None) -> DoseRange | None:
    """The curated range for an INN, or None when this module has nothing to say.

    None is "not curated", never "no maximum". Every caller has to keep that distinction: a drug
    absent from the table has not been checked, and reporting it as within range would be the
    fail-open shape this codebase has closed four times already.
    """
    return THERAPEUTIC_RANGES.get((generic_name or "").strip().lower())


def is_weight_dosed(generic_name: str | None) -> bool:
    """True when this drug's curated ceiling is a function of body weight.

    "Weight-dependent" is not a property anyone typed in beside each drug — it is exactly the
    set that carries ``paediatric_mg_per_kg_per_day``, because that band is the only place in
    this table where the patient's weight is an input to the arithmetic. Deriving it rather
    than curating a second list is what keeps the two from drifting: a drug that gains a
    weight-based band gains the staleness check with it, and one that loses the band loses it.

    Read by ``app.core.safety.check_weight_staleness``, which asks a different question from
    ``assess_dose``: not "is this dose right" but "is the weight that judgement rests on still
    this patient's weight".
    """
    dose_range = range_for(generic_name)
    return dose_range is not None and dose_range.paediatric_mg_per_kg_per_day is not None


# --- reading what the chart wrote -------------------------------------------------------------

# ``medication_events.dose`` is free text off a prescription or an OCR pass, and it arrives as
# "500", "500 mg", "1 tab", "2 puffs", "1-2 tablets". Anchored at the start because the leading
# number is the dose; a trailing number is a tablet count, a duration or a strength repeated.
_CHARTED_AMOUNT_RE = re.compile(
    rf"^\s*(?P<amount>{NUMBER})"
    # A range is judged at its upper end, exactly as ``dose_text`` judges prose ranges.
    rf"(?:\s*(?:-|–|—|to)\s*(?P<upper>{NUMBER}))?"
    rf"(?:\s*(?P<unit>{UNIT_ALTERNATION})s?(?![A-Za-z]))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChartedAmount:
    """One charted dose, reduced to a number and a mass."""

    amount: float
    unit: str
    milligrams: float


def parse_charted_amount(dose: str | None, dose_unit: str | None) -> ChartedAmount | None:
    """The mass a ``dose``/``dose_unit`` pair names, or None if it names no mass.

    Returns None — not a guess — for "1 tablet", "2 puffs", "as directed" and every other
    quantity expressed in units of the *product* rather than of the drug. A tablet count is a
    real and common way to write a prescription, and converting it would need the dispensed
    strength, which the chart does not record on the medication row. Nothing is flagged from a
    tablet count, in either direction.

    The unit written inside ``dose`` wins over ``dose_unit`` when both are present: extraction
    fills ``dose_unit`` from a separate column of the source document, and the two disagreeing
    means the structured column is the one that lost information.
    """
    if not dose or not dose.strip():
        return None
    match = _CHARTED_AMOUNT_RE.match(dose)
    if not match:
        return None
    raw_unit = match.group("unit") or dose_unit or ""
    unit = canonical_unit(raw_unit.strip())
    if unit not in TO_MILLIGRAMS:
        return None
    amount = to_float(match.group("upper") or match.group("amount"))
    if amount <= 0:
        return None
    return ChartedAmount(amount=amount, unit=unit, milligrams=amount * TO_MILLIGRAMS[unit])


# Frequency as prescriptions in this market actually write it. The Latin abbreviations are what
# a doctor writes by hand; the "q8h" forms are what a discharge summary prints; and "1-0-1" is
# the morning-afternoon-night grid used on nearly every Indian prescription pad, which no
# formulary lists and which is by far the most common of the three here.
_FREQUENCY_PER_DAY: dict[str, float] = {
    "od": 1,
    "qd": 1,
    "qds": 4,
    "qid": 4,
    "bd": 2,
    "bid": 2,
    "tds": 3,
    "tid": 3,
    "hs": 1,
    "nocte": 1,
    "mane": 1,
    "om": 1,
    "on": 1,
    "daily": 1,
    "once daily": 1,
    "once a day": 1,
    "twice daily": 2,
    "twice a day": 2,
    "thrice daily": 3,
    "three times daily": 3,
    "three times a day": 3,
    "four times daily": 4,
    "four times a day": 4,
    "every other day": 0.5,
    "alternate days": 0.5,
    "alternate day": 0.5,
    "eod": 0.5,
    "weekly": 1 / 7,
    "once weekly": 1 / 7,
    "once a week": 1 / 7,
    "every week": 1 / 7,
}

# "q6h", "6 hourly", "every 8 hours" — one family, three spellings.
_HOURLY_RE = re.compile(
    r"(?:^|\b)(?:q\s*|every\s+)?(?P<hours>\d{1,2})\s*(?:h|hr|hrs|hour|hours|hourly)\b",
    re.IGNORECASE,
)

# The Indian dosing grid: "1-0-1", "1-1-1", "0-0-1", also written with slashes. Read as a count
# of administrations rather than of tablets, which is what it is: each position is one
# administration time, and the number in it is how many units are taken then.
_GRID_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)\s*$")

# As-needed. A PRN frequency has no daily total, because the whole point of it is that the
# number of doses is the patient's decision. The single-dose ceiling still applies.
_AS_NEEDED = ("prn", "sos", "as needed", "as required", "when required", "if needed")


def doses_per_day(frequency: str | None) -> float | None:
    """How many times a day ``frequency`` says to take it, or None if it does not say.

    None covers three different situations that all have the same consequence — an as-needed
    order, a frequency nobody wrote down, and a spelling this function does not know — and the
    consequence is that no daily total can be computed, so only the single-dose ceiling is
    applied. None is never treated as once daily: assuming the smallest frequency would clear
    every overdose written as "500 mg" beside an unreadable frequency.
    """
    if not frequency or not frequency.strip():
        return None
    text = frequency.strip().lower()
    if any(marker in text for marker in _AS_NEEDED):
        return None
    if grid := _GRID_RE.match(text):
        # Each non-zero position is one administration. The magnitudes are tablet counts and
        # multiply the *amount*, not the frequency, and this function answers only about
        # frequency — see ``assess_dose``, which is why the grid's counts are deliberately
        # discarded here rather than folded in.
        return float(sum(1 for value in grid.groups() if float(value) > 0)) or None
    squashed = " ".join(text.replace(".", "").split())
    if squashed in _FREQUENCY_PER_DAY:
        return _FREQUENCY_PER_DAY[squashed]
    for phrase, per_day in _FREQUENCY_PER_DAY.items():
        # Word-bounded so "od" does not match inside "food" and "on" does not match "once".
        if re.search(rf"(?:^|\s){re.escape(phrase)}(?:\s|$)", squashed):
            return per_day
    if hourly := _HOURLY_RE.search(squashed):
        hours = float(hourly.group("hours"))
        if 0 < hours <= 24:
            return 24 / hours
    return None


def is_daily_frequency(frequency: str | None) -> bool:
    """Whether ``frequency`` says "at least once a day" — the weekly-drug test.

    Separate from ``doses_per_day`` returning a number because an unreadable frequency must not
    read as daily: a methotrexate row whose frequency nobody could transcribe is not evidence
    that it was taken daily, and a fatal-error flag raised on no evidence is the flag a
    clinician learns to dismiss.
    """
    per_day = doses_per_day(frequency)
    return per_day is not None and per_day >= 1


# --- the judgement ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DoseAssessment:
    """One thing wrong with one charted dose, with the arithmetic that produced it.

    ``message`` is prescriber-framed prose (Critical Safety Rule #4) and is what the flag's
    summary is built from; ``details`` is the same finding as numbers, so a clinician who
    disagrees can see exactly which ceiling was applied and why that one.
    """

    kind: AssessmentKind
    generic_name: str
    message: str
    details: dict
    # True when the excess is large enough to grade the flag critical rather than warning; see
    # ``CRITICAL_EXCESS_MULTIPLE``. Always True for a unit or interval mismatch, which are
    # specific findings rather than a matter of degree.
    severe: bool = False


def _quantity(milligrams: float, unit: str) -> str:
    """A milligram figure written in the unit the drug is prescribed in.

    Every ``DoseRange`` in the table is dosed in ``mg`` or ``mcg``, so those are the two cases;
    anything else falls through to milligrams rather than raising, because a display helper is
    not where a curation slip should surface.
    """
    if unit == "mcg":
        return f"{_trim(milligrams * 1000)} mcg"
    return f"{_trim(milligrams)} mg"


def _trim(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _applicable_daily_max(
    dose_range: DoseRange, *, egfr: float | None, age_years: int | None
) -> tuple[float, str]:
    """The daily ceiling for *this* patient, and the one word for why it is that one.

    Renal first and geriatric second, then the tighter of the two wins, because the two adjust
    for different things and an older adult with poor renal function is entitled to both. The
    reason string names whichever ceiling actually bound, which is what the flag has to say: "80
    mg/day exceeds the 25 mg/day this patient's age indicates" is actionable and "exceeds the
    maximum" is not.
    """
    ceiling = dose_range.daily_max_mg
    reason = "the usual adult maximum"
    if egfr is not None:
        for threshold, renal_max in dose_range.renal_daily_max_mg:
            if egfr < threshold and renal_max < ceiling:
                ceiling, reason = renal_max, f"the maximum at an eGFR of {_trim(egfr)}"
    if (
        age_years is not None
        and age_years >= GERIATRIC_MIN_AGE_YEARS
        and dose_range.geriatric_daily_max_mg is not None
        and dose_range.geriatric_daily_max_mg < ceiling
    ):
        ceiling = dose_range.geriatric_daily_max_mg
        reason = f"the maximum indicated at {age_years} years"
    return ceiling, reason


def assess_dose(
    *,
    generic_name: str,
    dose: str | None,
    dose_unit: str | None,
    frequency: str | None,
    age_years: int | None = None,
    weight_kg: float | None = None,
    egfr: float | None = None,
) -> list[DoseAssessment]:
    """Everything wrong with one charted dose of one drug, for one patient.

    An empty list means one of two very different things, and the caller must not conflate them:
    the dose was checked and is within range, or there was nothing checkable — no curated range,
    no readable mass, a combination product, a tablet count. ``range_for`` is what distinguishes
    them, and the service asks it separately so the chart-level "not evaluated" note can be
    written once for the whole medication list rather than once per drug.
    """
    dose_range = range_for(generic_name)
    if dose_range is None:
        return []
    amount = parse_charted_amount(dose, dose_unit)
    if amount is None:
        return []

    out: list[DoseAssessment] = []
    per_day = doses_per_day(frequency)

    if dose_range.interval == "weekly" and is_daily_frequency(frequency):
        out.append(
            DoseAssessment(
                kind="interval_mismatch",
                generic_name=generic_name,
                message=(
                    f"{generic_name} is charted at a daily frequency "
                    f"(“{(frequency or '').strip()}”). "
                    + (dose_range.note or f"{generic_name} is a once-weekly dose.")
                    + " Confirm the interval against the source."
                ),
                details={
                    "charted_frequency": (frequency or "").strip(),
                    "expected_interval": "weekly",
                    "doses_per_day": per_day,
                },
                severe=True,
            )
        )
        # A weekly drug charted daily has already produced the finding that matters; measuring
        # its individual dose against a weekly ceiling as well would put a second, weaker flag
        # under the one the clinician has to act on.
        return out

    if slip := _unit_slip(dose_range, amount, generic_name, per_day):
        return [slip]

    if amount.milligrams > dose_range.single_max_mg:
        excess = amount.milligrams / dose_range.single_max_mg
        out.append(
            DoseAssessment(
                kind="above_maximum",
                generic_name=generic_name,
                message=(
                    f"A single dose of {_quantity(amount.milligrams, dose_range.dosing_unit)} "
                    f"{generic_name} is above the usual maximum single dose of "
                    f"{_quantity(dose_range.single_max_mg, dose_range.dosing_unit)}. "
                    "Confirm the intended dose."
                ),
                details={
                    "charted_single_dose_mg": amount.milligrams,
                    "maximum_single_dose_mg": dose_range.single_max_mg,
                    "basis": "single dose",
                },
                severe=excess >= CRITICAL_EXCESS_MULTIPLE,
            )
        )

    paediatric = _paediatric(dose_range, amount, generic_name, per_day, age_years, weight_kg)
    if paediatric is not None:
        out.append(paediatric)
        return out
    if age_years is not None and age_years < PAEDIATRIC_MAX_AGE_YEARS:
        # A child on a drug with no curated weight-based range: the adult daily ceiling below is
        # not this patient's ceiling, so it is not applied. The single-dose finding above still
        # stands — it is an upper bound for any body — and ``check_paediatric_cautions`` speaks
        # separately to whether the drug suits a child at all.
        return out

    if per_day is None or dose_range.interval == "weekly":
        # A weekly drug has no daily total to compare against anything: 15 mg once a week is
        # 2.14 mg "a day", which is below every figure in its own range and means nothing. The
        # single-dose ceiling above *is* its weekly ceiling, and the interval check above is
        # what catches the error that actually happens to it.
        return out
    daily_total = amount.milligrams * per_day
    ceiling, reason = _applicable_daily_max(dose_range, egfr=egfr, age_years=age_years)
    if daily_total > ceiling:
        out.append(
            DoseAssessment(
                kind="above_maximum",
                generic_name=generic_name,
                message=(
                    f"{generic_name} totals "
                    f"{_quantity(daily_total, dose_range.dosing_unit)} a day as charted "
                    f"({_quantity(amount.milligrams, dose_range.dosing_unit)} × "
                    f"{_trim(per_day)}), above {reason} of "
                    f"{_quantity(ceiling, dose_range.dosing_unit)} a day. "
                    "Confirm the intended dose."
                    + (f" {dose_range.note}" if dose_range.note else "")
                ),
                details={
                    "charted_daily_total_mg": daily_total,
                    "maximum_daily_mg": ceiling,
                    "ceiling_basis": reason,
                    "doses_per_day": per_day,
                    "egfr_applied": egfr is not None and bool(dose_range.renal_daily_max_mg),
                    "basis": "daily total",
                },
                severe=daily_total / ceiling >= CRITICAL_EXCESS_MULTIPLE,
            )
        )
    elif dose_range.daily_min_mg is not None and daily_total < dose_range.daily_min_mg:
        out.append(
            DoseAssessment(
                kind="below_minimum",
                generic_name=generic_name,
                message=(
                    f"{generic_name} totals "
                    f"{_quantity(daily_total, dose_range.dosing_unit)} a day as charted, below "
                    f"the usual minimum effective dose of "
                    f"{_quantity(dose_range.daily_min_mg, dose_range.dosing_unit)} a day. This "
                    "may be intentional — a starting dose, or a deliberate reduction — and is "
                    "noted rather than questioned."
                ),
                details={
                    "charted_daily_total_mg": daily_total,
                    "minimum_daily_mg": dose_range.daily_min_mg,
                    "doses_per_day": per_day,
                    "basis": "daily total",
                },
            )
        )
    return out


def _unit_slip(
    dose_range: DoseRange, amount: ChartedAmount, generic_name: str, per_day: float | None
) -> DoseAssessment | None:
    """The mcg/mg slip, detected by a test more specific than "the number is large".

    A dose is a slipped unit when it is outside the range as charted **and** the identical
    number, read in the unit the drug is actually dosed in, lands inside it. "Levothyroxine 100
    mg" fails the first test by 1000x and passes the second exactly — 100 mcg is the commonest
    levothyroxine dose there is. A genuine 100-fold overdose of a milligram-dosed drug fails
    both, and is reported as the overdose it is rather than as a typing error nobody made.

    Requiring both halves is what keeps this quiet. A number that is merely large gets the
    ordinary above-maximum flag, which does not tell the clinician to go and check the unit.
    """
    if amount.unit == dose_range.dosing_unit:
        return None
    reinterpreted = amount.amount * TO_MILLIGRAMS[dose_range.dosing_unit]
    # ``daily_min_mg`` is the floor used at both ends here, including for the single-dose
    # comparison. It is the smallest amount of this drug that does anything over a whole day, so
    # a *single* dose beneath it is beneath any dose there is — which is the only claim the slip
    # test needs, and a per-dose floor would be a second curated number for no extra precision.
    charted_out_of_range = amount.milligrams > dose_range.single_max_mg or (
        dose_range.daily_min_mg is not None and amount.milligrams < dose_range.daily_min_mg
    )
    if not charted_out_of_range:
        return None
    if reinterpreted > dose_range.single_max_mg:
        return None
    if dose_range.daily_min_mg is not None and reinterpreted < dose_range.daily_min_mg:
        return None
    direction = "above" if amount.milligrams > dose_range.single_max_mg else "below"
    return DoseAssessment(
        kind="unit_mismatch",
        generic_name=generic_name,
        message=(
            f"{generic_name} is charted as {_trim(amount.amount)} {amount.unit}, which is "
            f"{direction} any dose of it, while {_trim(amount.amount)} "
            f"{dose_range.dosing_unit} is an ordinary one — so the unit on this line may be "
            f"wrong. {generic_name} is dosed in {dose_range.dosing_unit}. Confirm the unit "
            "against the source before acting on it."
            + (f" {dose_range.note}" if dose_range.note else "")
        ),
        details={
            "charted_amount": amount.amount,
            "charted_unit": amount.unit,
            "expected_unit": dose_range.dosing_unit,
            "charted_single_dose_mg": amount.milligrams,
            "maximum_single_dose_mg": dose_range.single_max_mg,
            "doses_per_day": per_day,
        },
        severe=True,
    )


def _paediatric(
    dose_range: DoseRange,
    amount: ChartedAmount,
    generic_name: str,
    per_day: float | None,
    age_years: int | None,
    weight_kg: float | None,
) -> DoseAssessment | None:
    """The weight-based judgement for a child, or the statement that it could not be made."""
    if age_years is None or age_years >= PAEDIATRIC_MAX_AGE_YEARS:
        return None
    band = dose_range.paediatric_mg_per_kg_per_day
    if band is None:
        return None
    if weight_kg is None or weight_kg <= 0:
        return DoseAssessment(
            kind="not_evaluated",
            generic_name=generic_name,
            message=(
                f"{generic_name} is charted for a patient aged {age_years}, whose dose is "
                "calculated from body weight — and this chart records no weight, so the dose "
                "could not be checked against the paediatric range. This is not the same as a "
                "dose within range. Record a weight to have it checked."
            ),
            details={
                "age_years": age_years,
                "paediatric_mg_per_kg_per_day": list(band),
                "evaluated": False,
            },
        )
    if per_day is None:
        return None
    per_kg_per_day = amount.milligrams * per_day / weight_kg
    _, maximum = band
    if per_kg_per_day <= maximum:
        return None
    return DoseAssessment(
        kind="above_maximum",
        generic_name=generic_name,
        message=(
            f"{generic_name} totals {_trim(per_kg_per_day)} mg/kg a day for this patient "
            f"({_quantity(amount.milligrams * per_day, dose_range.dosing_unit)} at "
            f"{_trim(weight_kg)} kg, aged {age_years}), above the paediatric maximum of "
            f"{_trim(maximum)} mg/kg a day. Confirm the intended dose."
        ),
        details={
            "charted_mg_per_kg_per_day": per_kg_per_day,
            "maximum_mg_per_kg_per_day": maximum,
            "weight_kg": weight_kg,
            "age_years": age_years,
            "doses_per_day": per_day,
            "basis": "weight-based paediatric daily total",
        },
        severe=per_kg_per_day / maximum >= CRITICAL_EXCESS_MULTIPLE,
    )
