"""Shared schemas: enums, pagination, error envelope.

Enums here must stay in sync with packages/shared-types/src/enums.ts.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class AutonomyTier(str, Enum):
    informational = "informational"
    suggestive = "suggestive"
    flag_for_review = "flag_for_review"


class ProbabilityBand(str, Enum):
    high = "high"
    moderate = "moderate"
    low = "low"
    very_low = "very_low"
    insufficient_data = "insufficient_data"


class ExtractionConfidence(str, Enum):
    high = "high"  # >= 0.85
    medium = "medium"  # 0.50 - 0.84
    low = "low"  # < 0.50


class SafetySeverity(str, Enum):
    info = "info"
    warning = "warning"
    critical = "critical"
    hard_block = "hard_block"


class ReasoningStatus(str, Enum):
    """Lifecycle of a reasoning session."""

    created = "created"
    intake = "intake"
    intake_complete = "intake_complete"
    reasoning = "reasoning"
    awaiting_review = "awaiting_review"
    completed = "completed"
    failed = "failed"
    offline_paused = "offline_paused"


class ClinicalOutputType(str, Enum):
    """Kinds of immutable ClinicalSuggestion records."""

    differential = "differential"
    cant_miss = "cant_miss"
    investigation = "investigation"
    management = "management"
    safety = "safety"
    summary = "summary"


class IntakeQuestionType(str, Enum):
    red_flag = "red_flag"
    relevant_negative = "relevant_negative"
    clarifying = "clarifying"
    history = "history"
    exam = "exam"


class SpecialistRole(str, Enum):
    internal_medicine = "internal_medicine"
    cardiology = "cardiology"
    infectious_disease = "infectious_disease"
    primary_care = "primary_care"
    sentinel = "sentinel"


class VerifierStatus(str, Enum):
    """Outcome of the gatekeeper Verifier agent."""

    agree = "agree"
    partial_disagreement = "partial_disagreement"
    major_disagreement = "major_disagreement"


class AvailabilityTier(str, Enum):
    """Indian primary-care facility tier where an investigation is obtainable."""

    phc = "phc"
    chc = "chc"
    district_hospital = "district_hospital"
    referral = "referral"


class ClinicianDecision(str, Enum):
    acknowledged = "acknowledged"
    accepted = "accepted"
    dismissed = "dismissed"
    overridden = "overridden"


class GuidelineSource(str, Enum):
    icmr = "icmr"
    who = "who"
    nice = "nice"


class PaginationMeta(BaseModel):
    total: int
    limit: int
    offset: int
    has_more: bool


class PaginatedResponse[T](BaseModel):
    items: list[T]
    pagination: PaginationMeta


class ErrorResponse(BaseModel):
    """The body of every domain error — see ``main.aether_error_handler``.

    It used to declare a ``detail`` field, which the API has never returned. ``detail`` is the
    internal cause (``AetherError.detail``), and ``aether_error_handler`` deliberately keeps it
    in the log only: it names internals, and on a 401 storm the difference between causes is
    exactly what an attacker would like echoed back. Advertising it invited clients to read a
    field that is never populated, so it is gone.
    """

    code: str = Field(
        ...,
        description=(
            "Stable machine-readable error code. Match on this, not on `message` — the message "
            "is clinician-facing prose and is rewritten whenever it reads badly."
        ),
    )
    message: str = Field(..., description="What the clinician should do next, in plain language")
    request_id: str | None = Field(
        default=None,
        description=(
            "Set on 500 only: the reference that correlates this response with the server log "
            "line. Also on every response as the `X-Request-Id` header."
        ),
    )


class MessageResponse(BaseModel):
    message: str
