"""ClinicalSuggestion model — the IMMUTABLE audit record of any clinical output (P2-08).

Critical Safety Rule #7: once written, a ClinicalSuggestion is never UPDATEd or DELETEd.
The migration installs a Postgres BEFORE UPDATE/DELETE trigger that hard-rejects mutation,
mirroring ``audit_logs``. Corrections are NEW rows that reference the original via
``supersedes_id``. Clinician decisions are recorded in the append-only ``clinician_decisions``
table, never by editing the suggestion.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

_OUTPUT_TYPES = ("differential", "cant_miss", "investigation", "management", "safety", "summary")
_TIERS = ("informational", "suggestive", "flag_for_review")
# Must stay in step with ``app.agents.state.ProbabilityBand`` and the ``ProbabilityBand`` union
# in packages/shared-types — pinned by tests/test_shared_enums.py.
_BANDS = ("high", "moderate", "low", "very_low", "insufficient_data")
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
        # The third of the three closed-vocabulary columns on this table, and the one that was
        # left unconstrained. It is also the only one written from a value the *model* supplied:
        # ``output_type`` and ``autonomy_tier`` are chosen by our own code, while
        # ``confidence_band`` is ``Hypothesis.probability_band``, normalised by
        # ``agents.util.norm_band`` from whatever the specialist answered. The normaliser is the
        # guard today; this makes it an invariant of the immutable record rather than a promise
        # one function keeps, on the table nothing can UPDATE afterwards to correct.
        #
        # NULL is allowed and meaningful — investigations, management options and hard blocks
        # carry no band (see ``agents.synthesis.build_suggestions``).
        CheckConstraint(
            f"confidence_band IS NULL OR confidence_band IN ({', '.join(repr(b) for b in _BANDS)})",
            name="ck_clinical_suggestions_confidence_band",
        ),
        # Serves the session_id lookup for the per-session suggestion read. Like the other
        # unbounded list reads (documents, labs, medications) that read has no LIMIT, so the
        # trailing created_at does not save it a sort -- the planner filters on session_id and
        # orders the result itself. Kept because the per-session set is small enough that the
        # extra column costs almost nothing, and because the table is append-only across every
        # reasoning run in the system, so the session_id lookup itself has to stay indexed.
        Index("ix_clinical_suggestions_session_created", "session_id", "created_at"),
    )

    # No single-column index: ix_clinical_suggestions_session_created leads with session_id.
    # See migration 0009.
    session_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("reasoning_sessions.id"), nullable=False
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
