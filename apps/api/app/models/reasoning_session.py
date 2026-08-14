"""ReasoningSession model — one run of the 8-agent diagnostic reasoning engine (P2-01).

A session is created from a presenting complaint, passes through an adaptive intake loop,
then the multi-agent pipeline. The final ``case_state`` snapshot captures the complete
CaseState for reproducibility/audit (architecture-design §6.2). Sessions are mutable while
running; the ClinicalSuggestion records they emit are immutable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.db.types import GUID, JSONBType
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

# The one status that means "a pipeline run holds this session". Defined here rather than in
# the service because the lease rule that interprets it lives on the model now, and two spellings
# of the same string is how the reader and the claim would drift apart.
RUNNING = "reasoning"

_STATUSES = (
    "created",
    "intake",
    "intake_complete",
    "reasoning",
    "awaiting_review",
    "completed",
    "failed",
    "offline_paused",
)


class ReasoningSession(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A diagnostic reasoning session over a patient's longitudinal record."""

    __tablename__ = "reasoning_sessions"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _STATUSES)})",
            name="ck_reasoning_sessions_status",
        ),
        CheckConstraint(
            "autonomy_tier IS NULL OR autonomy_tier IN "
            "('informational', 'suggestive', 'flag_for_review')",
            name="ck_reasoning_sessions_tier",
        ),
    )

    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=False, index=True
    )
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("encounters.id"), nullable=True
    )
    presenting_complaint: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="created", server_default="created", index=True
    )
    autonomy_tier: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Highest-information-gain remaining, set by the triage agent each intake round.
    info_gain_score: Mapped[float | None] = mapped_column(nullable=True)
    intake_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    online: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # Full CaseState snapshot (hypotheses, traces, verdicts) at completion — reproducibility.
    case_state: Mapped[dict] = mapped_column(
        JSONBType, nullable=False, default=dict, server_default="{}"
    )
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When a pipeline run last took this session. The single-run claim in
    # ``ReasoningService.run`` compare-and-swaps on this value, so two requests that reach the
    # session at the same moment cannot both start the panel — the loser gets a 409 instead of
    # writing a second, duplicate set of immutable ClinicalSuggestion rows against one session.
    #
    # A separate column rather than ``updated_at``: this one is written explicitly with a Python
    # ``datetime.now(UTC)``, so it always changes by microseconds between two claims. The
    # ``onupdate=func.now()`` on ``updated_at`` is the database's clock, which on SQLite has
    # second resolution — two takeovers inside one second would swap on an unchanged value and
    # both win, which is the race this exists to close.
    #
    # Also the lease: a run killed mid-flight (the Reasoning Theatre's ``EventSource``
    # disconnecting cancels the worker task) leaves the claim standing with nothing to release
    # it. A claim older than ``settings.reasoning_run_lease_minutes`` is treated as abandoned and
    # can be taken over, so a dead run cannot make a case permanently unrunnable.
    run_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def run_in_progress(self) -> bool:
        """Whether a pipeline run holds this session *right now*.

        Not the same question as ``status == "reasoning"``, and the difference is the whole
        reason this exists. A run that was killed rather than finished leaves that status behind
        with nothing to clear it — the ordinary way it happens is a Reasoning Theatre tab
        closing, which cancels the worker with a ``CancelledError`` no failure handler sees. The
        session then reads as running forever while :meth:`ReasoningService.assert_runnable`
        will happily start a new run on it.

        A client cannot be expected to hold both halves of that. Gating a Run button on the
        status — the obvious thing to do — strands the clinician for the whole lease on a case
        whose run already died, which is the failure the lease was added to prevent, moved from
        the server to the browser. So the live answer is computed here, from the same rule the
        claim uses, and serialized alongside the recorded status rather than replacing it: the
        status is still what the record says happened, and this is whether it is happening.

        A NULL ``run_claimed_at`` under a ``reasoning`` status is a session from before the
        column existed, or one whose claim predates it. Nothing can argue the claim is live, so
        the forgiving reading is the only safe one.
        """
        if self.status != RUNNING or self.run_claimed_at is None:
            return False
        # SQLite drops tzinfo on round-trip; a naive timestamp is UTC.
        claimed = self.run_claimed_at
        if claimed.tzinfo is None:
            claimed = claimed.replace(tzinfo=UTC)
        return claimed + timedelta(minutes=settings.reasoning_run_lease_minutes) > datetime.now(UTC)
