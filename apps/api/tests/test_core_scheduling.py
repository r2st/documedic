"""The scheduling arithmetic, on its own — no database, no clock, no settings.

``app.core.scheduling`` is written as pure functions for the same reason the safety engine is:
a booking refusal has to be reproducible from the record afterwards. These tests are what makes
that property worth having, because they can state the boundary cases directly rather than
through a request that has to build a chart first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.scheduling import (
    APPOINTMENT_MODALITIES,
    APPOINTMENT_STATUSES,
    OCCUPYING_STATUSES,
    AvailabilityWindow,
    Booking,
    Slot,
    availability_verdict,
    due_reminders,
    find_conflicts,
    normalise,
    overlap_minutes,
    overlaps,
    reminder_instants,
    slot_problems,
)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC instant in a fixed week. 2026-08-17 is a Monday."""
    return datetime(2026, 8, 16 + day, hour, minute, tzinfo=UTC)


def _slot(day: int, hour: int, minute: int = 0, *, minutes: int = 30) -> Slot:
    start = _at(day, hour, minute)
    return Slot(starts_at=start, ends_at=start + timedelta(minutes=minutes))


# --- overlap ------------------------------------------------------------------------------


def test_back_to_back_appointments_do_not_overlap():
    """The convention a clinic list is written in.

    Half-open intervals are the whole reason a full morning is bookable: with closed intervals
    every 09:30–10:00 would clash with every 10:00–10:30, and a busy diary would be unfillable.
    """
    assert not overlaps(_slot(1, 9, 30), _slot(1, 10, 0))
    assert overlap_minutes(_slot(1, 9, 30), _slot(1, 10, 0)) == 0


def test_a_slot_starting_one_minute_early_overlaps():
    assert overlaps(_slot(1, 9, 30), _slot(1, 9, 59))
    assert overlap_minutes(_slot(1, 9, 30), _slot(1, 9, 59)) == pytest.approx(1.0)


def test_a_slot_entirely_inside_another_overlaps():
    outer = Slot(starts_at=_at(1, 9), ends_at=_at(1, 12))
    inner = _slot(1, 10, minutes=15)
    assert overlaps(outer, inner) and overlaps(inner, outer)
    assert overlap_minutes(outer, inner) == pytest.approx(15.0)


def test_a_naive_timestamp_is_read_as_utc_rather_than_raising():
    """SQLite drops tzinfo on the round trip and PostgreSQL keeps it, so both reach these
    functions. Subtracting one from the other raises, which would make overlap detection fail
    on exactly one of the two backends."""
    naive = Slot(
        starts_at=datetime(2026, 8, 17, 9, 30),
        ends_at=datetime(2026, 8, 17, 10, 0),
    )
    assert overlaps(naive, _slot(1, 9, 45))
    assert normalise(naive.starts_at).tzinfo is UTC


# --- conflicts ----------------------------------------------------------------------------


def _booking(identifier: str, provider: str, slot: Slot) -> Booking:
    return Booking(appointment_id=identifier, provider_name=provider, slot=slot)


def test_two_providers_at_the_same_hour_do_not_conflict():
    """A normal Tuesday. Scoping conflicts to the account rather than the provider would make
    the diary unusable for any practice with more than one clinician."""
    existing = [_booking("a", "Dr Priya Nair", _slot(1, 11))]
    assert find_conflicts(_slot(1, 11), "Dr Anil Rao", existing) == []


def test_the_same_provider_spelled_differently_still_conflicts():
    """Case and whitespace must not open a hole. A conflict check that misses because somebody
    typed two spaces is a conflict check that is not there."""
    existing = [_booking("a", "Dr  Priya   Nair", _slot(1, 11))]
    conflicts = find_conflicts(_slot(1, 11), "dr priya nair", existing)
    assert [c.appointment_id for c in conflicts] == ["a"]


def test_rescheduling_a_booking_does_not_conflict_with_itself():
    """Without the exclusion, moving an appointment by ten minutes is impossible without first
    cancelling it — which opens a real gap another booking can take."""
    existing = [_booking("a", "Dr Priya Nair", _slot(1, 11))]
    moved = _slot(1, 11, 10)
    assert find_conflicts(moved, "Dr Priya Nair", existing) != []
    assert find_conflicts(moved, "Dr Priya Nair", existing, exclude_appointment_id="a") == []


def test_conflicts_come_back_worst_overlap_first_and_in_a_total_order():
    """A client rendering "this clashes with…" must show the same clash every time it asks."""
    existing = [
        _booking("small", "Dr Priya Nair", _slot(1, 10, 55, minutes=10)),
        _booking("large", "Dr Priya Nair", Slot(starts_at=_at(1, 11), ends_at=_at(1, 12))),
    ]
    conflicts = find_conflicts(_slot(1, 11, minutes=30), "Dr Priya Nair", existing)
    assert [c.appointment_id for c in conflicts] == ["large", "small"]
    assert conflicts[0].overlap_minutes == pytest.approx(30.0)
    assert conflicts[1].overlap_minutes == pytest.approx(5.0)


# --- availability -------------------------------------------------------------------------

# 2026-08-17 is a Monday, so weekday 0. 540 = 09:00 local, 780 = 13:00 local.
_IST = 330
_MONDAY_MORNING = AvailabilityWindow(weekday=0, start_minute=540, end_minute=780)


def test_a_slot_inside_recorded_hours_has_no_verdict():
    # 09:30 IST on the Monday is 04:00 UTC.
    slot = Slot(
        starts_at=datetime(2026, 8, 17, 4, 0, tzinfo=UTC),
        ends_at=datetime(2026, 8, 17, 4, 30, tzinfo=UTC),
    )
    assert availability_verdict(slot, [_MONDAY_MORNING], utc_offset_minutes=_IST) is None


def test_a_provider_with_no_recorded_hours_gets_its_own_answer():
    """Not "outside hours" and not silence.

    Reporting unknown hours as a breach would train people to ignore the warning; reporting
    them as nothing would let a booking claim to have been checked against hours that do not
    exist. Same doctrine as the safety engine's "not evaluated" flags.
    """
    assert (
        availability_verdict(_slot(1, 9), [], utc_offset_minutes=_IST)
        == "availability_not_recorded"
    )


def test_a_slot_outside_the_window_is_flagged():
    # 18:00 IST is 12:30 UTC, past the 13:00 local close.
    slot = Slot(
        starts_at=datetime(2026, 8, 17, 12, 30, tzinfo=UTC),
        ends_at=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
    )
    assert (
        availability_verdict(slot, [_MONDAY_MORNING], utc_offset_minutes=_IST)
        == "outside_availability"
    )


def test_the_utc_offset_is_what_decides_the_weekday():
    """The bug this guards is a whole day out, not an hour.

    23:00 IST on Monday is 17:30 UTC on Monday — but 01:00 IST on *Tuesday* is 19:30 UTC on
    Monday, and a comparison done in UTC would check a Tuesday booking against Monday's hours.
    """
    # 09:30 IST Tuesday = 04:00 UTC Tuesday. Against a Monday-only window it must fail.
    tuesday = Slot(
        starts_at=datetime(2026, 8, 18, 4, 0, tzinfo=UTC),
        ends_at=datetime(2026, 8, 18, 4, 30, tzinfo=UTC),
    )
    assert (
        availability_verdict(tuesday, [_MONDAY_MORNING], utc_offset_minutes=_IST)
        == "outside_availability"
    )
    # ...and the same wall-clock time with a zero offset is a *different local day*, which is
    # the whole point: at UTC+0, 04:00 Tuesday is still Tuesday, so it still fails.
    assert (
        availability_verdict(tuesday, [_MONDAY_MORNING], utc_offset_minutes=0)
        == "outside_availability"
    )


def test_a_slot_spanning_two_abutting_windows_is_flagged_rather_than_joined_up():
    """Deliberate. Two windows are two clinics, and a consultation running from one into the
    other is something to look at rather than something the arithmetic should quietly merge."""
    morning = AvailabilityWindow(weekday=0, start_minute=540, end_minute=780)
    afternoon = AvailabilityWindow(weekday=0, start_minute=780, end_minute=1020)
    # 12:45–13:15 IST = 07:15–07:45 UTC, straddling the 13:00 local boundary.
    straddling = Slot(
        starts_at=datetime(2026, 8, 17, 7, 15, tzinfo=UTC),
        ends_at=datetime(2026, 8, 17, 7, 45, tzinfo=UTC),
    )
    assert (
        availability_verdict(straddling, [morning, afternoon], utc_offset_minutes=_IST)
        == "outside_availability"
    )


def test_a_slot_running_past_local_midnight_is_never_inside_a_window():
    """A window is a block of one weekday. An appointment crossing midnight is not in one, and
    the minute arithmetic must not wrap it round into looking like an early-morning slot."""
    late = AvailabilityWindow(weekday=0, start_minute=1380, end_minute=1440)  # 23:00–24:00
    # 23:45–00:15 IST = 18:15–18:45 UTC on the Monday.
    crossing = Slot(
        starts_at=datetime(2026, 8, 17, 18, 15, tzinfo=UTC),
        ends_at=datetime(2026, 8, 17, 18, 45, tzinfo=UTC),
    )
    assert availability_verdict(crossing, [late], utc_offset_minutes=_IST) == "outside_availability"


# --- slot validation ------------------------------------------------------------------------

_NOW = datetime(2026, 8, 17, 6, 0, tzinfo=UTC)
_BOUNDS = {"min_duration_minutes": 5, "max_duration_minutes": 480, "max_days_ahead": 730}


def test_a_well_formed_slot_has_no_problems():
    assert slot_problems(_slot(1, 12), now=_NOW, **_BOUNDS) == []


def test_an_inverted_slot_is_named_as_such():
    backwards = Slot(starts_at=_at(1, 12), ends_at=_at(1, 11))
    assert slot_problems(backwards, now=_NOW, **_BOUNDS) == ["ends_before_it_starts"]


def test_a_zero_length_slot_is_refused_rather_than_being_infinitely_bookable():
    """An interval that occupies no time overlaps nothing, so it can be stacked on top of every
    other booking without limit and appears on no list that filters by "covers this hour"."""
    instant = Slot(starts_at=_at(1, 12), ends_at=_at(1, 12))
    assert slot_problems(instant, now=_NOW, **_BOUNDS) == ["zero_length"]


def test_the_duration_bounds_are_reported_separately_from_the_date_bounds():
    """A list, not the first failure: a booking form should be told everything at once."""
    far_and_long = Slot(
        starts_at=_NOW + timedelta(days=900),
        ends_at=_NOW + timedelta(days=900, hours=12),
    )
    assert sorted(slot_problems(far_and_long, now=_NOW, **_BOUNDS)) == [
        "too_far_ahead",
        "too_long",
    ]


def test_a_past_slot_is_reported_but_is_the_service_layers_call():
    """Writing up a walk-in after the fact is ordinary clinic work. This module reports it;
    ``AppointmentService`` is where the decision to permit it lives."""
    assert slot_problems(_slot(-3, 9), now=_NOW, **_BOUNDS) == ["starts_in_the_past"]


# --- reminders ----------------------------------------------------------------------------


def test_reminder_instants_are_deduplicated_and_ordered_furthest_out_first():
    instants = reminder_instants(_at(3, 9), [1440, 120, 1440])
    assert [lead for lead, _ in instants] == [1440, 120]
    assert [when for _, when in instants] == [_at(2, 9), _at(3, 7)]


def test_a_lead_at_or_after_the_appointment_is_dropped():
    """A "reminder" that fires when the patient should already be in the waiting room has
    failed at the one thing a reminder does."""
    assert reminder_instants(_at(3, 9), [0, -60]) == []


def test_a_reminder_whose_moment_has_already_passed_is_not_returned():
    """The conservative reading, and the one not to reverse casually: returning everything
    overdue turns a missed poll into a burst of "your appointment is tomorrow" messages about
    appointments that already happened."""
    appointment = _booking("a", "Dr Priya Nair", _slot(1, 9))
    # The 24-hour reminder for a Monday-09:00 appointment fell due on the Sunday.
    due = due_reminders(
        [appointment],
        {"a": "patient-1"},
        now=_at(1, 8),
        horizon_minutes=120,
        lead_minutes=[1440, 120],
    )
    assert [reminder.lead_minutes for reminder in due] == []


def test_a_reminder_inside_the_horizon_comes_back_with_its_patient():
    appointment = _booking("a", "Dr Priya Nair", _slot(1, 9))
    due = due_reminders(
        [appointment],
        {"a": "patient-1"},
        now=_at(1, 6, 30),
        horizon_minutes=60,
        lead_minutes=[1440, 120],
    )
    assert len(due) == 1
    assert due[0].lead_minutes == 120
    assert due[0].patient_id == "patient-1"
    assert due[0].due_at == _at(1, 7)


def test_reminders_are_returned_in_a_total_order():
    bookings = [
        _booking("later", "Dr Priya Nair", _slot(1, 10)),
        _booking("earlier", "Dr Anil Rao", _slot(1, 9)),
    ]
    due = due_reminders(
        bookings,
        {"later": "p2", "earlier": "p1"},
        now=_at(1, 6),
        horizon_minutes=180,
        lead_minutes=[120],
    )
    assert [reminder.appointment_id for reminder in due] == ["earlier", "later"]


# --- the vocabularies -----------------------------------------------------------------------


def test_cancelling_is_the_only_thing_that_frees_a_slot():
    """Stated as a test because the constant is short enough to look like an oversight.

    ``cancelled`` must not occupy time, or a clinic that reschedules twice in a morning locks
    itself out of its own diary. ``completed`` and ``no_show`` are excluded rather than relied
    on to be historical, because backdating a walk-in is real and must not collide with the
    visit it *is*.
    """
    assert OCCUPYING_STATUSES == ("scheduled",)
    assert set(OCCUPYING_STATUSES) <= set(APPOINTMENT_STATUSES)
    assert "cancelled" not in OCCUPYING_STATUSES


def test_the_modality_vocabulary_distinguishes_audio_from_video():
    """Not cosmetic: on an audio call the prescriber has not seen the patient, which is the
    line India's Telemedicine Practice Guidelines draw and ``app.core.telehealth`` enforces."""
    assert APPOINTMENT_MODALITIES == ("in_person", "video", "audio")
