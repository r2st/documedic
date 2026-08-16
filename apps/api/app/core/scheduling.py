"""Deterministic scheduling arithmetic: overlap, availability, and reminder timing.

Everything a booking decision rests on is here, as pure functions over values the service
layer hands in. Nothing in this module reads a clock, a database or a setting — ``now`` is an
argument, the clinic's UTC offset is an argument, and the ceilings are arguments — for the same
reason ``app.core.safety`` is written that way: a scheduling refusal has to be reproducible
from the record months later, and a function that reads ``datetime.now()`` inside itself
produces a different answer every time it is asked about the same booking.

Three ideas do the work.

**Intervals are half-open.** ``[starts_at, ends_at)``. An appointment ending at 10:00 and one
starting at 10:00 do not overlap, which is the convention a clinic list is written in — the
alternative would refuse every back-to-back slot in a full morning.

**"Outside availability" is advice, and "no availability recorded" is a third answer.** A
provider with no recorded hours is not a provider who is never free; it is a provider nobody
has told the system about. Reporting that as "outside your working hours" would train people
to ignore the warning, and reporting it as nothing at all would let a booking claim to have
been checked against hours that do not exist. It gets its own code, and the caller decides.
Same doctrine as the safety engine's "not evaluated" flags: a comparison that could not be
attempted is never reported as one that passed.

**Reminders are computed, never delivered.** This system has no channel to send anything on —
no SMS gateway, no mail transport — and a scheduling feature that implies a reminder went out
is worse than one that does not offer reminders at all, because the clinic then stops
telephoning. :func:`due_reminders` derives, from the appointment list alone, which reminders
*fall due* in a window. It is a queue for a person or an integration to work, in exactly the
sense the critical-lab queue is, and nothing here marks one as sent.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# The lifecycle vocabulary, in one place so the check constraint, the service's transition
# table and the schema Literal cannot drift apart. Mirrored by ``AppointmentStatus`` in
# app/schemas/common.py and in packages/shared-types/src/enums.ts.
APPOINTMENT_STATUSES: tuple[str, ...] = ("scheduled", "completed", "cancelled", "no_show")

# The statuses that *occupy* a slot. Only one, and the exclusions are the interesting part:
#
# ``cancelled`` must not hold a slot, or a clinic that reschedules twice in a morning locks
# itself out of its own diary — the whole point of cancelling is to free the time.
#
# ``completed`` and ``no_show`` are both in the past by the time they are set, so they cannot
# conflict with a new booking in practice; they are excluded anyway rather than relied on to be
# historical, because a backdated booking is a real thing a clinic does (writing up a walk-in
# after the fact) and it must not be refused for colliding with the visit it *is*.
OCCUPYING_STATUSES: tuple[str, ...] = ("scheduled",)

# How the visit is conducted. Not administrative: a booking made as ``audio`` says, in advance,
# that the prescriber will not have seen the patient, which is the distinction
# ``app.core.telehealth`` turns into prescribing restrictions on the encounter that follows.
APPOINTMENT_MODALITIES: tuple[str, ...] = ("in_person", "video", "audio")

_MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class Slot:
    """A half-open interval of clinic time, ``[starts_at, ends_at)``, in UTC."""

    starts_at: datetime
    ends_at: datetime

    @property
    def duration_minutes(self) -> float:
        return (self.ends_at - self.starts_at).total_seconds() / 60.0


@dataclass(frozen=True)
class Booking:
    """An appointment already in the diary, reduced to what a conflict check needs."""

    appointment_id: str
    provider_name: str
    slot: Slot


@dataclass(frozen=True)
class Conflict:
    """One existing booking that the proposed slot runs into."""

    appointment_id: str
    provider_name: str
    starts_at: datetime
    ends_at: datetime
    overlap_minutes: float


@dataclass(frozen=True)
class AvailabilityWindow:
    """One recurring block of a provider's working week, in *clinic local* time.

    ``weekday`` is Monday=0 through Sunday=6, matching ``datetime.weekday()``. The two minute
    offsets are measured from local midnight, so 09:00–13:00 is ``(540, 780)``. Minutes rather
    than ``time`` objects because every comparison this module makes is arithmetic, and a
    ``time`` cannot be subtracted from another ``time`` without inventing a date to hang them on.
    """

    weekday: int
    start_minute: int
    end_minute: int


@dataclass(frozen=True)
class Reminder:
    """One reminder that falls due for one appointment."""

    appointment_id: str
    patient_id: str
    provider_name: str
    appointment_starts_at: datetime
    due_at: datetime
    lead_minutes: int


def normalise(moment: datetime) -> datetime:
    """A naive datetime read as UTC; an aware one converted to it.

    SQLite drops ``tzinfo`` on the round trip where PostgreSQL keeps it, so a value that came
    back from the database may be naive while one stamped in this process is aware. Comparing
    the two raises, and every function here compares them, so both are put on the same footing
    at the boundary rather than at a dozen call sites.
    """
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def overlaps(first: Slot, second: Slot) -> bool:
    """True when two half-open intervals share any time at all.

    Half-open is what makes a back-to-back list bookable: 09:30–10:00 and 10:00–10:30 touch and
    do not overlap. A zero-length interval overlaps nothing, including itself — it occupies no
    time — which is why :func:`slot_problems` refuses to create one rather than leaving it to be
    silently double-bookable.
    """
    return normalise(first.starts_at) < normalise(second.ends_at) and normalise(
        second.starts_at
    ) < normalise(first.ends_at)


def overlap_minutes(first: Slot, second: Slot) -> float:
    """How much time two intervals share, in minutes; zero when they do not overlap."""
    start = max(normalise(first.starts_at), normalise(second.starts_at))
    end = min(normalise(first.ends_at), normalise(second.ends_at))
    return max((end - start).total_seconds() / 60.0, 0.0)


def find_conflicts(
    proposed: Slot,
    provider_name: str,
    existing: Iterable[Booking],
    *,
    exclude_appointment_id: str | None = None,
) -> list[Conflict]:
    """Every booking of the *same provider* the proposed slot runs into, worst overlap first.

    Scoped to one provider by name rather than to the account, because two clinicians in one
    practice seeing two patients at eleven o'clock is a normal Tuesday and refusing it would
    make the feature unusable. Matching is on the trimmed, case-folded name for the same reason
    the drug vocabulary folds names: "Dr Priya Nair" and "dr priya nair" are one person, and a
    conflict check that misses because of a capital letter is a conflict check that is not
    there.

    ``exclude_appointment_id`` is how a *reschedule* asks the question. Without it an
    appointment being moved by ten minutes always conflicts with itself, and the only way past
    that would be to cancel first — which leaves a real gap in the diary that another booking
    can take.
    """
    key = _fold(provider_name)
    conflicts = [
        Conflict(
            appointment_id=booking.appointment_id,
            provider_name=booking.provider_name,
            starts_at=normalise(booking.slot.starts_at),
            ends_at=normalise(booking.slot.ends_at),
            overlap_minutes=overlap_minutes(proposed, booking.slot),
        )
        for booking in existing
        if booking.appointment_id != exclude_appointment_id
        and _fold(booking.provider_name) == key
        and overlaps(proposed, booking.slot)
    ]
    # Worst first, then by start time, then by id: a total order, so a client rendering "this
    # clashes with…" shows the same clash every time it asks.
    conflicts.sort(key=lambda c: (-c.overlap_minutes, c.starts_at, c.appointment_id))
    return conflicts


def availability_verdict(
    slot: Slot,
    windows: Sequence[AvailabilityWindow],
    *,
    utc_offset_minutes: int,
) -> str | None:
    """``None`` when the slot sits inside recorded hours, else a reason code.

    Three outcomes, and the third is the one worth having:

    * ``None`` — the whole slot falls inside one recorded window.
    * ``"availability_not_recorded"`` — this provider has no hours on file. Not a refusal and
      not silence: nobody has said when they work, so the question could not be answered, and
      saying so is the only honest answer. The caller surfaces it as a note.
    * ``"outside_availability"`` — hours are on file and the slot is not inside them.

    The slot must fit inside a *single* window. Two adjacent windows that happen to abut
    (09:00–13:00 and 13:00–17:00) will refuse an appointment spanning one o'clock, and that is
    deliberate: two windows are two clinics, and a consultation running from one into the other
    is a booking somebody should look at rather than one the arithmetic should quietly join up.

    ``utc_offset_minutes`` is the clinic's offset from UTC — 330 for IST. Passed in rather than
    read from configuration because this module holds no configuration, and because the answer
    has to be reproducible: a booking checked against the hours in force when it was made must
    still evaluate the same way afterwards.
    """
    if not windows:
        return "availability_not_recorded"
    local_start = normalise(slot.starts_at) + timedelta(minutes=utc_offset_minutes)
    local_end = normalise(slot.ends_at) + timedelta(minutes=utc_offset_minutes)
    start_minute = local_start.hour * 60 + local_start.minute
    # Measured from the *start's* midnight, so a slot running past local midnight lands beyond
    # 1440 and matches no window — which is the right answer. A window is a block of one
    # weekday, and an appointment crossing midnight is not inside one.
    end_minute = start_minute + int(round((local_end - local_start).total_seconds() / 60.0))
    weekday = local_start.weekday()

    inside = any(
        window.weekday == weekday
        and window.start_minute <= start_minute
        and end_minute <= window.end_minute
        and end_minute <= _MINUTES_PER_DAY
        for window in windows
    )
    return None if inside else "outside_availability"


def slot_problems(
    slot: Slot,
    *,
    now: datetime,
    min_duration_minutes: int,
    max_duration_minutes: int,
    max_days_ahead: int,
) -> list[str]:
    """Every reason this interval cannot be an appointment, as reason codes.

    A list rather than the first failure, and codes rather than prose, for the same reason
    ``column_fit`` returns strings: a client fixing a booking form wants to be told everything
    that is wrong with it at once, and the clinician-facing wording belongs in the exception
    that renders these, not in the arithmetic that finds them.

    A booking in the past is a *problem*, not a refusal this module makes — writing up a walk-in
    after the fact is ordinary, so the service treats ``starts_in_the_past`` as permitted and the
    ceiling checks as fatal. The distinction is the caller's to make, which is why this returns
    findings instead of raising.
    """
    problems: list[str] = []
    starts_at, ends_at = normalise(slot.starts_at), normalise(slot.ends_at)
    duration = (ends_at - starts_at).total_seconds() / 60.0

    if duration <= 0:
        # Covers both "ends before it starts" and "zero length". They are one problem from the
        # diary's point of view: an interval that occupies no time overlaps nothing, so it can
        # be booked on top of anything, without limit, and shows on no list.
        problems.append("ends_before_it_starts" if duration < 0 else "zero_length")
    else:
        if duration < min_duration_minutes:
            problems.append("too_short")
        if duration > max_duration_minutes:
            problems.append("too_long")

    reference = normalise(now)
    if starts_at < reference:
        problems.append("starts_in_the_past")
    if starts_at > reference + timedelta(days=max_days_ahead):
        problems.append("too_far_ahead")
    return problems


def reminder_instants(
    starts_at: datetime, lead_minutes: Sequence[int]
) -> list[tuple[int, datetime]]:
    """``(lead, instant)`` for each reminder lead time, soonest-to-the-appointment last.

    Deduplicated and sorted so that a configuration listing ``1440,120,1440`` produces two
    reminders rather than three. Negative or zero leads are dropped: a "reminder" at or after
    the appointment has already failed at the one thing a reminder does.
    """
    base = normalise(starts_at)
    leads = sorted({lead for lead in lead_minutes if lead > 0}, reverse=True)
    return [(lead, base - timedelta(minutes=lead)) for lead in leads]


def due_reminders(
    appointments: Iterable[Booking],
    patient_ids: dict[str, str],
    *,
    now: datetime,
    horizon_minutes: int,
    lead_minutes: Sequence[int],
) -> list[Reminder]:
    """Reminders falling due between ``now`` and ``now + horizon``, soonest first.

    ``horizon_minutes`` is how far forward the caller intends to look, not how stale a reminder
    may be: the window starts at ``now``, so a reminder whose instant has already passed is
    **not** returned. That is the conservative reading and it is the wrong one to reverse
    casually — the alternative, returning everything overdue, turns a missed poll into a burst
    of "your appointment is tomorrow" messages about appointments that happened last week.

    Nothing here records that a reminder was issued, because nothing here issues one. See the
    module docstring: this is a derivation from the diary, and the diary is the only state.
    """
    reference = normalise(now)
    cutoff = reference + timedelta(minutes=horizon_minutes)
    due: list[Reminder] = []
    for booking in appointments:
        for lead, instant in reminder_instants(booking.slot.starts_at, lead_minutes):
            if reference <= instant <= cutoff:
                due.append(
                    Reminder(
                        appointment_id=booking.appointment_id,
                        patient_id=patient_ids.get(booking.appointment_id, ""),
                        provider_name=booking.provider_name,
                        appointment_starts_at=normalise(booking.slot.starts_at),
                        due_at=instant,
                        lead_minutes=lead,
                    )
                )
    due.sort(key=lambda r: (r.due_at, r.appointment_id, r.lead_minutes))
    return due


def _fold(name: str) -> str:
    """Provider names compared the way the drug vocabulary compares drug names.

    Whitespace-collapsed and case-folded. A conflict check that treats "Dr  Priya Nair" and
    "Dr Priya Nair" as two clinicians double-books one of them.
    """
    return " ".join(name.split()).casefold()
