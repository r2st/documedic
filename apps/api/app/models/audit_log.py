"""AuditLog model — append-only, SHA-256 hash-chained log of all clinical actions (P1-09)."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

# Every action type written to the immutable trail, and nothing else. Authentication events
# are part of the access trail (who reached patient data, and every failed/blocked attempt
# to), and so are the ``*_viewed`` reads: under the DPDP Act the question asked of this trail
# months later is "who saw this patient's data", which a write-only trail cannot answer.
#
# Held to the code by ``tests/test_audit_phi_reads.py``, in both directions. A missing entry
# means an action nobody can look up; a spare one describes disclosure the system never makes.
AUDIT_ACTIONS = (
    "auth_signup",
    "auth_login_success",
    "auth_login_failed",
    "auth_login_locked_out",
    "auth_logout",
    "auth_logout_all",
    "auth_token_refreshed",
    "auth_refresh_token_reuse_detected",
    "auth_session_revoked",
    "auth_session_idle_expired",
    "auth_password_changed",
    "auth_password_change_failed",
    # The reset flow. All four are recorded even though the endpoint tells the caller nothing:
    # the response is deliberately identical whether the address exists, is over its ceiling, or
    # got a token, so the trail is the *only* place a burst of reset attempts against one
    # clinician is visible. ``auth_password_reset_token_reused`` is the sharpest of them — the
    # legitimate holder has no reason to present a spent token twice.
    "auth_password_reset_requested",
    "auth_password_reset_throttled",
    "auth_password_reset_completed",
    "auth_password_reset_token_reused",
    # Retention housekeeping. Dead session rows are deleted; this is what makes their removal
    # accountable, since the rows themselves can no longer say they existed.
    "auth_sessions_purged",
    "auth_reset_tokens_purged",
    "critical_lab_value_detected",
    "critical_lab_value_not_evaluated",
    "patient_created",
    "patient_updated",
    "patient_deleted",
    "document_uploaded",
    "extraction_completed",
    "extraction_failed",
    "extraction_approved",
    "field_corrected",
    "graph_merged",
    "drug_safety_check",
    "drug_safety_hard_block_overridden",
    "reasoning_session_started",
    "reasoning_intake_answered",
    # The opening bookend of one *run* of the panel, distinct from the session being opened:
    # a session is opened once and can be run several times, by clinicians other than the one
    # who opened it. Written in the claim's own transaction, which is the only commit that
    # survives a run killed mid-flight — so this is what says an analysis happened at all when
    # neither of the two records below ever arrives. See ``ReasoningService._claim_for_run``.
    "reasoning_run_started",
    "reasoning_session_completed",
    "reasoning_session_failed",
    "clinical_suggestion_created",
    "clinician_decision_recorded",
    "hard_block_triggered",
    # A hard block that was documented on the chart *while the panel was deliberating*, and so
    # was invisible to it — caught by the re-check that runs before anything is written. Kept
    # apart from ``hard_block_triggered`` because it says something that entry cannot: this
    # run's reasoning was built on a chart that no longer existed by the time it was published.
    "reasoning_chart_changed_under_run",
    "validation_run_executed",
    "safety_report_filed",
    "regulatory_dossier_generated",
    # PHI disclosures. Every endpoint that returns a patient's clinical data to a clinician
    # records one of these, so the trail answers "who saw this" and not only "who changed it".
    "patient_viewed",
    "patient_record_viewed",
    # The widest disclosure this API performs: the whole chart, in one file, leaving the system.
    "patient_record_exported",
    "document_downloaded",
    "document_list_viewed",
    "extraction_viewed",
    "drug_safety_flags_viewed",
    "clinical_suggestions_viewed",
    "patient_pathways_viewed",
)


class AuditLog(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One immutable audit entry.

    Tamper-evidence: ``record_hash = sha256(prev_hash || canonical_payload)``. Each entry
    chains to the previous entry's hash, so any retroactive edit breaks the chain. Rows are
    append-only — the table has no ``updated_at``/``is_deleted`` and the service never updates.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        # The per-patient audit view reads WHERE patient_id = ? ORDER BY sequence DESC with a
        # LIMIT. This table is append-only and never pruned, so it becomes the largest in the
        # system; carrying the sort column in the index keeps that read from degrading.
        Index("ix_audit_logs_patient_sequence", "patient_id", text("sequence DESC")),
    )

    # Monotonic per-table sequence used to order the hash chain deterministically.
    # Assigned by the audit service (max+1) under a row lock, not DB autoincrement,
    # because this is not the primary key.
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=True
    )
    # No single-column index: ix_audit_logs_patient_sequence leads with patient_id and so
    # answers every lookup a plain (patient_id) index could. See migration 0009.
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The record_hash of this patient's *previous* entry — a second chain, running through one
    # chart's rows only, alongside the global one in ``prev_hash``.
    #
    # It exists because the per-patient verification endpoint could not deliver what it says.
    # ``prev_hash`` links a row to whatever was appended before it anywhere in the system, so a
    # walk restricted to one patient cannot check linkage at all: it can only recompute each
    # surviving row's own hash, which a *deleted* row passes trivially. Removing the entry that
    # records who exported a chart is the tamper that matters — nobody edits an audit row, they
    # delete it — and ``GET /patients/{id}/audit/verify`` answered ``chain_valid: true`` for it
    # while its own documentation promised the opposite.
    #
    # NULL on every row written before migration 0025, and excluded from the hash when NULL so
    # those rows still verify byte-identically. A patient's first post-migration entry chains to
    # its legacy predecessor, so the two eras join up rather than restarting.
    patient_prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Deliberately unindexed. Verification recomputes each hash from the row it already holds
    # (app.core.audit_hash) and never searches by hash, so an index here only cost writes on
    # the busiest table in the system. See migration 0009.
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)
