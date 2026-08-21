"""What a patient may see of their own record, and — the part that matters — what they may not
see *yet*.

A patient's right to their own record is not in question; DPDP grants it and this product should
make it easy. The hard part is timing and framing, and both are clinical safety questions rather
than access-control ones.

**Timing.** A potassium of 6.9 extracted from an overnight report is a result that needs a phone
call, not a web page. If the portal shows every result the moment it is ingested, the patient
learns of a life-threatening value alone, at 3am, from a number with an arrow next to it — before
any clinician has seen it. Every real results-release policy embargoes exactly this class of
result, and this module implements that embargo with the machinery the codebase already has: a
critical value is withheld until a clinician has *acknowledged* it, which is the same act that
takes it off the critical-lab queue. Acknowledgement means somebody looked. Nothing else releases
it — not time passing, not the chart having been opened.

**Framing.** What is released is released as data, never as interpretation. A reference interval
and a high/low marker are facts the report itself carries. "Your kidney function is declining" is
a clinical conclusion, and this product's whole design says a clinician makes those (Critical
Safety Rule #4). So the portal carries values, units, ranges and dates, and carries no
differential, no reasoning output, no devil's-advocate dissent and no clinician's notes.

**What is absent by construction.** Diagnoses are not in the portal at all. The failure mode is
specific and severe: a condition merged from an uploaded report — "carcinoma", "HIV" — appearing
on a patient's phone before the consultation that was going to explain it. That is not a
redaction rule that could be got wrong later, because there is no diagnosis-shaped field in any
portal response model to leak into.

Pure: no clock read inside a decision (the caller passes the reference moment), no database, no
configuration. The decisions here are the kind that must be reproducible from the record they
were made against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from app.core.lab_safety import evaluate_critical_value

# Why a result is being withheld. The patient sees a neutral form of this; the clinician's own
# view of the chart is unaffected and shows the result normally.
WithholdReason = Literal["awaiting_clinician_review"]

# What the patient is told in place of a withheld result. Deliberately says that a result exists
# and is being reviewed rather than hiding its existence: a portal that silently omitted it
# would be a portal a patient could not trust, and "there is a result your clinician is looking
# at" is both true and the thing that prompts the phone call.
WITHHELD_SUMMARY = (
    "A result from this test is with your clinical team for review. They will be in touch about "
    "it. It is not shown here until they have seen it."
)


@dataclass(frozen=True)
class PortalLabInput:
    """One lab row as the release decision sees it."""

    lab_result_id: str
    marker_name: str
    value_numeric: float | None
    value_text: str | None
    unit: str | None
    reference_low: float | None
    reference_high: float | None
    sample_date: date | None
    is_abnormal: bool | None
    # Whether a clinician has recorded that they have seen this result. The only thing that
    # releases a critical value — see the module docstring.
    acknowledged: bool


@dataclass(frozen=True)
class PortalLabDecision:
    """Whether one result is released to the patient, and what they see instead if not."""

    lab_result_id: str
    marker_name: str
    sample_date: date | None
    released: bool
    withheld_reason: WithholdReason | None = None
    withheld_summary: str | None = None
    # Populated only when ``released``. Written as an explicit field set rather than by handing
    # back the input, so that a column added to ``lab_results`` later cannot reach the patient
    # by arriving inside an object that gets serialized wholesale.
    value_numeric: float | None = None
    value_text: str | None = None
    unit: str | None = None
    reference_low: float | None = None
    reference_high: float | None = None
    is_abnormal: bool | None = None


def decide_lab_release(lab: PortalLabInput) -> PortalLabDecision:
    """Whether this result may be shown to the patient now.

    The rule: a value the deterministic critical-value screen flags is withheld until a
    clinician has acknowledged it. Everything else is released.

    Note which way the uncertainty falls. ``evaluate_critical_value`` returns nothing for a
    result it cannot place on a scale — an unreadable unit, an analyte it does not curate — and
    such a result is *released*, because withholding every result the screen cannot read would
    embargo most of a patient's record on the strength of a unit string. That is the correct
    direction here and the opposite of the direction the same function's answer is used in for
    the clinician's queue, where an unreadable row is surfaced rather than dropped. The
    asymmetry is deliberate: the clinician must be told the machine could not read something,
    and the patient must not have their ordinary cholesterol result hidden because of it.
    """
    withheld = _is_withheld(lab)
    if withheld:
        return PortalLabDecision(
            lab_result_id=lab.lab_result_id,
            marker_name=lab.marker_name,
            sample_date=lab.sample_date,
            released=False,
            withheld_reason="awaiting_clinician_review",
            withheld_summary=WITHHELD_SUMMARY,
        )
    return PortalLabDecision(
        lab_result_id=lab.lab_result_id,
        marker_name=lab.marker_name,
        sample_date=lab.sample_date,
        released=True,
        value_numeric=lab.value_numeric,
        value_text=lab.value_text,
        unit=lab.unit,
        reference_low=lab.reference_low,
        reference_high=lab.reference_high,
        is_abnormal=lab.is_abnormal,
    )


def _is_withheld(lab: PortalLabInput) -> bool:
    if lab.acknowledged:
        return False
    if lab.value_numeric is None:
        # Nothing numeric to screen. A qualitative result ("Reactive") cannot be run through
        # the critical-value tables at all, so there is no screen to embargo on. Released,
        # consistent with the unreadable case above.
        return False
    flag = evaluate_critical_value(lab.marker_name, lab.value_numeric, lab.unit)
    return flag is not None


@dataclass(frozen=True)
class PortalMedicationInput:
    """One medication event as the portal sees it."""

    medication_event_id: str
    display_name: str | None
    generic_name: str | None
    dose: str | None
    frequency: str | None
    route: str | None
    status: str
    started_on: date | None
    stopped_on: date | None


@dataclass(frozen=True)
class PortalMedication:
    """A medication as shown to the patient. No safety findings, deliberately.

    A patient's own drug-safety flags are *not* on this list, and that is a considered omission
    rather than an oversight. An interaction alert is a prompt for a prescriber to make a
    decision; shown to a patient with no prescriber attached, the reliable outcome is somebody
    stopping a medicine on their own. The list says what they are on and what it is for them to
    ask about; the checks stay where a clinician can act on them.
    """

    medication_event_id: str
    name: str
    dose: str | None
    frequency: str | None
    route: str | None
    status: str
    started_on: date | None
    stopped_on: date | None


def present_medication(event: PortalMedicationInput) -> PortalMedication:
    """One medication, named the way the patient will recognise it.

    The name shown prefers what was written on their prescription over the INN generic. A
    patient handed a strip labelled "Crocin" and shown "Paracetamol" has been shown a different
    medicine as far as they can tell, and the predictable outcome of that confusion is a double
    dose. The vocabulary resolution that maps one to the other is what the safety engine runs
    on; it is not what the patient's list should be printed from.
    """
    return PortalMedication(
        medication_event_id=event.medication_event_id,
        name=event.display_name or event.generic_name or "Unnamed medicine",
        dose=event.dose,
        frequency=event.frequency,
        route=event.route,
        status=event.status,
        started_on=event.started_on,
        stopped_on=event.stopped_on,
    )


def grant_is_live(*, expires_at: datetime, revoked_at: datetime | None, now: datetime) -> bool:
    """Whether a portal credential may still be used, at moment ``now``.

    Both conditions, and neither implies the other: ``revoked_at`` is the deliberate withdrawal
    a practice performs when a patient rings to say they lost their phone, and ``expires_at`` is
    the ceiling that makes an unremembered link stop working on its own.

    ``now`` is a parameter rather than a clock read for the reason everything in ``app.core``
    takes its moment as an argument: a decision about access must be reproducible from the row
    it was made against when somebody asks, months later, whether that link was live on the day
    the record was read.

    Both timestamps are normalised to UTC before comparison rather than trusted to arrive
    aware. They come off a ``DateTime(timezone=True)`` column, which PostgreSQL hands back with
    a tzinfo and SQLite hands back without — so an un-normalised comparison here raises
    ``TypeError`` on one dialect and works on the other. Normalising at this single chokepoint
    rather than at each call site is deliberate: a caller who forgets produces a 500 on the
    *authentication* path, in dev only, which is the failure shape most likely to be shipped.
    """
    if revoked_at is not None:
        return False
    return _as_utc(expires_at) > _as_utc(now)


def _as_utc(value: datetime) -> datetime:
    """A naive timestamp read back from SQLite, read as the UTC it was written as."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
