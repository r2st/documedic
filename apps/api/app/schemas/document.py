"""Document + extraction schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    file_name: str
    file_type: str
    file_size_bytes: int
    document_type: str | None
    document_date: date | None
    extraction_status: str
    ocr_fallback_used: bool
    page_count: int | None
    created_at: datetime


class ExtractionField(BaseModel):
    """A single extracted field with its confidence and review state."""

    name: str
    value: Any
    confidence: float = Field(..., ge=0.0, le=1.0)
    confidence_band: Literal["high", "medium", "low"]
    needs_confirmation: bool


class ExtractedEntity(BaseModel):
    entity_type: Literal["encounter", "medication", "lab_result", "condition", "allergy"]
    fields: list[ExtractionField]
    region: dict | None = None


class ExtractionResult(BaseModel):
    document_id: uuid.UUID
    document_type: str | None
    model: str | None
    ocr_fallback_used: bool
    entities: list[ExtractedEntity]
    confirmation_required_count: int


# The widest text column an extracted field lands in (medication brand/generic name) is
# String(500), so a longer value cannot be stored. PostgreSQL rejects it outright — an
# unhandled DataError at flush, a 500, and the clinician's whole approval rolled back — while
# SQLite silently keeps it, which is why the test suite never noticed.
MAX_FIELD_VALUE_CHARS = 500

# An extracted clinical field is a scalar: a drug name, a dose, a lab number, a date as text.
# ``value`` used to be ``Any``, which let a client submit a dict or a list and have it merged
# into the patient graph — where the drug resolver calls ``.strip()`` on it and the request
# dies with an AttributeError-turned-500 instead of a 422 naming the bad field.
CorrectedValue = (
    Annotated[str, StringConstraints(max_length=MAX_FIELD_VALUE_CHARS)] | int | float | bool | None
)


class FieldCorrection(BaseModel):
    """A clinician's amendment to one extracted field, applied before the graph merge."""

    entity_index: int = Field(..., ge=0)
    field_name: str = Field(..., max_length=200)
    value: CorrectedValue = Field(
        ..., description="Scalar replacement value; see CorrectedValue for the bounds and why."
    )


class ExtractionApproval(BaseModel):
    """Clinician confirms (optionally corrected) extraction; triggers graph merge."""

    corrections: list[FieldCorrection] = Field(default_factory=list, max_length=500)
    rejected_entity_indexes: list[int] = Field(default_factory=list, max_length=500)
