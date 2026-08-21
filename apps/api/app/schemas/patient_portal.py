"""Patient-portal schemas — the clinician's grant management, and the patient's own view.

The patient-facing models are the redaction, expressed as types. There is no field here for a
clinician's note, a differential, a diagnosis, a reasoning output or a drug-safety finding — not
filtered out at the last moment, but absent from the shape, so that a column added to any of
those tables later has nowhere to arrive.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.core.portal_redaction import WithholdReason
from app.core.text_sanitize import clean_free_text, clean_identifier
from app.services.patient_portal_service import DEFAULT_GRANT_DAYS, MAX_GRANT_DAYS

MAX_LABEL_CHARS = 200
MIN_REVOCATION_REASON_CHARS = 5
MAX_REVOCATION_REASON_CHARS = 500


class PortalGrantRequest(BaseModel):
    """Issue a read-only portal credential for this chart."""

    days_valid: int = Field(
        default=DEFAULT_GRANT_DAYS,
        ge=1,
        le=MAX_GRANT_DAYS,
        description=(
            "How long the link stays usable. Capped: a read-only link to a medical record that "
            "never expires is one that outlives the reason it was issued and the device it was "
            "opened on."
        ),
    )
    label: str | None = Field(
        default=None,
        max_length=MAX_LABEL_CHARS,
        description=(
            'The practice\'s own note about this credential ("printed at the desk"). Never '
            "shown to the patient."
        ),
    )

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = clean_identifier(value).strip()
        return cleaned or None


class PortalGrantRevokeRequest(BaseModel):
    reason: str = Field(
        ...,
        min_length=MIN_REVOCATION_REASON_CHARS,
        max_length=MAX_REVOCATION_REASON_CHARS,
        description="Why the link is being withdrawn. Kept on the row for the access history.",
    )

    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str) -> str:
        cleaned = clean_free_text(value).strip()
        if len(cleaned) < MIN_REVOCATION_REASON_CHARS:
            raise ValueError(f"must be at least {MIN_REVOCATION_REASON_CHARS} characters")
        return cleaned


class PortalGrantResponse(BaseModel):
    """A credential as the clinician's list shows it. Never carries the token."""

    id: uuid.UUID
    patient_id: uuid.UUID
    issued_by_account_id: uuid.UUID
    label: str | None = None
    expires_at: datetime
    revoked_at: datetime | None = None
    revocation_reason: str | None = None
    last_used_at: datetime | None = Field(
        default=None, description="When the link was last used, or null if it never has been"
    )
    created_at: datetime
    is_live: bool = Field(
        description=(
            "Whether the credential would be accepted right now — neither expired nor withdrawn"
        )
    )


class PortalGrantIssuedResponse(PortalGrantResponse):
    """The response to issuing a credential — the **only** place the token ever appears."""

    token: str = Field(
        description=(
            "The credential, in full, once. It is stored only as a hash, so it cannot be "
            "retrieved again: if it is lost, withdraw this one and issue another."
        )
    )


class PortalGrantListResponse(BaseModel):
    patient_id: uuid.UUID
    grants: list[PortalGrantResponse] = Field(
        default_factory=list,
        description=(
            "Every credential ever issued for this chart, newest first — withdrawn ones "
            "included. The question is who has had access and between when, which a list of "
            "only the live ones answers differently."
        ),
    )


# --- The patient's own view ---------------------------------------------------------------


class PortalPatientResponse(BaseModel):
    """Who the record belongs to, as shown back to them.

    Their own name and date of birth, which they already know — returned so a patient opening a
    link can confirm at a glance that it is *their* record and not somebody else's, which is the
    check that catches a mis-issued credential before anything worse does.
    """

    full_name: str
    date_of_birth: date | None = None
    sex: str | None = None


class PortalLabResponse(BaseModel):
    """One of the patient's own results, or a placeholder where one is being reviewed."""

    lab_result_id: str
    marker_name: str
    sample_date: date | None = None
    released: bool = Field(
        description=(
            "False when the result is with the clinical team. The entry is still listed — a "
            "portal that silently omitted it would be one a patient could not trust, and "
            '"there is a result being looked at" is what prompts the phone call.'
        )
    )
    withheld_reason: WithholdReason | None = None
    withheld_summary: str | None = None
    value_numeric: float | None = None
    value_text: str | None = None
    unit: str | None = None
    reference_low: float | None = None
    reference_high: float | None = None
    is_abnormal: bool | None = Field(
        default=None,
        description=(
            "Whether the report itself marked the value outside its reference interval. A fact "
            "the report carries, not an interpretation of it — what it means is a conversation "
            "with a clinician."
        ),
    )


class PortalLabListResponse(BaseModel):
    results: list[PortalLabResponse] = Field(default_factory=list)
    withheld_count: int = Field(
        description="How many entries in this list are awaiting clinical review"
    )


class PortalMedicationResponse(BaseModel):
    """One current medicine, named the way the patient will recognise it."""

    medication_event_id: str
    name: str = Field(
        description=(
            "As written on their prescription where that is known, rather than the generic "
            'name. A patient handed a strip labelled "Crocin" and shown "Paracetamol" has '
            "been shown a different medicine as far as they can tell."
        )
    )
    dose: str | None = None
    frequency: str | None = None
    route: str | None = None
    status: str
    started_on: date | None = None
    stopped_on: date | None = None


class PortalMedicationListResponse(BaseModel):
    medications: list[PortalMedicationResponse] = Field(default_factory=list)


class PortalAppointmentResponse(BaseModel):
    """One upcoming appointment."""

    id: uuid.UUID
    provider_name: str
    starts_at: datetime
    ends_at: datetime
    modality: str
    appointment_type: str | None = None


class PortalAppointmentListResponse(BaseModel):
    appointments: list[PortalAppointmentResponse] = Field(default_factory=list)
