"""Longitudinal record + patient-graph entity schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.text_sanitize import clean_free_text, clean_identifier
from app.schemas.common import PaginationMeta


class SourceLink(BaseModel):
    source_document_id: uuid.UUID | None = None
    extraction_region: dict | None = None
    clinician_confirmed: bool = False


class MedicationItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand_name_raw: str | None
    generic_name: str | None
    dose: str | None
    dose_unit: str | None
    frequency: str | None
    route: str | None
    event_type: str
    event_date: date | None
    end_date: date | None
    is_current: bool
    drug_vocabulary_id: uuid.UUID | None
    source_document_id: uuid.UUID | None
    clinician_confirmed: bool


class LabItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    marker_name: str
    value_numeric: Decimal | None
    value_text: str | None
    unit: str | None
    reference_range_low: Decimal | None
    reference_range_high: Decimal | None
    # The interval as the document printed it. A one-sided range ("< 200") reduces to a single
    # bound, so the numeric pair alone cannot tell "the report printed no range" from "the report
    # printed one with an open end" — this is what distinguishes them on the row the clinician
    # reads, and it is already what the record PDF prints.
    reference_range_text: str | None
    is_abnormal: bool | None
    abnormality_direction: str | None
    sample_date: datetime | None
    source_document_id: uuid.UUID | None
    clinician_confirmed: bool


class ConditionItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    condition_name: str
    icd10_code: str | None
    status: str
    onset_date: date | None
    severity: str | None
    source_document_id: uuid.UUID | None
    clinician_confirmed: bool


class AllergyItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    allergen_name: str
    allergen_type: str
    reaction_description: str | None
    severity: str | None
    status: str
    drug_vocabulary_id: uuid.UUID | None
    source_document_id: uuid.UUID | None
    clinician_confirmed: bool


class DerivedMarkerItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    marker_name: str
    value_numeric: Decimal
    unit: str | None
    formula_name: str
    formula_version: str
    is_abnormal: bool | None
    computed_at: datetime


class RecordPagination(BaseModel):
    """Where each section of the record stopped.

    The record is five independent collections, and `limit`/`offset` are applied to each of
    them separately rather than to some flattened whole — there is no single sequence that a
    shared cursor could walk. So a request is a page *of each section*, and this reports the
    outcome per section: the section's `total` before paging, and `has_more` when the client
    is holding a truncated view of it.

    That per-section `has_more` is the reason this is a required field rather than a
    convenience. A clinician looking at a chart cannot be left to infer that the labs they can
    see are all the labs there are, and neither can an agent reading the snapshot: the
    difference between "no further results" and "further results not fetched" has to be
    carried in the payload, not left to the caller's arithmetic on `len(items)`.
    """

    medications: PaginationMeta
    lab_results: PaginationMeta
    conditions: PaginationMeta
    allergies: PaginationMeta
    derived_markers: PaginationMeta


class LongitudinalRecord(BaseModel):
    patient_id: uuid.UUID
    medications: list[MedicationItem]
    lab_results: list[LabItem]
    conditions: list[ConditionItem]
    allergies: list[AllergyItem]
    derived_markers: list[DerivedMarkerItem]
    pagination: RecordPagination = Field(
        ...,
        description=(
            "Per-section paging state. Each section was paged independently with the same "
            "`limit`/`offset`; check `has_more` on the section you care about."
        ),
    )


class CriticalLabFlagItem(BaseModel):
    lab_result_id: uuid.UUID
    marker_name: str
    value: float
    unit: str | None
    severity: str
    summary: str
    details: dict


class UnreadableLabItem(BaseModel):
    """A curated marker the critical-value screen declined to evaluate, and why."""

    lab_result_id: uuid.UUID
    marker_name: str
    value: float
    unit: str | None
    reason: str
    summary: str


class CriticalLabFlagsResponse(BaseModel):
    patient_id: uuid.UUID
    flags: list[CriticalLabFlagItem]
    # Rows with curated thresholds that could not be placed on a scale — an unconvertible unit,
    # or no unit at all on a value that two of this marker's units could both produce. An empty
    # ``flags`` alone reads as "nothing critical"; these are the rows for which nobody looked.
    unreadable: list[UnreadableLabItem] = []


# The floor on an acknowledging clinician's name. Short enough for "Dr Rao", long enough that a
# single keystroke does not clear a panic value off the queue with a signature nobody can read.
MIN_ACKNOWLEDGED_BY_CHARS = 2
MAX_ACKNOWLEDGED_BY_CHARS = 200
MAX_ACTION_NOTE_CHARS = 2000


class CriticalLabAcknowledgementRequest(BaseModel):
    """A named clinician recording that they have seen one critical value.

    ``acknowledged_by`` is required and is not derived from the authenticated account. This
    product's deployment model is one practice login held signed in across a shift and several
    machines, so the account identifies the practice rather than the person — and an
    acknowledgement whose entire value is that a *named* clinician takes responsibility for
    having seen a panic potassium cannot be signed by "the practice". Compare the same
    reasoning behind step-up re-authentication.

    ``action_note`` is optional on purpose. The required part is that somebody named saw the
    value; making the note mandatory puts a text box between a clinician and clearing a queue
    at 2am, and the predictable outcome is a queue cleared with "." rather than a queue
    cleared with a note.
    """

    acknowledged_by: str = Field(
        ...,
        min_length=MIN_ACKNOWLEDGED_BY_CHARS,
        max_length=MAX_ACKNOWLEDGED_BY_CHARS,
        description="Name of the clinician acknowledging this result",
    )
    action_note: str | None = Field(
        default=None,
        max_length=MAX_ACTION_NOTE_CHARS,
        description="What was done about it, in the clinician's own words",
    )

    @field_validator("acknowledged_by")
    @classmethod
    def _name_is_substantive(cls, value: str) -> str:
        # The floor is applied to the *stripped*, control-character-free text and the stripped
        # form is what gets stored, so " " cannot satisfy a min_length that counts raw
        # characters — the same hole that was closed on hard-block override reasoning.
        stripped = clean_identifier(value).strip()
        if len(stripped) < MIN_ACKNOWLEDGED_BY_CHARS:
            raise ValueError(
                f"acknowledged_by must be at least {MIN_ACKNOWLEDGED_BY_CHARS} characters"
            )
        return stripped

    @field_validator("action_note")
    @classmethod
    def _clean_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return clean_free_text(value).strip() or None


class CriticalLabAcknowledgementResponse(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    lab_result_id: uuid.UUID
    marker_name: str
    value: float
    unit: str | None
    severity: str
    acknowledged_by: str
    action_note: str | None
    created_at: datetime


class CriticalQueueEntry(BaseModel):
    """One outstanding critical value on the panel-wide queue."""

    patient_id: uuid.UUID
    lab_result_id: uuid.UUID
    marker_name: str
    value: float
    unit: str | None
    severity: str
    summary: str
    sample_date: datetime | None = Field(
        default=None, description="When the sample was taken, if the report carried a date"
    )
    # How long this has been sitting unacknowledged. The queue's clinical meaning is almost
    # entirely in this number: a panic potassium detected four minutes ago and one detected
    # three days ago call for different responses, and neither the value nor the severity says
    # which of the two a row is.
    detected_days_ago: int | None = None


class CriticalLabQueueResponse(BaseModel):
    entries: list[CriticalQueueEntry]
    # "Nothing outstanding" and "nothing critical anywhere" are different states, and an empty
    # list alone does not distinguish them.
    acknowledged_count: int = 0
    truncated: bool = Field(
        default=False,
        description="True if the scan hit its ceiling; entries beyond it are not shown",
    )
    offline_capable: bool = True
