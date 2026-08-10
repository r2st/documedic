"""Clinical pathway response schemas (reference lookups, not reasoning-engine output)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.schemas.reasoning import CitationOut


class PathwayStageOut(BaseModel):
    key: str
    title: str
    items: list[str]
    citations: list[CitationOut] = []


class ClinicalPathwayOut(BaseModel):
    condition_name: str
    source: str
    autonomy_tier: str = "informational"
    stages: list[PathwayStageOut]


class PatientPathwaysResponse(BaseModel):
    patient_id: uuid.UUID
    pathways: list[ClinicalPathwayOut]
    unmapped_conditions: list[str]
