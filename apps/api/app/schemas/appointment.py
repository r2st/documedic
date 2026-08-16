"""Appointment and provider-availability schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.core.text_sanitize import clean_free_text, clean_identifier
from app.schemas.common import AppointmentModality, AppointmentStatus

MIN_PROVIDER_NAME_CHARS = 2
MAX_PROVIDER_NAME_CHARS = 200
MAX_REASON_CHARS = 2000
MAX_APPOINTMENT_TYPE_CHARS = 50
# The diary read's ceiling. Published in the schema (see
# ``test_every_paginated_route_publishes_its_ceiling``) so a client discovers it rather than
# finding out by passing 100000.
MAX_DIARY_ROWS = 500
# How far ahead the reminder derivation will look in one call. A week: long enough for the
# longest lead time this system's defaults use, short enough that the result is a working
# queue rather than a dump of the quarter's diary.
MAX_REMINDER_HORIZON_MINUTES = 7 * 24 * 60

_MINUTES_PER_DAY = 24 * 60


def _clean_name(value: str) -> str:
    stripped = clean_identifier(value).strip()
    if len(stripped) < MIN_PROVIDER_NAME_CHARS:
        raise ValueError(f"must be at least {MIN_PROVIDER_NAME_CHARS} characters")
    return stripped


class AppointmentCreateRequest(BaseModel):
    """Book a slot. Conflicts are detected by the server, not asserted by the client."""

    provider_name: str = Field(
        ...,
        min_length=MIN_PROVIDER_NAME_CHARS,
        max_length=MAX_PROVIDER_NAME_CHARS,
        description="Whose diary this booking belongs in, by name as typed",
    )
    starts_at: datetime
    ends_at: datetime
    modality: AppointmentModality = Field(
        default=AppointmentModality.in_person,
        description=(
            "How the visit will be conducted. `audio` and `video` are not interchangeable: on "
            "an audio-only call the prescriber will not have seen the patient, which restricts "
            "what may be newly prescribed at the encounter that follows."
        ),
    )
    appointment_type: str | None = Field(default=None, max_length=MAX_APPOINTMENT_TYPE_CHARS)
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)

    @field_validator("provider_name")
    @classmethod
    def _clean_provider(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("appointment_type")
    @classmethod
    def _clean_type(cls, value: str | None) -> str | None:
        return None if value is None else (clean_identifier(value).strip() or None)

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str | None) -> str | None:
        return None if value is None else (clean_free_text(value).strip() or None)


class AppointmentRescheduleRequest(BaseModel):
    """Move a booking. Conflict detection excludes the appointment being moved.

    Without that exclusion an appointment shifted by ten minutes always collides with itself,
    and the only way past it would be to cancel first — which opens a real gap in the diary
    that another booking can take while the clinician is still typing.
    """

    starts_at: datetime
    ends_at: datetime
    provider_name: str | None = Field(
        default=None,
        min_length=MIN_PROVIDER_NAME_CHARS,
        max_length=MAX_PROVIDER_NAME_CHARS,
        description="Hand the booking to a different provider; omit to keep the current one",
    )

    @field_validator("provider_name")
    @classmethod
    def _clean_provider(cls, value: str | None) -> str | None:
        return None if value is None else _clean_name(value)


class AppointmentCancelRequest(BaseModel):
    """Cancel a booking and free the slot.

    The reason is required and the requirement is not bureaucratic: a cancelled appointment is
    the one status that makes the time re-bookable, so it is the status somebody will reach for
    to tidy a mistake. "Booked in error" and "patient declined follow-up" are different facts
    about a chart, and only the second of them is clinical.
    """

    reason: str = Field(..., min_length=3, max_length=MAX_REASON_CHARS)

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str) -> str:
        stripped = clean_free_text(value).strip()
        if len(stripped) < 3:
            raise ValueError("must be at least 3 characters of actual content")
        return stripped


class AppointmentOutcomeRequest(BaseModel):
    """Close a past booking as attended or not attended.

    Both outcomes are recorded, and the un-attended one is the one worth having: a patient who
    does not come to a follow-up they were booked for is a clinical event, and a diary that
    only records attendance cannot distinguish it from a visit nobody got round to closing.
    """

    attended: bool = Field(
        ...,
        description="True closes the booking as `completed`; false records it as `no_show`.",
    )


class AppointmentResponse(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    provider_name: str
    starts_at: datetime
    ends_at: datetime
    status: AppointmentStatus
    modality: AppointmentModality
    appointment_type: str | None = None
    reason: str | None = None
    source_protocol_key: str | None = None
    cancelled_at: datetime | None = None
    cancellation_reason: str | None = None
    # ``None`` when the slot sits inside recorded hours; ``availability_not_recorded`` when this
    # provider has no hours on file at all, which is deliberately distinct from "outside them" —
    # a question that could not be asked is never reported as one that passed.
    availability_note: str | None = Field(
        default=None,
        description=(
            "`outside_availability` when the slot falls outside this provider's recorded "
            "hours, `availability_not_recorded` when none are on file, absent otherwise. "
            "Advisory — a booking outside recorded hours is accepted and flagged, because "
            "clinics run late and a refusal would be wrong more often than right."
        ),
    )
    created_at: datetime


class AvailabilityWindowRequest(BaseModel):
    """One recurring block of a provider's week, in clinic-local time."""

    provider_name: str = Field(
        ..., min_length=MIN_PROVIDER_NAME_CHARS, max_length=MAX_PROVIDER_NAME_CHARS
    )
    weekday: int = Field(..., ge=0, le=6, description="Monday = 0 through Sunday = 6")
    start_minute: int = Field(
        ..., ge=0, lt=_MINUTES_PER_DAY, description="Minutes from local midnight, e.g. 540 = 09:00"
    )
    end_minute: int = Field(
        ..., gt=0, le=_MINUTES_PER_DAY, description="Minutes from local midnight, exclusive"
    )

    @field_validator("provider_name")
    @classmethod
    def _clean_provider(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("end_minute")
    @classmethod
    def _after_start(cls, value: int, info: ValidationInfo) -> int:
        start = info.data.get("start_minute")
        if start is not None and value <= start:
            raise ValueError("end_minute must be after start_minute")
        return value


class AvailabilityWindowResponse(BaseModel):
    id: uuid.UUID
    provider_name: str
    weekday: int
    start_minute: int
    end_minute: int


class ReminderResponse(BaseModel):
    """One reminder that falls due — derived from the diary, never sent by this system."""

    appointment_id: uuid.UUID
    patient_id: uuid.UUID
    provider_name: str
    appointment_starts_at: datetime
    due_at: datetime
    lead_minutes: int


class ReminderQueueResponse(BaseModel):
    generated_at: datetime
    horizon_minutes: int
    items: list[ReminderResponse]
    # Stated in the payload and not only in the prose, because a client that renders this as a
    # "reminders sent" list would be publishing a claim the system cannot support.
    delivery_channel: str = Field(
        default="none",
        description=(
            "Always `none`. This deployment has no messaging transport; the queue is a "
            "derivation from the diary for a person or an integration to work."
        ),
    )
