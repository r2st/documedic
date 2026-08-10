"""AuditLog model — append-only, SHA-256 hash-chained log of all clinical actions (P1-09)."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

# Recognised action types written to the immutable trail. Authentication events are part of
# the access trail (who reached patient data, and every failed/blocked attempt to).
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
    "critical_lab_value_detected",
    "patient_created",
    "patient_updated",
    "patient_deleted",
    "document_uploaded",
    "extraction_completed",
    "extraction_approved",
    "field_corrected",
    "graph_merged",
    "drug_safety_check",
    "record_exported",
    "reasoning_session_started",
    "reasoning_intake_answered",
    "reasoning_session_completed",
    "reasoning_session_failed",
    "clinical_suggestion_created",
    "clinician_decision_recorded",
    "hard_block_triggered",
    "validation_run_executed",
    "safety_report_filed",
    "regulatory_dossier_generated",
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
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
