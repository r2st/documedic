"""Hepatic severity, computed from what the chart measures — and bounded where it cannot be.

Two scores, and they answer different questions.

**Child-Pugh** is the one the hepatic dosing literature is written against: almost every "reduce
the dose in moderate hepatic impairment" on a drug label means Child-Pugh B. It takes five
components, and only three of them are laboratory values. Ascites and encephalopathy are graded
by a clinician at the bedside, and this record holds neither as structured data. That is why
``app.core.safety.HepaticPanel`` deliberately stopped short of a class: a class inferred from
labs alone is the confident-wrong number this engine refuses everywhere else.

What it does not follow is that nothing can be said. The two clinical components contribute
between 2 and 6 points, whatever they turn out to be, so three lab values put the total inside a
window five points wide — and for a sick enough liver that window lies entirely inside one class.
A patient with bilirubin above 3, albumin below 2.8 and an INR above 1.7 scores at least 10
before anyone has looked for ascites, and 10 is Child-Pugh C by any grading. So this module
reports the *bounds*, and names a class only when the bounds agree. Anything less specific is
reported as what it is — "B or C, pending the bedside grading" — which is a fact about this
patient rather than a hedge.

**MELD** is entirely lab-derived (bilirubin, INR, serum creatinine), so it needs nothing a
clinician has to grade. It is a mortality/transplant-priority index, NOT a dosing instrument:
nothing here should be read as "reduce the dose because MELD is 22". It is carried because it is
the number a hepatology referral is written in, and because it is computable without guessing.

Everything here is a pure function over floats — no database, no LLM, no network. Critical
Safety Rule #8: the hepatic picture has to be available when the network is not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Child-Pugh component thresholds, as the score is defined. Each component scores 1, 2 or 3.
#
# Written as (upper bound of the 1-point band, upper bound of the 2-point band) for the markers
# that score upward with severity, and handled separately for albumin, which scores downward.
_BILIRUBIN_BANDS = (2.0, 3.0)  # mg/dL: <2 = 1, 2-3 = 2, >3 = 3
_INR_BANDS = (1.7, 2.3)  # <1.7 = 1, 1.7-2.3 = 2, >2.3 = 3
_ALBUMIN_BANDS = (3.5, 2.8)  # g/dL: >3.5 = 1, 2.8-3.5 = 2, <2.8 = 3

# The two components this record cannot supply — ascites and encephalopathy — score 1, 2 or 3
# each, so they contribute somewhere in this range no matter how they are graded.
CLINICAL_COMPONENTS = ("ascites", "encephalopathy")
_CLINICAL_MIN = len(CLINICAL_COMPONENTS)  # every component scores at least 1
_CLINICAL_MAX = 3 * len(CLINICAL_COMPONENTS)

# The three lab components, in the order a report prints them, with the unit each is expected in.
CHILD_PUGH_LAB_INPUTS: tuple[tuple[str, str, str], ...] = (
    ("bilirubin_mg_dl", "total bilirubin", "mg/dL"),
    ("albumin_g_dl", "serum albumin", "g/dL"),
    ("inr", "INR", "ratio"),
)

# MELD needs a serum creatinine on top of two of the Child-Pugh labs.
MELD_INPUTS: tuple[tuple[str, str, str], ...] = (
    ("bilirubin_mg_dl", "total bilirubin", "mg/dL"),
    ("inr", "INR", "ratio"),
    ("creatinine_mg_dl", "serum creatinine", "mg/dL"),
)


def _class_for(total: int) -> str:
    """Child-Pugh class for a total score. A 5-6, B 7-9, C 10-15."""
    if total <= 6:
        return "A"
    if total <= 9:
        return "B"
    return "C"


def _bilirubin_points(value: float) -> int:
    low, high = _BILIRUBIN_BANDS
    return 1 if value < low else (2 if value <= high else 3)


def _inr_points(value: float) -> int:
    low, high = _INR_BANDS
    return 1 if value < low else (2 if value <= high else 3)


def _albumin_points(value: float) -> int:
    """Albumin runs the other way: a *falling* albumin is a worsening liver."""
    high, low = _ALBUMIN_BANDS
    return 1 if value > high else (2 if value >= low else 3)


@dataclass(frozen=True)
class ChildPughAssessment:
    """What three lab values establish about a Child-Pugh class, and what they leave open.

    ``child_pugh_class`` is set only when ``min_class == max_class`` — that is, only when the
    ungraded clinical components cannot move the patient out of the class the labs already put
    them in. Otherwise it is None and the range says what is actually known. A caller that wants
    one letter and treats None as "not sick" has misread this: None means *undetermined*, and
    ``max_class`` is the worst case the labs are consistent with.
    """

    lab_points: int
    component_points: dict[str, int]
    min_total: int
    max_total: int
    min_class: str
    max_class: str
    child_pugh_class: str | None
    # Named so a summary can say what is missing without the caller re-deriving it.
    ungraded_components: tuple[str, ...] = CLINICAL_COMPONENTS

    @property
    def is_determinate(self) -> bool:
        return self.child_pugh_class is not None

    def as_details(self) -> dict:
        """The shape this goes into a ``SafetyFlag``'s ``details`` as."""
        return {
            "lab_points": self.lab_points,
            "component_points": dict(self.component_points),
            "score_range": [self.min_total, self.max_total],
            "class_range": [self.min_class, self.max_class],
            "child_pugh_class": self.child_pugh_class,
            "ungraded_components": list(self.ungraded_components),
        }


def child_pugh_from_labs(
    *, bilirubin_mg_dl: float | None, albumin_g_dl: float | None, inr: float | None
) -> ChildPughAssessment | None:
    """The Child-Pugh window three lab values establish, or None if any of them is absent.

    None rather than a partial score on purpose. Scoring two components and calling the third
    normal would put a number in front of a clinician that no measurement supports — and it would
    understate, because the marker a chart is missing is disproportionately the one nobody
    ordered on a patient who looked well. Absent inputs are named by
    ``assess_hepatic_severity`` so the gap is reported rather than filled.
    """
    if bilirubin_mg_dl is None or albumin_g_dl is None or inr is None:
        return None

    points = {
        "bilirubin": _bilirubin_points(bilirubin_mg_dl),
        "albumin": _albumin_points(albumin_g_dl),
        "inr": _inr_points(inr),
    }
    lab_points = sum(points.values())
    min_total = lab_points + _CLINICAL_MIN
    max_total = lab_points + _CLINICAL_MAX
    min_class = _class_for(min_total)
    max_class = _class_for(max_total)
    return ChildPughAssessment(
        lab_points=lab_points,
        component_points=points,
        min_total=min_total,
        max_total=max_total,
        min_class=min_class,
        max_class=max_class,
        child_pugh_class=min_class if min_class == max_class else None,
    )


# MELD clamps every input at 1.0 from below, because the formula is a sum of logarithms and a
# value below 1 contributes a negative term — a healthy bilirubin of 0.4 would otherwise *lower*
# the score by more than a raised INR raises it. Creatinine is additionally capped at 4.0, above
# which the score is no longer measuring the liver.
_MELD_FLOOR = 1.0
_MELD_CREATININE_CAP = 4.0
_MELD_MAX = 40


@dataclass(frozen=True)
class MeldScore:
    """A MELD score and the inputs it was actually computed from.

    ``clamped`` names every input the formula moved before using it. Without it the score is
    unanswerable: a patient with a creatinine of 9 and one with a creatinine of 4 produce the
    same number, and a clinician looking at 31 has no way to tell which chart they are reading.
    """

    score: int
    inputs: dict[str, float]
    clamped: tuple[str, ...] = ()

    def as_details(self) -> dict:
        return {
            "meld": self.score,
            "meld_inputs": dict(self.inputs),
            "meld_clamped_inputs": list(self.clamped),
        }


def meld_score(
    *,
    bilirubin_mg_dl: float | None,
    inr: float | None,
    creatinine_mg_dl: float | None,
    on_dialysis: bool = False,
) -> MeldScore | None:
    """The UNOS MELD score, or None if any of its three inputs is absent from the chart.

    ``MELD = 3.78·ln(bilirubin) + 11.2·ln(INR) + 9.57·ln(creatinine) + 6.43``, with every input
    floored at 1.0, creatinine capped at 4.0, and the result capped at 40. Dialysis twice in the
    past week substitutes a creatinine of 4.0 by definition — the parameter exists so a caller
    that knows this can say so, and defaults to False rather than being inferred, because
    inferring dialysis from a chart is not something this module can do honestly.

    Not a dosing instrument. See the module docstring.
    """
    if bilirubin_mg_dl is None or inr is None or creatinine_mg_dl is None:
        return None

    clamped: list[str] = []
    creatinine = creatinine_mg_dl
    if on_dialysis:
        creatinine = _MELD_CREATININE_CAP
        clamped.append("creatinine (dialysis)")
    elif creatinine > _MELD_CREATININE_CAP:
        creatinine = _MELD_CREATININE_CAP
        clamped.append("creatinine (capped at 4.0)")

    values = {"bilirubin": bilirubin_mg_dl, "inr": inr, "creatinine": creatinine}
    for name, value in list(values.items()):
        if value < _MELD_FLOOR:
            values[name] = _MELD_FLOOR
            clamped.append(f"{name} (floored at 1.0)")

    raw = (
        3.78 * math.log(values["bilirubin"])
        + 11.2 * math.log(values["inr"])
        + 9.57 * math.log(values["creatinine"])
        + 6.43
    )
    # Banker's rounding would send a raw 6.5 to 6; MELD is reported as a conventional
    # round-half-up integer, and the half-point differences matter at the allocation boundaries.
    score = min(_MELD_MAX, int(math.floor(raw + 0.5)))
    return MeldScore(
        score=score,
        inputs={k: round(v, 2) for k, v in values.items()},
        clamped=tuple(clamped),
    )


@dataclass(frozen=True)
class HepaticSeverity:
    """The whole hepatic picture a chart supports: a Child-Pugh window, a MELD, and the gaps.

    Falsy when neither score was computable, so ``if severity:`` reads as "the chart establishes
    something about this liver" — the same convention ``HepaticPanel`` uses for "the chart
    measures this liver at all". The two are not the same question: a chart with a bilirubin and
    nothing else has a panel and no severity.
    """

    child_pugh: ChildPughAssessment | None = None
    meld: MeldScore | None = None
    # The named inputs each score wanted and the chart did not carry. Populated even when the
    # score came out, because MELD and Child-Pugh want overlapping-but-different inputs and
    # "MELD 18, Child-Pugh not computable — no albumin" is the useful thing to be able to say.
    missing_child_pugh: tuple[str, ...] = ()
    missing_meld: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.child_pugh is not None or self.meld is not None

    def as_details(self) -> dict:
        details: dict = {}
        if self.child_pugh is not None:
            details.update(self.child_pugh.as_details())
        if self.meld is not None:
            details.update(self.meld.as_details())
        if self.missing_child_pugh:
            details["child_pugh_missing_inputs"] = list(self.missing_child_pugh)
        if self.missing_meld:
            details["meld_missing_inputs"] = list(self.missing_meld)
        return details


def assess_hepatic_severity(
    *,
    bilirubin_mg_dl: float | None,
    albumin_g_dl: float | None,
    inr: float | None,
    creatinine_mg_dl: float | None,
    on_dialysis: bool = False,
) -> HepaticSeverity:
    """Both scores over whatever the chart carries, with each score's own gaps named.

    Never raises and never partially guesses: an input that is absent removes the score that
    needs it and appears in that score's ``missing_*`` list. A chart with nothing on it produces
    a falsy result carrying the full list of what it would have taken.
    """
    supplied = {
        "bilirubin_mg_dl": bilirubin_mg_dl,
        "albumin_g_dl": albumin_g_dl,
        "inr": inr,
        "creatinine_mg_dl": creatinine_mg_dl,
    }
    return HepaticSeverity(
        child_pugh=child_pugh_from_labs(
            bilirubin_mg_dl=bilirubin_mg_dl, albumin_g_dl=albumin_g_dl, inr=inr
        ),
        meld=meld_score(
            bilirubin_mg_dl=bilirubin_mg_dl,
            inr=inr,
            creatinine_mg_dl=creatinine_mg_dl,
            on_dialysis=on_dialysis,
        ),
        missing_child_pugh=tuple(
            label for key, label, _u in CHILD_PUGH_LAB_INPUTS if supplied[key] is None
        ),
        missing_meld=tuple(label for key, label, _u in MELD_INPUTS if supplied[key] is None),
    )
