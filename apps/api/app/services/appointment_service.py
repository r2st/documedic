"""Booking, rescheduling, cancelling, and the derived reminder queue.

The arithmetic is in :mod:`app.core.scheduling`; what is here is the database work around it —
which rows to compare a proposed slot against, what to refuse, and what to record.

One decision is worth stating up front because it shapes every method: **a conflict is a
refusal and everything else is a note.** Overlapping another booking of the same provider
raises; falling outside that provider's recorded hours does not. The asymmetry is not
squeamishness about being strict. A double-booked slot is two patients in one waiting room
finding out by arriving, and the clinician who genuinely means to overbook has a cancel and a
reschedule to do it with, both of which leave a record. Hours, by contrast, are a rough
description that clinics run past every day of the week, and a system that refused a 5.40pm
booking against a 5.30pm close would teach its users to keep the diary somewhere else.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.scheduling import (
    OCCUPYING_STATUSES,
    AvailabilityWindow,
    Booking,
    Conflict,
    Reminder,
    Slot,
    availability_verdict,
    due_reminders,
    find_conflicts,
    slot_problems,
)
from app.exceptions import (
    AppointmentConflictError,
    AppointmentNotFoundError,
    AppointmentNotOpenError,
    InvalidAppointmentSlotError,
)
from app.models.appointment import Appointment, ProviderAvailability
from app.services.audit_service import AuditService
from app.services.patient_service import PatientService

# The one slot problem that is a note rather than a refusal. Writing up a walk-in after the
# fact is ordinary clinic work; refusing it would push people into backdating the visit's own
# date instead, which loses the thing the record is for.
_PERMITTED_SLOT_PROBLEMS = frozenset({"starts_in_the_past"})


class AppointmentService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)

    # --- Availability -----------------------------------------------------------------------

    async def list_availability(
        self, *, account_id: uuid.UUID, provider_name: str | None = None
    ) -> list[ProviderAvailability]:
        query = select(ProviderAvailability).where(
            ProviderAvailability.account_id == account_id,
            ProviderAvailability.is_deleted.is_(False),
        )
        if provider_name is not None:
            query = query.where(ProviderAvailability.provider_name == provider_name)
        rows = await self.db.execute(
            query.order_by(
                ProviderAvailability.provider_name,
                ProviderAvailability.weekday,
                ProviderAvailability.start_minute,
            )
        )
        return list(rows.scalars().all())

    async def add_availability(
        self,
        *,
        account_id: uuid.UUID,
        provider_name: str,
        weekday: int,
        start_minute: int,
        end_minute: int,
    ) -> ProviderAvailability:
        """Record one recurring block of a provider's week.

        Re-adding an identical block returns the existing row rather than failing. A repeated
        window is not a second clinic, and ``uq_provider_availability_slot`` would refuse the
        insert anyway — answering the caller with the row they were trying to create is the
        useful reading of "make sure this window exists".
        """
        existing = await self.db.execute(
            select(ProviderAvailability).where(
                ProviderAvailability.account_id == account_id,
                ProviderAvailability.provider_name == provider_name,
                ProviderAvailability.weekday == weekday,
                ProviderAvailability.start_minute == start_minute,
                ProviderAvailability.is_deleted.is_(False),
            )
        )
        row = existing.scalars().first()
        if row is not None:
            row.end_minute = end_minute
            await self.db.flush()
            return row
        row = ProviderAvailability(
            account_id=account_id,
            provider_name=provider_name,
            weekday=weekday,
            start_minute=start_minute,
            end_minute=end_minute,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def remove_availability(
        self, *, account_id: uuid.UUID, window_id: uuid.UUID
    ) -> ProviderAvailability:
        row = await self.db.get(ProviderAvailability, window_id)
        if row is None or row.is_deleted or row.account_id != account_id:
            raise AppointmentNotFoundError(
                "That availability window is not on this account.",
                detail=f"availability window {window_id} absent or not this account's",
            )
        row.is_deleted = True
        row.deleted_at = datetime.now(UTC)
        await self.db.flush()
        return row

    async def _windows_for(
        self, *, account_id: uuid.UUID, provider_name: str
    ) -> list[AvailabilityWindow]:
        rows = await self.list_availability(account_id=account_id, provider_name=provider_name)
        return [
            AvailabilityWindow(
                weekday=row.weekday, start_minute=row.start_minute, end_minute=row.end_minute
            )
            for row in rows
        ]

    # --- Conflict detection -----------------------------------------------------------------

    async def _occupying_bookings(
        self,
        *,
        account_id: uuid.UUID,
        provider_name: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> list[Booking]:
        """Live bookings of this provider that could possibly overlap the proposed window.

        Narrowed in SQL to the rows whose interval intersects the proposal, so a provider with
        five years of diary is not loaded to answer a question about one afternoon. The
        intersection is then re-checked in ``find_conflicts`` rather than trusted from here:
        the SQL predicate uses the same half-open convention, but the authority on what
        "overlap" means is the one pure function, not two implementations that have to agree.
        """
        rows = await self.db.execute(
            select(Appointment).where(
                Appointment.account_id == account_id,
                Appointment.provider_name == provider_name,
                Appointment.is_deleted.is_(False),
                Appointment.status.in_(OCCUPYING_STATUSES),
                Appointment.starts_at < ends_at,
                Appointment.ends_at > starts_at,
            )
        )
        return [
            Booking(
                appointment_id=str(row.id),
                provider_name=row.provider_name,
                slot=Slot(starts_at=row.starts_at, ends_at=row.ends_at),
            )
            for row in rows.scalars().all()
        ]

    def _validate_slot(self, slot: Slot) -> None:
        problems = [
            problem
            for problem in slot_problems(
                slot,
                now=datetime.now(UTC),
                min_duration_minutes=settings.appointment_min_duration_minutes,
                max_duration_minutes=settings.appointment_max_duration_minutes,
                max_days_ahead=settings.appointment_max_days_ahead,
            )
            if problem not in _PERMITTED_SLOT_PROBLEMS
        ]
        if problems:
            raise InvalidAppointmentSlotError(
                detail=f"slot rejected: {', '.join(sorted(problems))}"
            )

    async def _assert_free(
        self,
        *,
        account_id: uuid.UUID,
        provider_name: str,
        slot: Slot,
        exclude_appointment_id: uuid.UUID | None = None,
    ) -> None:
        existing = await self._occupying_bookings(
            account_id=account_id,
            provider_name=provider_name,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
        )
        conflicts = find_conflicts(
            slot,
            provider_name,
            existing,
            exclude_appointment_id=(
                None if exclude_appointment_id is None else str(exclude_appointment_id)
            ),
        )
        if conflicts:
            raise AppointmentConflictError(detail=_conflict_detail(conflicts))

    async def availability_note(
        self, *, account_id: uuid.UUID, provider_name: str, slot: Slot
    ) -> str | None:
        windows = await self._windows_for(account_id=account_id, provider_name=provider_name)
        return availability_verdict(
            slot, windows, utc_offset_minutes=settings.clinic_utc_offset_minutes
        )

    # --- Lifecycle --------------------------------------------------------------------------

    async def book(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        provider_name: str,
        starts_at: datetime,
        ends_at: datetime,
        modality: str = "in_person",
        appointment_type: str | None = None,
        reason: str | None = None,
        source_protocol_key: str | None = None,
        encounter_id: uuid.UUID | None = None,
    ) -> Appointment:
        """Book a slot, refusing an overlap and noting an out-of-hours time.

        The ``IntegrityError`` branch is the race the read-then-check above cannot close. Two
        booking requests for the same provider and instant can both find the diary empty and
        both proceed; ``uq_appointments_provider_start`` refuses the second insert, and turning
        that into the same 409 the check produces is what makes the guarantee hold under a
        double-submitted form rather than only under sequential use. It catches exact
        collisions only — partial overlap has no index that can express it (see the model
        docstring) — so the check above is not redundant with it.
        """
        await PatientService(self.db).get(account_id, patient_id)
        slot = Slot(starts_at=starts_at, ends_at=ends_at)
        self._validate_slot(slot)
        await self._assert_free(account_id=account_id, provider_name=provider_name, slot=slot)

        appointment = Appointment(
            account_id=account_id,
            patient_id=patient_id,
            provider_name=provider_name,
            starts_at=starts_at,
            ends_at=ends_at,
            status="scheduled",
            modality=modality,
            appointment_type=appointment_type,
            reason=reason,
            source_protocol_key=source_protocol_key,
            booked_at_encounter_id=encounter_id,
            created_by_account_id=account_id,
        )
        self.db.add(appointment)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            await self.db.rollback()
            raise AppointmentConflictError(
                detail=(
                    f"uq_appointments_provider_start refused a concurrent booking of "
                    f"{provider_name!r} at {starts_at.isoformat()}"
                )
            ) from exc

        note = await self.availability_note(
            account_id=account_id, provider_name=provider_name, slot=slot
        )
        await self.audit.record(
            action="appointment_booked",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="appointment",
            entity_id=appointment.id,
            payload={
                # The provider's name is the *clinician's*, not the patient's, and "whose diary
                # this went into" is the point of the entry. The reason the patient is coming is
                # clinician prose about a patient and stays on the row that ``entity_id`` points
                # at; only whether one was given is recorded.
                "provider_name": provider_name,
                "starts_at": _iso(starts_at),
                "duration_minutes": int(slot.duration_minutes),
                "modality": modality,
                "reason_recorded": reason is not None,
                "availability_note": note,
                "source_protocol_key": source_protocol_key,
            },
        )
        return appointment

    async def get(self, patient_id: uuid.UUID, appointment_id: uuid.UUID) -> Appointment:
        appointment = await self.db.get(Appointment, appointment_id)
        if appointment is None or appointment.is_deleted or appointment.patient_id != patient_id:
            raise AppointmentNotFoundError(
                detail=f"appointment {appointment_id} not found on patient {patient_id}"
            )
        return appointment

    async def reschedule(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        appointment_id: uuid.UUID,
        starts_at: datetime,
        ends_at: datetime,
        provider_name: str | None = None,
    ) -> Appointment:
        """Move a booking, excluding it from its own conflict check.

        Only a ``scheduled`` booking can move. A cancelled one has released its slot, which may
        already be somebody else's; a completed or missed one is a record of what happened, and
        rewriting when it happened is not rescheduling.
        """
        await PatientService(self.db).get(account_id, patient_id)
        appointment = await self.get(patient_id, appointment_id)
        _assert_open(appointment)

        target_provider = provider_name or appointment.provider_name
        slot = Slot(starts_at=starts_at, ends_at=ends_at)
        self._validate_slot(slot)
        await self._assert_free(
            account_id=account_id,
            provider_name=target_provider,
            slot=slot,
            exclude_appointment_id=appointment_id,
        )

        previous_start = appointment.starts_at
        appointment.provider_name = target_provider
        appointment.starts_at = starts_at
        appointment.ends_at = ends_at
        await self.db.flush()

        note = await self.availability_note(
            account_id=account_id, provider_name=target_provider, slot=slot
        )
        await self.audit.record(
            action="appointment_rescheduled",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="appointment",
            entity_id=appointment.id,
            payload={
                "provider_name": target_provider,
                # Both ends of the move. The old time is not recoverable from the row afterwards
                # — the row now holds the new one — and "this was moved from Tuesday" is the
                # question a later reader asks about a missed appointment.
                "previous_starts_at": _iso(previous_start),
                "starts_at": _iso(starts_at),
                "duration_minutes": int(slot.duration_minutes),
                "availability_note": note,
            },
        )
        return appointment

    async def cancel(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        appointment_id: uuid.UUID,
        reason: str,
    ) -> Appointment:
        """Release the slot. The only thing that does — see ``OCCUPYING_STATUSES``."""
        await PatientService(self.db).get(account_id, patient_id)
        appointment = await self.get(patient_id, appointment_id)
        _assert_open(appointment)

        appointment.status = "cancelled"
        appointment.cancelled_at = datetime.now(UTC)
        appointment.cancellation_reason = reason
        await self.db.flush()
        await self.audit.record(
            action="appointment_cancelled",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="appointment",
            entity_id=appointment.id,
            payload={
                "provider_name": appointment.provider_name,
                "starts_at": _iso(appointment.starts_at),
                # The reason is clinician prose and stays on the row. Its length says one was
                # actually written rather than the field being satisfied with a full stop.
                "reason_chars": len(reason),
            },
        )
        return appointment

    async def close(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        appointment_id: uuid.UUID,
        attended: bool,
    ) -> Appointment:
        """Record whether the patient came.

        ``no_show`` is kept as its own status rather than folded into ``cancelled`` because the
        two are different clinical facts: a cancelled follow-up is one somebody decided against,
        and a missed one is a patient who has fallen out of care. Only the second is worth
        anybody's attention afterwards, and a diary that cannot tell them apart shows neither.
        """
        await PatientService(self.db).get(account_id, patient_id)
        appointment = await self.get(patient_id, appointment_id)
        _assert_open(appointment)

        appointment.status = "completed" if attended else "no_show"
        await self.db.flush()
        await self.audit.record(
            action="appointment_closed",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="appointment",
            entity_id=appointment.id,
            payload={
                "provider_name": appointment.provider_name,
                "starts_at": _iso(appointment.starts_at),
                "status": appointment.status,
            },
        )
        return appointment

    # --- Reads ------------------------------------------------------------------------------

    async def list_for_patient(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, limit: int
    ) -> list[Appointment]:
        await PatientService(self.db).get(account_id, patient_id)
        rows = await self.db.execute(
            select(Appointment)
            .where(
                Appointment.patient_id == patient_id,
                Appointment.is_deleted.is_(False),
            )
            # Soonest first among the forthcoming, which is what a chart's "next appointment"
            # panel wants; the primary key breaks ties so paging over two bookings at the same
            # instant is a partition rather than a lottery.
            .order_by(Appointment.starts_at.desc(), Appointment.id)
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def diary(
        self,
        *,
        account_id: uuid.UUID,
        window_start: datetime,
        window_end: datetime,
        provider_name: str | None,
        limit: int,
    ) -> list[Appointment]:
        """This account's bookings in a window, across every chart on the panel."""
        query = select(Appointment).where(
            Appointment.account_id == account_id,
            Appointment.is_deleted.is_(False),
            Appointment.starts_at >= window_start,
            Appointment.starts_at < window_end,
        )
        if provider_name is not None:
            query = query.where(Appointment.provider_name == provider_name)
        rows = await self.db.execute(
            query.order_by(Appointment.starts_at, Appointment.id).limit(limit)
        )
        return list(rows.scalars().all())

    async def reminders_due(self, *, account_id: uuid.UUID, horizon_minutes: int) -> list[Reminder]:
        """Reminders falling due between now and the horizon. Derived; nothing is sent.

        The rows swept are the *forthcoming* bookings whose earliest reminder could fall inside
        the window — an appointment further ahead than the longest lead plus the horizon cannot
        have a reminder due yet, and one already past cannot either.
        """
        now = datetime.now(UTC)
        leads = settings.reminder_lead_minutes
        if not leads:
            return []
        furthest = max(leads)
        rows = await self.db.execute(
            select(Appointment).where(
                Appointment.account_id == account_id,
                Appointment.is_deleted.is_(False),
                Appointment.status.in_(OCCUPYING_STATUSES),
                Appointment.starts_at >= now,
                Appointment.starts_at <= now + timedelta(minutes=furthest + horizon_minutes),
            )
        )
        appointments = list(rows.scalars().all())
        bookings = [
            Booking(
                appointment_id=str(row.id),
                provider_name=row.provider_name,
                slot=Slot(starts_at=row.starts_at, ends_at=row.ends_at),
            )
            for row in appointments
        ]
        patient_ids = {str(row.id): str(row.patient_id) for row in appointments}
        return due_reminders(
            bookings,
            patient_ids,
            now=now,
            horizon_minutes=horizon_minutes,
            lead_minutes=leads,
        )


def _assert_open(appointment: Appointment) -> None:
    if appointment.status not in OCCUPYING_STATUSES:
        raise AppointmentNotOpenError(
            detail=f"appointment {appointment.id} is {appointment.status}"
        )


def _conflict_detail(conflicts: Sequence[Conflict]) -> str:
    """The clash, for the log. Never serialised — the clinician-facing message is the class's."""
    return "; ".join(
        f"{c.appointment_id} {c.starts_at.isoformat()}–{c.ends_at.isoformat()} "
        f"({c.overlap_minutes:.0f}m overlap)"
        for c in conflicts
    )


def _iso(moment: datetime) -> str:
    """A timestamp for the audit payload, always UTC and always aware.

    SQLite hands back naive datetimes where PostgreSQL keeps the offset, and the payload is
    hashed into the chain — so a value that serialised differently on the two backends would
    make the same action produce two different record hashes.
    """
    aware = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return aware.isoformat()
