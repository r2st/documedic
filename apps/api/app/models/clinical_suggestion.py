"""ClinicalSuggestion model — the IMMUTABLE audit record of any clinical output (P2-08).

Critical Safety Rule #7: once written, a ClinicalSuggestion is never UPDATEd or DELETEd.
The migration installs a Postgres BEFORE UPDATE/DELETE trigger that hard-rejects mutation,
mirroring ``audit_logs``. Corrections are NEW rows that reference the original via
``supersedes_id``. Clinician decisions are recorded in the append-only ``clinician_decisions``
table, never by editing the suggestion.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

_OUTPUT_TYPES = ("differential", "cant_miss", "investigation", "management", "safety", "summary")
_TIERS = ("informational", "suggestive", "flag_for_review")
_DECISIONS = ("acknowledged", "accepted", "dismissed", "overridden")


class ClinicalSuggestion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One immutable clinical suggestion emitted by the reasoning engine."""

    __tablename__ = "clinical_suggestions"
    __table_args__ = (
        CheckConstraint(
            f"output_type IN ({', '.join(repr(t) for t in _OUTPUT_TYPES)})",
            name="ck_clinical_suggestions_output_type",
        ),
        CheckConstraint(
            f"autonomy_tier IN ({', '.join(repr(t) for t in _TIERS)})",
            name="ck_clinical_suggestions_tier",
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("reasoning_sessions.id"), nullable=False, index=True
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False, index=True
    )
    output_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    autonomy_tier: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence_band: Mapped[str | None] = mapped_column(String(30), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Evidence for/against, citations, agent trace, verifier verdict, devil's-advocate critique.
    evidence: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    citations: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    agent_trace: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    verifier_verdict: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    devils_advocate: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    is_hard_block: Mapped[bool] = mapped_column(nullable=False, default=False)
    cant_miss_flag: Mapped[bool] = mapped_column(nullable=False, default=False)
    # A correction points back at the suggestion it supersedes (never an in-place edit).
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("clinical_suggestions.id"), nullable=True
    )


class ClinicianDecisionRecord(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only record of a clinician's engagement with a suggestion.

    Kept separate from ``clinical_suggestions`` so that suggestions stay strictly immutable
    while decisions (acknowledge / accept / dismiss / override) are still captured.
    """

    __tablename__ = "clinician_decisions"
    __table_args__ = (
        CheckConstraint(
            f"decision IN ({', '.join(repr(d) for d in _DECISIONS)})",
            name="ck_clinician_decisions_decision",
        ),
    )

    suggestion_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("clinical_suggestions.id"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    # Override of a hard block / flag-for-review requires documented reasoning (Safety Rule #3).
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
