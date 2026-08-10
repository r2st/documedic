"""Pydantic schemas for the reasoning engine API (Phase 2/3)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import (
    AutonomyTier,
    ClinicalOutputType,
    ClinicianDecision,
    ProbabilityBand,
    ReasoningStatus,
)


class StartReasoningRequest(BaseModel):
    presenting_complaint: str = Field(..., min_length=2, max_length=4000)


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


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    suggestion_id: uuid.UUID
    decision: ClinicianDecision
    reason: str | None
    created_at: datetime
