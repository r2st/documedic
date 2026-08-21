"""Encounter-participation schemas: granting, listing, and the participant's own view."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.encounter_roles import ENCOUNTER_ROLES, EncounterRole
from app.core.text_sanitize import clean_free_text
from app.services.encounter_participant_service import MAX_SHARED_ROWS

MIN_PURPOSE_CHARS = 10
MAX_PURPOSE_CHARS = 500
MIN_REASON_CHARS = 5
MAX_REASON_CHARS = 500


def _require_prose(value: str, *, minimum: int, label: str) -> str:
    cleaned = clean_free_text(value).strip()
    if len(cleaned) < minimum:
        raise ValueError(f"{label} must be at least {minimum} characters")
    return cleaned


class ParticipantAddRequest(BaseModel):
    """Share this consultation with a colleague, in a stated role, for a stated purpose."""

    email: EmailStr = Field(
        description=(
            "The colleague's account address on this system. An address with no account is a "
            "404 rather than a silent success — telling a clinician the record was shared when "
            "it was not is how a consultation ends up being emailed instead."
        )
    )
    role: EncounterRole = Field(
        description=(
            "What they are on this visit. `author` and `supervising` may countersign; "
            "`consulting` and `observing` read only. Neither of the first two can be added to "
            "a note that has already been signed — that would rewrite who attended an attested "
            "visit."
        )
    )
    purpose: str = Field(
        ...,
        min_length=MIN_PURPOSE_CHARS,
        max_length=MAX_PURPOSE_CHARS,
        description=(
            "Why this colleague is being given access. Required: an access grant to a clinical "
            "record that cannot say why it was made is the row nobody can justify at a review."
        ),
    )

    @field_validator("purpose")
    @classmethod
    def _clean_purpose(cls, value: str) -> str:
        return _require_prose(value, minimum=MIN_PURPOSE_CHARS, label="purpose")


class ParticipantRemoveRequest(BaseModel):
    """Withdraw a participation, with the reason it was withdrawn."""

    reason: str = Field(
        ...,
        min_length=MIN_REASON_CHARS,
        max_length=MAX_REASON_CHARS,
        description=(
            "Why access is being withdrawn. Required for the same reason the purpose is — a "
            "withdrawal with no reason is reliably the half of an access review somebody needs."
        ),
    )

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str) -> str:
        return _require_prose(value, minimum=MIN_REASON_CHARS, label="reason")


class ParticipantResponse(BaseModel):
    """One participation, live or historical."""

    id: uuid.UUID
    encounter_id: uuid.UUID
    account_id: uuid.UUID
    display_name: str | None = Field(
        default=None, description="The participating clinician's name, as their account records it"
    )
    email: str
    role: EncounterRole
    may_sign: bool = Field(
        description=(
            "Whether this role may attest to the visit. Derived from the role, never stored."
        )
    )
    purpose: str
    granted_by_account_id: uuid.UUID
    created_at: datetime
    removed_at: datetime | None = None
    removal_reason: str | None = None


class ParticipantListResponse(BaseModel):
    encounter_id: uuid.UUID
    participants: list[ParticipantResponse] = Field(default_factory=list)
    # Restated on every list read rather than left to the client's own copy: this is the
    # vocabulary a UI builds its role picker from, and a picker offering a role the server
    # rejects is a form that cannot be submitted.
    available_roles: list[str] = Field(
        default_factory=lambda: [str(role) for role in ENCOUNTER_ROLES]
    )


class SharedPatientResponse(BaseModel):
    """The minimum a participant needs to know who the visit is about.

    Deliberately not the patient schema. A participant was given one consultation, not a chart,
    and the identifiers that make a record linkable across systems — the date of birth, the
    phone number, the address — are not needed to read a note and would be a copy of somebody's
    demographics sitting in a practice that was never given the chart. DPDP data minimisation,
    expressed as a response model that has nowhere to put them: a field added to
    ``PatientResponse`` later cannot leak through this one, because this one does not inherit
    from it.
    """

    id: uuid.UUID
    full_name: str
    sex: str | None = None
    age_years: int | None = Field(
        default=None,
        description=(
            "Age at the time of the request, derived. The date of birth itself is not shared: "
            "age is what a clinician reads a note against, and a DOB is an identifier."
        ),
    )


class SharedEncounterResponse(BaseModel):
    """One consultation as a participant sees it."""

    id: uuid.UUID
    patient: SharedPatientResponse
    encounter_date: date
    encounter_type: str | None = None
    presenting_complaint: str | None = None
    clinician_notes: str | None = None
    status: str
    signed_at: datetime | None = None
    signed_by_account_id: uuid.UUID | None = None
    amends_encounter_id: uuid.UUID | None = None
    amendment_reason: str | None = None
    my_role: EncounterRole
    may_sign: bool
    shared_at: datetime = Field(description="When this participation was granted")
    purpose: str


class SharedEncounterListResponse(BaseModel):
    encounters: list[SharedEncounterResponse] = Field(default_factory=list)
    limit: int = Field(le=MAX_SHARED_ROWS, description="The ceiling this read applied")
