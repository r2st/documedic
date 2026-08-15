"""Longitudinal record + patient-graph entity schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

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
