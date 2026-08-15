"""Dose expressions named in clinical prose, and whether they can be believed.

The engine screens the drugs a management option *names* against the patient
(``SafetyService.screen_text``) by resolving whole names through the DrugVocabulary. That answers
"does this recommendation conflict with this chart". It cannot answer the question this module
exists for: **is the recommendation about a real drug, at a real dose, at all.**

The option text is model-written. It arrives as fluent, schema-conforming prose from whichever
provider is configured — in this deployment often a small free OpenRouter model — and two of its
failure modes are silent all the way to the screen:

* **A drug name that does not exist.** "Cardizemol 40 mg twice daily" resolves to nothing, so
  ``rows_named_in`` returns an empty mapping, so ``screen_text`` returns no flags, so synthesis
  marks the option unconflicted and presents it at the case autonomy tier — carrying a guideline
  citation, beside options that really were checked. An empty flag list means "checked, and
  fine" everywhere else in this product; here it meant "there was nothing we could check". This
  module is the third time that distinction has had to be made explicit (see
  ``check_unevaluated_medications`` and ``check_unevaluated_allergies`` in ``app.core.safety``),
  and it is the first time the unreadable thing was written by us rather than read off a scan.
* **A dose off by a factor of a thousand.** Levothyroxine is dispensed in micrograms and dosed in
  micrograms; "Levothyroxine 50 mg" is a fluent sentence, a plausible-looking number, and 500x
  the largest strength the product is made in. The mcg/mg slip is the classic one, and a
  misplaced decimal is the other.

Both checks are deterministic, offline and pure — Critical Safety Rule #8 — and both are
deliberately built to *under*-report. The cost of a false positive here is a correct guideline
recommendation escalated to flag-for-review, which is how a tier stops being read; so the
detector answers only for the canonical prescription shape (a name immediately followed by a
dose) and stays silent whenever the sentence is arranged any other way.

What is NOT attempted
---------------------
No judgement about whether a dose is *right* for this patient. That needs indication, weight,
renal function and a curated per-drug maximum, and the parts of that this system has are already
applied by the renal, hepatic and geriatric rules. What is judged here is narrower and needs no
curation: whether the number could be a dose of that product *at all*, measured against the
strengths the vocabulary lists it in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# How many times the largest strength a product is listed in a dose has to be before the number
# stops being a dose of that product and becomes a typing error. Deliberately enormous. Real
# prescribing exceeds a single tablet's strength routinely — a 4 g paracetamol day is four 1 g
# doses, a 5 g magnesium load is one — and a ceiling tight enough to catch those would fire on
# correct guideline text every day. At 100x nothing therapeutic reaches it and both hallucination
# shapes overshoot it: a mcg/mg confusion is 1000x, a misplaced decimal at least 10x per place.
IMPLAUSIBLE_STRENGTH_MULTIPLE = 100.0

# The ceiling that applies with no product to measure against — a drug the vocabulary has no
# mass strength for, or none at all. 100 g in one administration is above every oral and
# parenteral dose in use: activated charcoal tops out around 50 g and dextrose 25 g, which are
# the largest single doses in ordinary practice.
ABSOLUTE_MAX_SINGLE_DOSE_MG = 100_000.0

_TO_MILLIGRAMS: dict[str, float] = {
    "mcg": 0.001,
    "mg": 1.0,
    "g": 1000.0,
    "kg": 1_000_000.0,
}

# Spelling -> canonical unit. Longest spellings must be tried first in the alternation below, so
# "micrograms" is not matched as "mg" would never be, but "milligrams" is not truncated to "mill".
_UNIT_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("micrograms", "mcg"),
    ("microgram", "mcg"),
    ("milligrams", "mg"),
    ("milligram", "mg"),
    ("kilograms", "kg"),
    ("kilogram", "kg"),
    ("grams", "g"),
    ("gram", "g"),
    ("units", "iu"),
    ("unit", "iu"),
    ("mcg", "mcg"),
    ("µg", "mcg"),  # micro sign
    ("μg", "mcg"),  # Greek small letter mu — both reach us from PDFs
    ("ug", "mcg"),
    ("mg", "mg"),
    ("gm", "g"),
    ("kg", "kg"),
    ("iu", "iu"),
    ("g", "g"),
)

_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_UNIT_ALTERNATION = "|".join(re.escape(spelling) for spelling, _ in _UNIT_SPELLINGS)

_DOSE_RE = re.compile(
    rf"(?P<amount>{_NUMBER})"
    # A range ("500-1000 mg", "1 to 2 g") is one dose expression, judged at its upper end.
    rf"(?:\s*(?:-|–|—|to)\s*(?P<upper>{_NUMBER}))?"
    rf"\s*(?P<unit>{_UNIT_ALTERNATION})"
    # A plural "s" is part of the unit; any other letter means this was never a unit. "10 mgs"
    # is a dose, "5 gastric" is not 5 g, and "10 mgx" is not a unit this recognises.
    r"s?(?![A-Za-z])"
    # The denominator decides whether this is a dose at all; see ``_PER_UNITS``.
    r"(?:\s*/\s*(?P<denominator>[A-Za-z0-9.]+))?",
    re.IGNORECASE,
)

# Denominators that leave the number a dose. "mg/kg" is a weight-based dose (kept, but never
# compared against a product strength — the two are not the same kind of number); "g/day" is a
# daily total, which is still a quantity of drug administered.
_PER_BODY_WEIGHT = frozenset({"kg", "kilogram", "kilograms"})
_PER_TIME = frozenset({"day", "d", "24h", "24hr", "24hrs", "hour", "hr", "h", "week", "wk", "dose"})

# Every other denominator — /dL, /L, /mL, /cumm, /1.73m2 — makes the number a concentration, a
# rate or a laboratory value rather than an amount administered. "Creatinine 1.5 mg/dL" is the
# case that matters: a dose expression by shape and a lab result by meaning, in the same
# paragraph as a real recommendation. They are excluded by falling through, not by being listed,
# so a denominator nobody anticipated is read as "meaning unknown" rather than as a dose.

# The word immediately before a number, when it is one of these, is not a drug name. Kept to
# words that genuinely precede doses in clinical prose; a word missing from this list costs a
# flag on an option that named no drug, which is why the flag also requires the surrounding
# clause to name no *known* drug before it fires.
_NOT_A_DRUG_NAME = frozenset(
    {
        "about",
        "above",
        "additional",
        "adjust",
        "adjusted",
        "another",
        "approximately",
        "around",
        "below",
        "beyond",
        "capsule",
        "capsules",
        "consider",
        "considering",
        "containing",
        "contains",
        "daily",
        "divided",
        "dosage",
        "dose",
        "dosed",
        "doses",
        "each",
        "equivalent",
        "every",
        "exceed",
        "exceeding",
        "exceeds",
        "extra",
        "first",
        "further",
        "give",
        "given",
        "gives",
        "giving",
        "increase",
        "increased",
        "inhaler",
        "initial",
        "injection",
        "intravenous",
        "least",
        "less",
        "level",
        "levels",
        "loading",
        "maintenance",
        "maximum",
        "minimum",
        "more",
        "most",
        "nearly",
        "oral",
        "orally",
        "over",
        "receive",
        "received",
        "reduce",
        "reduced",
        "sachet",
        "second",
        "single",
        "start",
        "started",
        "starting",
        "strength",
        "syrup",
        "tablet",
        "tablets",
        "take",
        "taken",
        "takes",
        "taking",
        "target",
        "than",
        "then",
        "third",
        "titrate",
        "titrated",
        "titrating",
        "total",
        "under",
        "until",
        "upto",
        "value",
        "values",
        "weight",
        "with",
        "without",
    }
)

# A sentence ends the window a drug name may anchor a dose from. A drug named in the previous
# sentence has not been named in this one, and the whole point of the window is locality.
_CLAUSE_BREAK_RE = re.compile(r"[.;:!?](?=\s)|\n")

# How far after a dose to keep looking for the drug it belongs to. "500 mg of paracetamol" and
# "500 mg paracetamol orally" are both ordinary; four words is enough for either and short
# enough that the next recommendation in the sentence is not read as this one's subject.
_TRAILING_WINDOW_WORDS = 4

_NAME_BEFORE_RE = re.compile(r"([A-Za-z][A-Za-z'’\-]{3,})[\s(\[–-]*$")


@dataclass(frozen=True)
class DoseMention:
    """One "<amount> <unit>" expression found in prose, with the text around it."""

    text: str
    """The dose exactly as written, for quoting back to the clinician."""

    amount: float
    """The upper end of the expression: a range is judged at its largest."""

    unit: str
    """Canonical unit — ``mg``, ``mcg``, ``g``, ``kg`` or ``iu``."""

    milligrams: float | None
    """``amount`` in milligrams, or None for a unit that is not a mass (``iu``)."""

    per_body_weight: bool
    """True for "mg/kg": a dose, but not one comparable to a product's strength."""

    context: str
    """The clause the dose sits in, bounded by sentence breaks and by neighbouring doses.

    This is what is asked to name a drug. Bounding it at the previous dose is what lets
    "Paracetamol 500 mg and Cardizemol 40 mg" report the second name and not the first.
    """

    name_candidate: str
    """The word immediately before the amount, when it could be a drug name; "" otherwise.

    Adjacency is the precision guard. Anything else — "titrate to 500 mg", "a dose of 2 g" —
    leaves this empty and raises nothing, because a sentence not in prescription form is not
    evidence that a drug name was invented.
    """


@dataclass(frozen=True)
class DoseFinding:
    """A dose mention after the vocabulary has been consulted about it."""

    mention: DoseMention
    drug: str | None
    """The generic name the clause resolved to, or None if it named no drug this system knows."""

    reason: str | None
    """Why the amount cannot be a dose of ``drug``, or None if it is plausible."""

    ceiling_text: str | None = None
    """The strength the ceiling was derived from, quoted in the flag so it can be checked."""


def _to_float(raw: str) -> float:
    return float(raw.replace(",", ""))


def _canonical_unit(raw: str) -> str:
    lowered = raw.lower()
    for spelling, canonical in _UNIT_SPELLINGS:
        if lowered == spelling.lower():
            return canonical
    return lowered


def find_dose_mentions(text: str) -> list[DoseMention]:
    """Every dose expression in ``text``, in the order they appear.

    Laboratory values are excluded by their denominator rather than by their name: this runs over
    guideline prose, which reports a creatinine in mg/dL in the same paragraph that recommends a
    drug in mg, and no list of analyte names would keep up with what a model might mention.
    """
    if not text or not text.strip():
        return []

    raw: list[tuple[re.Match[str], str, float, bool]] = []
    for match in _DOSE_RE.finditer(text):
        denominator = (match.group("denominator") or "").lower().rstrip(".")
        per_body_weight = False
        if denominator:
            if denominator in _PER_BODY_WEIGHT:
                per_body_weight = True
            elif denominator not in _PER_TIME:
                # Includes every unit in ``_NOT_A_DOSE_DENOMINATOR`` and, deliberately, anything
                # else unrecognised: a denominator this module cannot read makes the number's
                # meaning unknown, and an unknown number is not evidence of anything.
                continue
        unit = _canonical_unit(match.group("unit"))
        amount = _to_float(match.group("upper") or match.group("amount"))
        raw.append((match, unit, amount, per_body_weight))

    mentions: list[DoseMention] = []
    for index, (match, unit, amount, per_body_weight) in enumerate(raw):
        previous_end = raw[index - 1][0].end() if index else 0
        next_start = raw[index + 1][0].start() if index + 1 < len(raw) else len(text)
        before = text[_clause_start(text, match.start(), previous_end) : match.start()]
        after = _trailing_window(text[match.end() : next_start])
        candidate = _NAME_BEFORE_RE.search(before)
        name = candidate.group(1) if candidate else ""
        if name.lower() in _NOT_A_DRUG_NAME:
            name = ""
        mentions.append(
            DoseMention(
                text=match.group(0).strip(),
                amount=amount,
                unit=unit,
                milligrams=(amount * _TO_MILLIGRAMS[unit] if unit in _TO_MILLIGRAMS else None),
                per_body_weight=per_body_weight,
                context=(before + match.group(0) + after),
                name_candidate=name,
            )
        )
    return mentions


def _clause_start(text: str, start: int, floor: int) -> int:
    """Where the dose's own clause begins: after the last sentence break, never before ``floor``."""
    last = floor
    for match in _CLAUSE_BREAK_RE.finditer(text, floor, start):
        last = match.end()
    return last


def _trailing_window(tail: str) -> str:
    """``tail`` truncated at the first sentence break, then to a few words."""
    if break_match := _CLAUSE_BREAK_RE.search(tail):
        tail = tail[: break_match.start()]
    words = tail.split()
    return " " + " ".join(words[:_TRAILING_WINDOW_WORDS]) if words else ""


_STRENGTH_COMPONENT_RE = re.compile(
    rf"(?P<amount>{_NUMBER})\s*(?P<unit>{_UNIT_ALTERNATION})s?(?![A-Za-z])",
    re.IGNORECASE,
)


def max_milligrams_in_strength(strength: str | None) -> float | None:
    """The largest mass component of a vocabulary ``strength``, in milligrams.

    The field is a product label, not a parsed quantity: "500mg", "500mg+1mg", "6/200mcg",
    "160mg+800mg", "100U/mL". The largest mass in it is what a dose of the product is measured
    against — for a combination that is its heaviest ingredient, which is the conservative
    reading because it makes the ceiling the highest of the available ones.

    Returns None when the strength carries no mass at all (an insulin in U/mL, a contrast medium
    with no strength recorded). There is then nothing to compare against, and
    :func:`implausible_dose_reason` falls back to the absolute ceiling rather than guessing.
    """
    if not strength:
        return None
    milligrams = [
        _to_float(match.group("amount")) * _TO_MILLIGRAMS[unit]
        for match in _STRENGTH_COMPONENT_RE.finditer(strength)
        if (unit := _canonical_unit(match.group("unit"))) in _TO_MILLIGRAMS
    ]
    return max(milligrams) if milligrams else None


def implausible_dose_reason(mention: DoseMention, ceiling_milligrams: float | None) -> str | None:
    """Why ``mention`` cannot be a dose of a drug with that largest strength, or None.

    ``ceiling_milligrams`` is the product's own largest listed strength (see
    :func:`max_milligrams_in_strength`); None means the vocabulary has no mass to compare
    against and only the absolute ceiling applies.
    """
    if mention.amount <= 0:
        return "the amount is zero"
    if mention.unit == "kg":
        return "a dose is not measured in kilograms"
    if mention.milligrams is None:
        # International units are drug-specific by definition — 100 IU of insulin and 100 IU of
        # vitamin D are not comparable quantities — so there is no unit-independent ceiling to
        # apply and this reports nothing rather than guessing.
        return None
    if mention.per_body_weight:
        # A mg/kg figure is a rate, not an amount; comparing it to a tablet's strength would
        # flag every correct paediatric dose in the corpus.
        return None
    if mention.milligrams > ABSOLUTE_MAX_SINGLE_DOSE_MG:
        return (
            f"{_quantity(mention.milligrams)} is more than "
            f"{_quantity(ABSOLUTE_MAX_SINGLE_DOSE_MG)}, which no drug is administered in"
        )
    ceiling = (ceiling_milligrams or 0.0) * IMPLAUSIBLE_STRENGTH_MULTIPLE
    if ceiling_milligrams and mention.milligrams > ceiling:
        return (
            f"{_quantity(mention.milligrams)} is over "
            f"{int(IMPLAUSIBLE_STRENGTH_MULTIPLE)} times the largest strength this drug is "
            f"listed in ({_quantity(ceiling_milligrams)})"
        )
    return None


def _quantity(milligrams: float) -> str:
    """A milligram figure written in the unit a clinician would read it in."""
    if milligrams >= 1000:
        return f"{_trim(milligrams / 1000)} g"
    if milligrams < 1:
        return f"{_trim(milligrams * 1000)} mcg"
    return f"{_trim(milligrams)} mg"


def _trim(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")
