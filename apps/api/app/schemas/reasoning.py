"""Pydantic schemas for the reasoning engine API (Phase 2/3)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.text_sanitize import clean_free_text
from app.schemas.common import (
    AutonomyTier,
    ClinicalOutputType,
    ClinicianDecision,
    ProbabilityBand,
    ReasoningStatus,
)


def _validate_clinical_prose(value: str | None) -> str | None:
    """Clean a clinician-written free-text field, and refuse one that is empty once cleaned.

    The cleaning is :func:`app.core.text_sanitize.clean_free_text` — the same rules the patient
    chart's ``notes`` field has always had, which the reasoning-engine fields did not. Two
    consequences, both real:

    * A U+0000 anywhere in the string cannot be written to a PostgreSQL text column. The
      presenting complaint is the first thing a run stores, so a NUL in it fails the flush and
      the run never starts; in an intake answer it kills the answer *and* the run state written
      alongside it.
    * The presenting complaint and every intake answer are interpolated into agent prompts and
      streamed to the Reasoning Theatre. A C1 escape survives that whole path into whatever
      renders it.

    The emptiness check has to happen *after* cleaning, and it is why this is a validator
    rather than a bare cleaner. ``min_length`` counts raw characters, so a complaint of
    ``"\\x00\\x01"`` satisfied ``min_length=2`` and then cleaned down to ``""`` — an empty
    complaint the panel is asked to reason about, which is the input the length floor exists
    to prevent.
    """
    if value is None:
        return None
    cleaned = clean_free_text(value).strip()
    if not cleaned:
        raise ValueError("must contain some text once control characters are removed")
    return cleaned


class StartReasoningRequest(BaseModel):
    presenting_complaint: str = Field(..., min_length=2, max_length=4000)

    _check_complaint = field_validator("presenting_complaint")(_validate_clinical_prose)


class IntakeQuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question_text: str
    question_type: str
    rationale: str | None
    sequence_order: int
    info_gain_score: float | None
    answered_at: datetime | None


class IntakeAnswerIn(BaseModel):
    question_id: uuid.UUID
    answer_text: str = Field(..., min_length=1, max_length=4000)

    _check_answer = field_validator("answer_text")(_validate_clinical_prose)


class SubmitAnswersRequest(BaseModel):
    answers: list[IntakeAnswerIn] = Field(..., max_length=50)


class StreamTokenOut(BaseModel):
    """Short-lived, session-scoped credential for the SSE query string."""

    token: str
    expires_in: int = Field(..., description="Seconds until the stream token expires")


class ReasoningSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    patient_id: uuid.UUID
    presenting_complaint: str
    status: ReasoningStatus
    # Whether the panel is running *now*, which ``status`` cannot answer on its own. A run
    # killed rather than finished — a Reasoning Theatre tab closing is the ordinary way — leaves
    # ``status`` reading ``reasoning`` with nothing to clear it, while the server will start a
    # new run on that session quite happily. Gate a Run control on this, not on the status, or a
    # clinician is locked out of a case whose run already died. See
    # ``ReasoningSession.run_in_progress``.
    run_in_progress: bool
    autonomy_tier: AutonomyTier | None
    intake_complete: bool
    info_gain_score: float | None
    online: bool
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class IntakeStateOut(BaseModel):
    """Returned after starting a session or submitting answers."""

    session: ReasoningSessionOut
    pending_questions: list[IntakeQuestionOut]
    intake_complete: bool


class CitationOut(BaseModel):
    section_id: str
    source: str
    document_title: str
    heading: str | None = None
    snippet: str | None = None
    score: float | None = None
    corpus_version: str | None = None
    page_range: str | None = None


class ClinicalSuggestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    patient_id: uuid.UUID
    output_type: ClinicalOutputType
    autonomy_tier: AutonomyTier
    confidence_band: ProbabilityBand | None
    title: str
    body: str | None
    evidence: dict
    citations: list
    agent_trace: list
    verifier_verdict: dict
    devils_advocate: dict
    is_hard_block: bool
    cant_miss_flag: bool
    supersedes_id: uuid.UUID | None
    created_at: datetime


class ReasoningResultOut(BaseModel):
    session: ReasoningSessionOut
    suggestions: list[ClinicalSuggestionOut]
    case_state: dict


class DecisionRequest(BaseModel):
    decision: ClinicianDecision
    reason: str | None = Field(default=None, max_length=4000)

    # Optional, unlike the two above, so an all-control-character reason becomes None rather
    # than a 422: the field is not required, and there is nothing for the clinician to fix.
    @field_validator("reason")
    @classmethod
    def _clean_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return clean_free_text(value).strip() or None


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    suggestion_id: uuid.UUID
    decision: ClinicianDecision
    reason: str | None
    created_at: datetime
