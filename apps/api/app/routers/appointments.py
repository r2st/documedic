"""Appointment scheduling: the chart's bookings, the account's diary, and provider hours."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scheduling import Slot
from app.db.session import get_db
from app.dependencies import get_current_account
from app.models.appointment import Appointment
from app.models.user import Account
from app.openapi import AUTH_ERRORS, PATIENT_ERRORS, errors
from app.schemas.appointment import (
    MAX_DIARY_ROWS,
    MAX_REMINDER_HORIZON_MINUTES,
    AppointmentCancelRequest,
    AppointmentCreateRequest,
    AppointmentOutcomeRequest,
    AppointmentRescheduleRequest,
    AppointmentResponse,
    AvailabilityWindowRequest,
    AvailabilityWindowResponse,
    ReminderQueueResponse,
    ReminderResponse,
)
from app.schemas.common import AppointmentModality, AppointmentStatus
from app.services.appointment_service import AppointmentService
from app.services.audit_service import AuditService

router = APIRouter(prefix="/patients/{patient_id}/appointments", tags=["appointments"])
# The panel-wide half: the diary, the reminder derivation and provider hours are not about one
# chart, so they hang off their own prefix rather than under a patient id that would have to be
# invented to reach them.
diary_router = APIRouter(prefix="/appointments", tags=["appointments"])

_DEFAULT_DIARY_DAYS = 7
_DEFAULT_REMINDER_HORIZON_MINUTES = 60


def _to_response(
    appointment: Appointment, *, availability_note: str | None = None
) -> AppointmentResponse:
    return AppointmentResponse(
        id=appointment.id,
        patient_id=appointment.patient_id,
        provider_name=appointment.provider_name,
        starts_at=appointment.starts_at,
        ends_at=appointment.ends_at,
        # Both columns are plain ``String`` holding one of a closed set; the response narrows
        # them to the enums the client is typed by. The DB check constraints and the service
        # keep the columns inside those sets.
        status=cast(AppointmentStatus, appointment.status),
        modality=cast(AppointmentModality, appointment.modality),
        appointment_type=appointment.appointment_type,
        reason=appointment.reason,
        source_protocol_key=appointment.source_protocol_key,
        cancelled_at=appointment.cancelled_at,
        cancellation_reason=appointment.cancellation_reason,
        availability_note=availability_note,
        created_at=appointment.created_at,
    )


@router.post(
    "",
    response_model=AppointmentResponse,
    status_code=201,
    summary="Book a slot for this patient",
    responses=PATIENT_ERRORS | errors(409),
)
async def book_appointment(
    patient_id: uuid.UUID,
    body: AppointmentCreateRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> AppointmentResponse:
    """Books the slot, or refuses it with 409 `appointment_conflict` if the provider is busy.

    **A clash is a refusal; out-of-hours is a note.** Overlapping another live booking of the
    same provider raises, because a double-booked slot is two patients in one waiting room
    finding out by arriving — and a clinician who means to overbook can cancel or reschedule,
    both of which leave a record. Falling outside that provider's recorded hours comes back as
    `availability_note` instead: clinics run late, and a refusal at 5.40pm against a 5.30pm
    close would teach people to keep the diary elsewhere.

    `availability_note` has a third value worth handling. `availability_not_recorded` means
    nobody has told the system when this provider works, which is deliberately distinct from
    `outside_availability` — a question that could not be asked is never reported as one that
    passed.

    Overlap is per **provider**, not per account: two clinicians in one practice seeing two
    patients at eleven o'clock is a normal Tuesday.

    A start time in the past is accepted. Writing up a walk-in after the fact is ordinary, and
    refusing it would push people into backdating the visit itself.
    """
    service = AppointmentService(db)
    appointment = await service.book(
        account_id=account.id,
        patient_id=patient_id,
        provider_name=body.provider_name,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        modality=body.modality.value,
        appointment_type=body.appointment_type,
        reason=body.reason,
    )
    note = await service.availability_note(
        account_id=account.id,
        provider_name=appointment.provider_name,
        slot=Slot(starts_at=appointment.starts_at, ends_at=appointment.ends_at),
    )
    await db.commit()
    return _to_response(appointment, availability_note=note)


@router.get(
    "",
    response_model=list[AppointmentResponse],
    summary="Every appointment recorded on this chart",
    responses=PATIENT_ERRORS,
)
async def list_appointments(
    patient_id: uuid.UUID,
    limit: int = Query(
        default=100,
        ge=1,
        le=MAX_DIARY_ROWS,
        description=(
            "Most recent bookings to return, newest start time first. Cancelled and missed "
            "appointments are included — a patient who did not attend a follow-up is a "
            "clinical fact, not a tidied-away one."
        ),
    ),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[AppointmentResponse]:
    """Newest start time first, including cancelled and missed bookings.

    Returns why the patient was coming, which is clinical content about them, so the read is
    audited as `appointment_list_viewed`.
    """
    rows = await AppointmentService(db).list_for_patient(
        account_id=account.id, patient_id=patient_id, limit=limit
    )
    await AuditService(db).record(
        action="appointment_list_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        payload={"appointment_count": len(rows)},
    )
    await db.commit()
    return [_to_response(row) for row in rows]


@router.post(
    "/{appointment_id}/reschedule",
    response_model=AppointmentResponse,
    summary="Move a booking to a different time",
    responses=PATIENT_ERRORS | errors(409),
)
async def reschedule_appointment(
    patient_id: uuid.UUID,
    appointment_id: uuid.UUID,
    body: AppointmentRescheduleRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> AppointmentResponse:
    """Moves the slot, excluding this booking from its own conflict check.

    Without that exclusion an appointment shifted by ten minutes always clashes with itself,
    and the only route past it would be cancel-then-rebook — which opens a real gap another
    booking can take while the clinician is still typing.

    409 `appointment_not_open` for a booking that is already cancelled, completed or missed. A
    cancelled slot has been released and may be somebody else's; a closed one is a record of
    what happened, and changing when it happened is not rescheduling.
    """
    service = AppointmentService(db)
    appointment = await service.reschedule(
        account_id=account.id,
        patient_id=patient_id,
        appointment_id=appointment_id,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        provider_name=body.provider_name,
    )
    note = await service.availability_note(
        account_id=account.id,
        provider_name=appointment.provider_name,
        slot=Slot(starts_at=appointment.starts_at, ends_at=appointment.ends_at),
    )
    await db.commit()
    return _to_response(appointment, availability_note=note)


@router.post(
    "/{appointment_id}/cancel",
    response_model=AppointmentResponse,
    summary="Cancel a booking and free the slot",
    responses=PATIENT_ERRORS | errors(409),
)
async def cancel_appointment(
    patient_id: uuid.UUID,
    appointment_id: uuid.UUID,
    body: AppointmentCancelRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> AppointmentResponse:
    """Cancelling is the only thing that frees a slot for re-booking.

    The reason is required, and not as bureaucracy: because cancellation is the status that
    makes time re-bookable, it is the one somebody reaches for to tidy away a mistake. "Booked
    in error" and "patient declined follow-up" are different facts about a chart, and only the
    second of them is clinical.
    """
    appointment = await AppointmentService(db).cancel(
        account_id=account.id,
        patient_id=patient_id,
        appointment_id=appointment_id,
        reason=body.reason,
    )
    await db.commit()
    return _to_response(appointment)


@router.post(
    "/{appointment_id}/outcome",
    response_model=AppointmentResponse,
    summary="Close a booking as attended or not attended",
    responses=PATIENT_ERRORS | errors(409),
)
async def close_appointment(
    patient_id: uuid.UUID,
    appointment_id: uuid.UUID,
    body: AppointmentOutcomeRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> AppointmentResponse:
    """Records whether the patient came. `attended: false` is stored as `no_show`, not cancelled.

    The distinction is the point of having both. A cancelled follow-up is one somebody decided
    against; a missed one is a patient who has fallen out of care, which is the only one of the
    two worth anybody's attention afterwards — and a diary that cannot tell them apart surfaces
    neither.
    """
    appointment = await AppointmentService(db).close(
        account_id=account.id,
        patient_id=patient_id,
        appointment_id=appointment_id,
        attended=body.attended,
    )
    await db.commit()
    return _to_response(appointment)


@diary_router.get(
    "/diary",
    response_model=list[AppointmentResponse],
    summary="This account's bookings in a date window, across every chart",
    responses=AUTH_ERRORS,
)
async def read_diary(
    days: int = Query(
        default=_DEFAULT_DIARY_DAYS,
        ge=1,
        le=90,
        description="How many days forward from now to include. Counted from this instant.",
    ),
    provider_name: str | None = Query(
        default=None,
        max_length=200,
        description="Narrow to one provider's diary, matched exactly as recorded.",
    ),
    limit: int = Query(
        default=200,
        ge=1,
        le=MAX_DIARY_ROWS,
        description="Bookings to return, earliest start time first.",
    ),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[AppointmentResponse]:
    """The forthcoming list, earliest first. No patient in the path — this is the whole panel.

    Discloses which patients are booked and why, so the read is audited as
    `appointment_diary_viewed` against the account rather than against any one chart, in the
    same way the critical-lab queue is.
    """
    now = datetime.now(UTC)
    rows = await AppointmentService(db).diary(
        account_id=account.id,
        window_start=now,
        window_end=now + timedelta(days=days),
        provider_name=provider_name,
        limit=limit,
    )
    await AuditService(db).record(
        action="appointment_diary_viewed",
        account_id=account.id,
        # No patient_id: the read is not about one chart, and each booking's own entry sits on
        # the patient's trail already.
        patient_id=None,
        entity_type="account",
        entity_id=account.id,
        payload={"days": days, "appointment_count": len(rows), "provider_filtered": provider_name},
    )
    await db.commit()
    return [_to_response(row) for row in rows]


@diary_router.get(
    "/reminders",
    response_model=ReminderQueueResponse,
    summary="Reminders falling due in the next window — derived, never sent",
    responses=AUTH_ERRORS,
)
async def reminder_queue(
    horizon_minutes: int = Query(
        default=_DEFAULT_REMINDER_HORIZON_MINUTES,
        ge=1,
        le=MAX_REMINDER_HORIZON_MINUTES,
        description=(
            "How far forward to look. The window starts now, so a reminder whose moment has "
            "already passed is not returned — a missed poll must not produce a burst of "
            "'your appointment is tomorrow' about appointments that already happened."
        ),
    ),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ReminderQueueResponse:
    """Which reminders fall due, computed from the diary. **This system sends nothing.**

    There is no messaging transport here — no SMS gateway, no mail relay — and a scheduling
    feature that implied a reminder had gone out would be worse than one that offered none,
    because the clinic would stop telephoning. So `delivery_channel` is always `none` and this
    is a queue for a person or an integration to work, in the same sense the critical-lab queue
    is.

    Nothing is marked as sent, and nothing here is stored: the reminders are derived from the
    bookings each time they are asked for. Lead times come from `APPOINTMENT_REMINDER_LEADS`.

    Deterministic and offline-capable — arithmetic over the diary, no LLM.
    """
    reminders = await AppointmentService(db).reminders_due(
        account_id=account.id, horizon_minutes=horizon_minutes
    )
    await AuditService(db).record(
        action="appointment_reminders_viewed",
        account_id=account.id,
        patient_id=None,
        entity_type="account",
        entity_id=account.id,
        payload={"horizon_minutes": horizon_minutes, "reminder_count": len(reminders)},
    )
    await db.commit()
    return ReminderQueueResponse(
        generated_at=datetime.now(UTC),
        horizon_minutes=horizon_minutes,
        items=[
            ReminderResponse(
                appointment_id=uuid.UUID(reminder.appointment_id),
                patient_id=uuid.UUID(reminder.patient_id),
                provider_name=reminder.provider_name,
                appointment_starts_at=reminder.appointment_starts_at,
                due_at=reminder.due_at,
                lead_minutes=reminder.lead_minutes,
            )
            for reminder in reminders
        ],
    )


@diary_router.get(
    "/availability",
    response_model=list[AvailabilityWindowResponse],
    summary="Recorded working hours, by provider",
    responses=AUTH_ERRORS,
)
async def list_availability(
    provider_name: str | None = Query(
        default=None, max_length=200, description="Narrow to one provider's hours."
    ),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[AvailabilityWindowResponse]:
    """Working hours in **clinic-local** time, as minutes from midnight (540 = 09:00).

    Local rather than UTC because that is how a clinic states its hours: "Tuesdays, nine to
    one" does not move when the offset does. The conversion happens once, when a booking is
    checked, against `CLINIC_UTC_OFFSET_MINUTES`.

    A provider with no rows here has *unknown* hours, not no hours — which is why a booking for
    them comes back with `availability_not_recorded` rather than with silence.

    Contains no patient data, so this read is not audited as a disclosure.
    """
    rows = await AppointmentService(db).list_availability(
        account_id=account.id, provider_name=provider_name
    )
    return [
        AvailabilityWindowResponse(
            id=row.id,
            provider_name=row.provider_name,
            weekday=row.weekday,
            start_minute=row.start_minute,
            end_minute=row.end_minute,
        )
        for row in rows
    ]


@diary_router.post(
    "/availability",
    response_model=AvailabilityWindowResponse,
    status_code=201,
    summary="Record one recurring block of a provider's week",
    responses=AUTH_ERRORS,
)
async def add_availability(
    body: AvailabilityWindowRequest,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> AvailabilityWindowResponse:
    """Monday = 0 through Sunday = 6; times as minutes from clinic-local midnight.

    Re-recording an identical window updates its end time rather than failing — "make sure
    Tuesday morning is on file" is the useful reading of a repeated call, and a duplicate
    window is not a second clinic.

    A slot must fit inside a *single* window to count as in-hours. Two blocks that abut
    (09:00–13:00 and 13:00–17:00) will flag an appointment spanning one o'clock, deliberately:
    two windows are two clinics, and a consultation running from one into the other is
    something to look at rather than something the arithmetic should quietly join up.
    """
    row = await AppointmentService(db).add_availability(
        account_id=account.id,
        provider_name=body.provider_name,
        weekday=body.weekday,
        start_minute=body.start_minute,
        end_minute=body.end_minute,
    )
    await AuditService(db).record(
        action="provider_availability_recorded",
        account_id=account.id,
        patient_id=None,
        entity_type="provider_availability",
        entity_id=row.id,
        payload={
            "provider_name": row.provider_name,
            "weekday": row.weekday,
            "start_minute": row.start_minute,
            "end_minute": row.end_minute,
        },
    )
    await db.commit()
    return AvailabilityWindowResponse(
        id=row.id,
        provider_name=row.provider_name,
        weekday=row.weekday,
        start_minute=row.start_minute,
        end_minute=row.end_minute,
    )


@diary_router.delete(
    "/availability/{window_id}",
    status_code=204,
    summary="Remove a recorded availability window",
    responses=AUTH_ERRORS | errors(404),
)
async def remove_availability(
    window_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Removes the window. Bookings already made inside it are untouched.

    An availability window is a description of when a provider works, not a permission the
    bookings depend on — withdrawing it must not silently invalidate a diary that has already
    been agreed with patients.
    """
    row = await AppointmentService(db).remove_availability(
        account_id=account.id, window_id=window_id
    )
    await AuditService(db).record(
        action="provider_availability_removed",
        account_id=account.id,
        patient_id=None,
        entity_type="provider_availability",
        entity_id=row.id,
        payload={
            "provider_name": row.provider_name,
            "weekday": row.weekday,
            "start_minute": row.start_minute,
            "end_minute": row.end_minute,
        },
    )
    await db.commit()
