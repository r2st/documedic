"""Drug-safety check schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

# The longest INN in the WHO list is around 60 characters; Indian brand names with a strength
# qualifier ("Augmentin Duo 625 Tablet") are shorter still. 200 is generous for anything a
# clinician or an OCR pass produces, and short enough that neither of the two things this
# string reaches can be driven by its length -- see the class docstring.
MAX_DRUG_NAME_CHARS = 200
# reference_id is a curated internal key ("DRUG-0042"), not user-authored text.
MAX_DRUG_REFERENCE_ID_CHARS = 64

# The floor on a hard-block override's documented justification, counted after stripping.
MIN_OVERRIDE_REASONING_CHARS = 10


class SafetyCheckRequest(BaseModel):
    """Check a proposed medication against the patient's record.

    Both fields are length-bounded, and neither bound is cosmetic. An unresolved name is
    quoted back verbatim in the "could not be matched" message — deliberately, because the
    clinician needs to see what was actually looked up — so an unbounded name is reflected
    into a clinician-facing toast and into the response body at whatever size it arrived.
    Before the cap a 60 KB name produced a 60 KB error.

    It is also the more expensive half. A name that misses the exact tier falls through to
    rapidfuzz, which scores the query against every brand and generic in the corpus at a cost
    proportional to the query's length — on an endpoint any authenticated clinician can call,
    with a corpus that grows with the product's market coverage.
    """

    drug_reference_id: str | None = Field(
        default=None,
        max_length=MAX_DRUG_REFERENCE_ID_CHARS,
        description="Canonical drug_vocabulary.reference_id",
    )
    drug_name: str | None = Field(
        default=None,
        max_length=MAX_DRUG_NAME_CHARS,
        description="Brand or generic name; resolved via DrugVocabulary",
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
    # Which of the patient's current medications this flag was raised *about*, on the endpoint
    # that evaluates all of them. `GET ../flags` runs every current medication against every
    # other, so a pairwise finding — an interaction, a duplicate therapy — is legitimately
    # raised twice, once from each side of the pair. The service keeps that grouped by drug;
    # the response used to flatten it and discard the drug, leaving a clinician on eight
    # medications with a list of interactions reported twice each and no way to tell which
    # medication any of them belonged to.
    #
    # Null means the flag is not about a particular drug: the chart-level notes (what could not
    # be read, the Child-Pugh window) are statements about the record itself, and are served
    # once for the whole chart rather than under any one medication.
    drug_reference_id: str | None = Field(
        default=None,
        description="Current medication this flag concerns; null for chart-level flags",
    )
    drug_name: str | None = Field(
        default=None,
        description="Generic (INN) name of `drug_reference_id`; null for chart-level flags",
    )


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
        min_length=MIN_OVERRIDE_REASONING_CHARS,
        max_length=2000,
        description="Documented clinical justification for overriding the hard block",
    )

    @field_validator("reasoning")
    @classmethod
    def _reasoning_is_not_blank(cls, value: str) -> str:
        """Apply the floor to the *stripped* text, and store the stripped form.

        ``min_length`` counts raw characters, and ``SafetyService.override_hard_block`` strips
        before storing. Ten spaces satisfied the first and became ``""`` in the second, so the
        one sanctioned path past a hard block -- a documented allergy or absolute
        contraindication, per Critical Safety Rule #3 -- could be walked with no documentation
        at all. The resulting ``drug_safety_overrides`` row recorded that a clinician had
        justified prescribing past the block and held nothing they had said, which is the
        record a CDSCO audit or an adverse-event review reads to reconstruct the decision.

        Stripping here rather than in the service is what keeps the validated value and the
        stored value the same string; the service's own ``.strip()`` is now a no-op it can keep
        for the non-HTTP callers.
        """
        stripped = value.strip()
        if len(stripped) < MIN_OVERRIDE_REASONING_CHARS:
            raise ValueError(
                f"reasoning must be at least {MIN_OVERRIDE_REASONING_CHARS} characters of "
                "actual justification"
            )
        return stripped


class DrugSafetyOverrideResponse(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    drug_safety_check_id: uuid.UUID
    reasoning: str
    created_at: datetime
