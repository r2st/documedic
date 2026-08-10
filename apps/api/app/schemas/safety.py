"""Drug-safety check schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class SafetyCheckRequest(BaseModel):
    """Check a proposed medication against the patient's record."""

    drug_reference_id: str | None = Field(
        default=None, description="Canonical drug_vocabulary.reference_id"
    )
    drug_name: str | None = Field(
        default=None, description="Brand or generic name; resolved via DrugVocabulary"
    )

    @model_validator(mode="after")
    def _require_a_drug(self) -> SafetyCheckRequest:
        if not (self.drug_reference_id or (self.drug_name and self.drug_name.strip())):
            raise ValueError("Provide either drug_reference_id or drug_name")
        return self


class SafetyFlagResponse(BaseModel):
    id: uuid.UUID | None = Field(
        default=None,
        description="drug_safety_checks.id -- required to submit a hard-block override",
    )
    check_type: str
    severity: str
    is_hard_block: bool
    summary: str
    details: dict
    drug_interaction_id: uuid.UUID | None = None
    contraindication_id: uuid.UUID | None = None
    allergy_id: uuid.UUID | None = None


class SafetyCheckResponse(BaseModel):
    patient_id: uuid.UUID
    proposed_drug_reference_id: str
    proposed_drug_name: str
    is_blocked: bool = Field(..., description="True if any hard block is present")
    is_hard_block: bool = Field(..., description="Hard blocks cannot be dismissed")
    checked_against: dict = Field(
        ..., description="Counts of meds/allergies/conditions the check ran against"
    )
    flags: list[SafetyFlagResponse]
    offline_capable: bool = True


class ActiveFlagsResponse(BaseModel):
    patient_id: uuid.UUID
    flags: list[SafetyFlagResponse]


class DrugSafetyOverrideRequest(BaseModel):
    """Documented clinician override of a hard-blocked drug-safety check (CLAUDE.md rule #3:
    hard blocks require explicit override with documented reasoning, never a silent bypass)."""

    drug_safety_check_id: uuid.UUID
    reasoning: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="Documented clinical justification for overriding the hard block",
    )


class DrugSafetyOverrideResponse(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    drug_safety_check_id: uuid.UUID
    reasoning: str
    created_at: datetime
