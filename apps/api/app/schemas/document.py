"""Document + extraction schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class FieldCorrection(BaseModel):
    entity_index: int
    field_name: str = Field(..., max_length=200)
    value: Any


class ExtractionApproval(BaseModel):
    """Clinician confirms (optionally corrected) extraction; triggers graph merge."""

    corrections: list[FieldCorrection] = Field(default_factory=list, max_length=500)
    rejected_entity_indexes: list[int] = Field(default_factory=list, max_length=500)
