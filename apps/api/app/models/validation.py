"""Phase 4 models: clinical-validation runs and safety (adverse-event) reports.

These support the CDSCO SaMD evidence pathway: a reproducible validation harness over gold
vignettes, and an append-only safety-reporting register. ValidationRun rows are immutable
records of a harness execution; SafetyReport rows are an append-only adverse-event register.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

_SEVERITIES = ("near_miss", "non_serious", "serious", "sentinel_event")
_REPORT_STATUS = ("open", "under_review", "closed")


class ValidationRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One execution of the clinical-validation harness over the vignette set."""

    __tablename__ = "validation_runs"
    __table_args__ = (
        # ``ValidationService.list_runs`` reads WHERE account_id = ? ORDER BY created_at DESC,
        # and this table had no index at all — so the one endpoint an assessor uses to read the
        # evidence trail was a sequential scan plus a sort. The rows are the expensive kind to
        # scan, too: ``results`` carries per-vignette detail for the whole harness run, so the
        # heap pages this walks are wide even though the filter reads two narrow columns.
        Index("ix_validation_runs_account_created", "account_id", text("created_at DESC")),
    )

    account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=True
    )
    corpus_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    vignette_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Aggregate metrics: top1/top3 diagnostic accuracy, cant_miss recall, citation faithfulness,
    # hard_block correctness, autonomy-tier distribution.
    metrics: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
    # Per-vignette detail for traceability.
    results: Mapped[list] = mapped_column(JSONBType, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class SafetyReport(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only adverse-event / safety-signal register (monitored pilot)."""

    __tablename__ = "safety_reports"
    __table_args__ = (
        CheckConstraint(
            f"severity IN ({', '.join(repr(s) for s in _SEVERITIES)})",
            name="ck_safety_reports_severity",
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _REPORT_STATUS)})",
            name="ck_safety_reports_status",
        ),
        # ``SafetyReportService.list_reports`` reads WHERE account_id = ? ORDER BY
        # created_at DESC. The two indexes this table already had cover the *other* two ways of
        # slicing it (by chart, by severity) and neither leads with account_id, so the register
        # every clinician opens was the one read that scanned the whole table — on a table that
        # is append-only by design and therefore only ever grows.
        Index("ix_safety_reports_account_created", "account_id", text("created_at DESC")),
    )

    account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id"), nullable=True
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=True, index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("reasoning_sessions.id"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    severity: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="open", server_default="open"
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict] = mapped_column(JSONBType, nullable=False, default=dict)
