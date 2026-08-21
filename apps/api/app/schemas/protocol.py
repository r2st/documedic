"""Protocol template (order set) schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.core.text_sanitize import clean_identifier

MAX_SELECTED_ITEMS = 64
MAX_APPLICATIONS_ROWS = 200
MIN_CLINICIAN_NAME_CHARS = 2
MAX_CLINICIAN_NAME_CHARS = 200


class InvestigationResponse(BaseModel):
    key: str
    label: str
    marker_name: str | None = None
    rationale: str | None = None


class MedicationResponse(BaseModel):
    key: str
    generic_name: str
    # Labelled as *typical* everywhere it appears. It is a common starting point that exists so
    # the template is usable without a second lookup, and it is checked against this patient's
    # age, weight and renal function like any other proposed dose — the check, not the number,
    # is what says whether it is usable here.
    typical_dose: str | None = None
    dose_unit: str | None = None
    frequency: str | None = None
    route: str | None = None
    note: str | None = None


class FollowUpResponse(BaseModel):
    key: str
    label: str
    interval_days: int
    appointment_type: str | None = None


class OrderSetResponse(BaseModel):
    key: str
    title: str
    condition_name: str
    # "icmr" where the content maps onto the real ICMR STW corpus, "curated" otherwise. A client
    # must not present curated-only content as guideline-cited.
    source: str
    indication: str
    version: str
    investigations: list[InvestigationResponse] = Field(default_factory=list)
    medications: list[MedicationResponse] = Field(default_factory=list)
    follow_ups: list[FollowUpResponse] = Field(default_factory=list)
    guideline_section_ids: list[str] = Field(default_factory=list)


class ProtocolSelectionRequest(BaseModel):
    """Which items of the template to consider.

    Omitting ``selected_keys`` means the whole template, which is the ordinary first read. An
    *empty list* is not the same request and is refused: it is almost always a client that
    failed to send its selection, and answering it with a clean preview of nothing is the wrong
    direction to fail in.
    """

    selected_keys: list[str] | None = Field(
        default=None,
        max_length=MAX_SELECTED_ITEMS,
        description=(
            "Item keys from the template to keep. Omit for all of them. Every item is "
            "deselectable — a template that cannot be unticked is a checkbox that trains "
            "people to tick."
        ),
    )

    @field_validator("selected_keys")
    @classmethod
    def _clean_keys(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return sorted({clean_identifier(key).strip() for key in value if key and key.strip()})


class ProtocolApplyRequest(ProtocolSelectionRequest):
    """Apply the selection to the chart. All of it, or none of it."""

    applied_by: str | None = Field(
        default=None,
        min_length=MIN_CLINICIAN_NAME_CHARS,
        max_length=MAX_CLINICIAN_NAME_CHARS,
        description=(
            "The clinician applying it, by name. Recorded on the medication events as the "
            "prescriber, for the same reason the handover names both clinicians: the account "
            "identifies the practice, not the person."
        ),
    )
    encounter_id: uuid.UUID | None = Field(
        default=None, description="The visit this is being applied at, when there is one."
    )
    follow_up_provider_name: str | None = Field(
        default=None,
        min_length=MIN_CLINICIAN_NAME_CHARS,
        max_length=MAX_CLINICIAN_NAME_CHARS,
        description=(
            "Whose diary the follow-up should go in. Omit to apply the rest without booking "
            "one — the follow-up interval is then recorded as selected but nothing is placed "
            "in the diary."
        ),
    )
    follow_up_starts_at: datetime | None = Field(
        default=None,
        description=(
            "Override the computed follow-up time. Omitted, the booking lands the template's "
            "interval ahead, at the clinic's configured hour."
        ),
    )

    @field_validator("applied_by", "follow_up_provider_name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = clean_identifier(value).strip()
        if len(stripped) < MIN_CLINICIAN_NAME_CHARS:
            raise ValueError(f"must be at least {MIN_CLINICIAN_NAME_CHARS} characters")
        return stripped


class ProtocolFindingResponse(BaseModel):
    """One safety finding standing against the selection."""

    # None for a chart-level note and for an interaction between two proposed drugs, which is
    # about the pair rather than about one line.
    drug: str | None = None
    check_type: str
    severity: str
    is_hard_block: bool
    summary: str
    # The persisted ``drug_safety_checks`` row, where there is one. It is the only handle an
    # override has, so a hard block always carries it.
    check_id: uuid.UUID | None = None


class ProtocolPreviewResponse(BaseModel):
    patient_id: uuid.UUID
    template: OrderSetResponse
    selected_keys: list[str]
    investigations: list[InvestigationResponse]
    medications: list[MedicationResponse]
    follow_ups: list[FollowUpResponse]
    # Hard blocks first, then by severity. The list is read top-down and the thing that stops a
    # prescription belongs at the top of it.
    findings: list[ProtocolFindingResponse]
    is_blocked: bool = Field(
        ...,
        description=(
            "True when a hard block stands against any selected medication. Applying is refused "
            "while it does — deselect that medication, or record an override with reasoning on "
            "the drug-safety screen first."
        ),
    )
    unresolved_medications: list[str] = Field(
        default_factory=list,
        description=(
            "Template medications the drug vocabulary could not identify. Never empty in a "
            "healthy deployment. A drug that cannot be resolved cannot be checked, so applying "
            "is refused while any remain rather than charting a medication that carries the "
            "appearance of having passed the same checks as its neighbours."
        ),
    )


class ProtocolApplicationResponse(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    template_key: str
    # The curated content moves; this says which version was actually applied, so a correction
    # made next month does not rewrite what a clinician ordered today.
    template_version: str
    template_title: str
    selected_keys: list[str]
    ordered_investigations: list[dict]
    medication_event_ids: list[str]
    follow_up_appointment_id: uuid.UUID | None = None
    applied_by: str | None = None
    warning_count: int
    created_at: datetime
