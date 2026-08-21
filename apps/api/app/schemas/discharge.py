"""Discharge summary request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.text_sanitize import clean_free_text, clean_identifier
from app.schemas.med_reconciliation import (
    ProposedMedicationRequest,
    ReconciliationFlagResponse,
    ReconciliationLineResponse,
)
from app.schemas.safety import SafetyFlagResponse
from app.services.med_reconciliation_service import MAX_PROPOSED_MEDICATIONS

DischargeStatus = Literal["draft", "finalized"]

# Prose ceilings. A hospital course is the longest thing a clinician writes in this product and
# is genuinely long on a complicated admission; the rest are paragraphs.
MAX_COURSE_CHARS = 20000
MAX_SECTION_CHARS = 5000
MIN_CLINICIAN_NAME_CHARS = 2
MAX_CLINICIAN_NAME_CHARS = 200
MAX_CORRECTION_REASON_CHARS = 2000

# No minimum length on any narrative section here, unlike the SBAR fields on a handover. The
# floor that matters is applied at *finalisation* by ``app.core.discharge.REQUIRED_SECTIONS``,
# where it can be reported as a readiness item alongside everything else the clinician has to
# read. Refusing a half-written draft at the schema would mean a discharge summary could not be
# saved and come back to, which is how discharge summaries end up written in one pass at the end
# of a shift — the condition this whole feature exists to improve.


def _clean_prose(value: str | None) -> str | None:
    if value is None:
        return None
    return clean_free_text(value).strip() or None


def _clean_name(value: str) -> str:
    stripped = clean_identifier(value).strip()
    if len(stripped) < MIN_CLINICIAN_NAME_CHARS:
        raise ValueError(f"must be at least {MIN_CLINICIAN_NAME_CHARS} characters")
    return stripped


class DischargeCreateRequest(BaseModel):
    """Start a discharge summary. Everything is optional and nothing is frozen yet."""

    encounter_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "The admission this discharge closes. Optional: outpatient care ends without an "
            "inpatient encounter to point at, and pointing at the nearest visit instead would "
            "be worse than a null."
        ),
    )
    admission_reason: str | None = Field(default=None, max_length=MAX_SECTION_CHARS)
    hospital_course: str | None = Field(default=None, max_length=MAX_COURSE_CHARS)
    discharge_diagnosis: str | None = Field(default=None, max_length=MAX_SECTION_CHARS)
    follow_up_instructions: str | None = Field(default=None, max_length=MAX_SECTION_CHARS)
    patient_instructions: str | None = Field(default=None, max_length=MAX_SECTION_CHARS)
    medications: list[ProposedMedicationRequest] = Field(
        default_factory=list,
        max_length=MAX_PROPOSED_MEDICATIONS,
        description="The take-home medication list, as it stands so far",
    )

    @field_validator(
        "admission_reason",
        "hospital_course",
        "discharge_diagnosis",
        "follow_up_instructions",
        "patient_instructions",
    )
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return _clean_prose(value)


class DischargeUpdateRequest(DischargeCreateRequest):
    """Edit a draft. Refused once finalised — a correction is a new summary.

    ``medications`` replaces the stored list wholesale rather than merging into it. A partial
    update of a medication list is the operation with no safe semantics: "these three lines
    changed" leaves the other lines' absence ambiguous between "unchanged" and "removed", and
    the whole value of the list is that it is complete.
    """

    # Inherits every field. Named separately because it is a different act on a different route
    # and the OpenAPI document should say so.


class DischargePreviewRequest(BaseModel):
    """Nothing. The preview reads the stored draft, so both sides see the same list."""


class DischargeFinalizeRequest(BaseModel):
    """Finalise: attest to the list, confirm every discontinuation, and write the chart.

    ``confirmed_stops`` must equal the set of discontinuations the server computes at
    finalisation, exactly — not a superset and not a subset. A charted drug that has *stopped*
    being absent from the list since the preview (somebody added it back) means the clinician
    confirmed a picture that is no longer current, just as much as a newly absent one does.

    Read the current set from `POST ../preview` immediately before finalising.
    """

    finalized_by: str = Field(
        ...,
        min_length=MIN_CLINICIAN_NAME_CHARS,
        max_length=MAX_CLINICIAN_NAME_CHARS,
        description="Name of the clinician discharging this patient",
    )
    confirmed_stops: list[str] = Field(
        default_factory=list,
        max_length=MAX_PROPOSED_MEDICATIONS,
        description=(
            "Every medication the preview reported as a `stop`, echoed back. Case-insensitive."
        ),
    )
    supersedes_id: uuid.UUID | None = Field(
        default=None,
        description="The finalised summary this one corrects, if it is a correction",
    )
    correction_reason: str | None = Field(
        default=None,
        max_length=MAX_CORRECTION_REASON_CHARS,
        description="Why the superseded summary was wrong. Required with `supersedes_id`.",
    )

    @field_validator("finalized_by")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("correction_reason")
    @classmethod
    def _clean_reason(cls, value: str | None) -> str | None:
        return _clean_prose(value)

    @field_validator("confirmed_stops")
    @classmethod
    def _clean_stops(cls, value: list[str]) -> list[str]:
        # Folded and deduplicated here so the comparison in the service is a set comparison over
        # already-normalised text. Confirming one drug twice is not a mismatch: the clinician
        # confirmed it, and a refusal with nothing clinical behind it is how a safety gate
        # teaches people to work around it.
        return sorted({clean_identifier(name).strip().lower() for name in value if name.strip()})


class ReadinessItemResponse(BaseModel):
    key: str
    severity: Literal["blocking", "advisory"] = Field(
        ...,
        description=(
            "blocking refuses the finalisation; advisory is reported and proceeded past by a "
            "clinician who has read it"
        ),
    )
    summary: str
    count: int
    subjects: list[str] = Field(default_factory=list)


class ChartActionResponse(BaseModel):
    kind: Literal["start", "change", "stop", "continue"] = Field(
        ...,
        description=(
            "What finalising would write for this line. `continue` writes nothing — the chart "
            "already carries the drug at that dose — and is listed so the table is complete."
        ),
    )
    label: str
    reference_id: str | None = None
    dose: str | None = None
    dose_unit: str | None = None
    frequency: str | None = None
    previous_dose: str | None = None


class DischargePreviewResponse(BaseModel):
    """What finalising would do, and what stands in the way. Writes nothing to the chart."""

    patient_id: uuid.UUID
    discharge_summary_id: uuid.UUID
    # Blocking items first. The list is read top-down and what stops a discharge belongs at the
    # top of it. Only non-zero items ever appear — see ``app.core.discharge.ReadinessItem``.
    readiness: list[ReadinessItemResponse]
    is_ready: bool = Field(
        ..., description="False if any readiness item is blocking; finalisation would be refused"
    )
    # The medication events finalising would write, and the ones it deliberately would not.
    chart_actions: list[ChartActionResponse]
    # Every discontinuation, folded, to be echoed back as `confirmed_stops`.
    stops_requiring_confirmation: list[str]
    lines: list[ReconciliationLineResponse]
    list_flags: list[ReconciliationFlagResponse]
    safety_flags: list[SafetyFlagResponse]
    charted_count: int
    proposed_count: int
    reconciled_count: int
    # The readiness rules and the reconciliation are deterministic and run with no LLM
    # (Critical Safety Rule #8): deciding whether it is safe to send a patient home is the last
    # thing that should wait on a provider being reachable.
    offline_capable: bool = True


class DischargeResponse(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    encounter_id: uuid.UUID | None = None
    status: DischargeStatus
    admission_reason: str | None = None
    hospital_course: str | None = None
    discharge_diagnosis: str | None = None
    follow_up_instructions: str | None = None
    patient_instructions: str | None = None
    discharge_medications: list[dict] = Field(default_factory=list)
    # Snapshots taken at finalisation, never re-derived on read. The record's job is to say what
    # was true when the patient went home; a table that recomputed itself would show today's
    # chart. Empty on a draft.
    reconciliation: dict = Field(default_factory=dict)
    readiness: list[ReadinessItemResponse] = Field(default_factory=list)
    confirmed_stops: list[str] = Field(default_factory=list)
    # The chart rows finalising wrote, so the document and the medication events point at each
    # other in both directions.
    medication_event_ids: list[uuid.UUID] = Field(default_factory=list)
    finalized_at: datetime | None = None
    finalized_by: str | None = None
    supersedes_id: uuid.UUID | None = None
    correction_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class DischargeListResponse(BaseModel):
    patient_id: uuid.UUID
    summaries: list[DischargeResponse]
