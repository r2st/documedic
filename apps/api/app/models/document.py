"""Document model — uploaded files + extraction metadata."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

ALLOWED_FILE_TYPES = ("pdf", "image/jpeg", "image/png", "image/webp", "image/heic")
EXTRACTION_STATUSES = ("pending", "processing", "completed", "failed", "needs_confirmation")
DOCUMENT_TYPES = (
    "prescription",
    "lab_report",
    "discharge_summary",
    "imaging_report",
    "referral",
    "other",
)


class Document(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Original uploaded file. Source-of-truth for all extracted data."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "file_type IN ('pdf', 'image/jpeg', 'image/png', 'image/webp', 'image/heic')",
            name="ck_documents_file_type",
        ),
        CheckConstraint(
            "extraction_status IN "
            "('pending', 'processing', 'completed', 'failed', 'needs_confirmation')",
            name="ck_documents_extraction_status",
        ),
        # Serves the patient_id lookup for the per-patient document list. The list is sorted
        # newest-first and this index carries created_at DESC, but the planner sorts anyway:
        # measured on PostgreSQL 16 with 800 documents for one patient, it bitmap-scans on
        # patient_id and quicksorts. DocumentService.list has no LIMIT, so the ordering buys
        # no early exit; it would if the list were ever paginated.
        Index("ix_documents_patient_created", "patient_id", text("created_at DESC")),
        # One stored file per chart, enforced by the database and not only by the read-then-insert
        # check in ``DocumentService.upload``.
        #
        # That check is a SELECT on (patient_id, storage_hash_sha256) followed by an INSERT, and
        # two uploads of the same file that overlap inside that window both read a chart without
        # the document and both write one. A double-clicked upload button is enough, and so is a
        # client that retries after a timeout while the first request is still running.
        #
        # What made the duplicate more than untidy is how the SELECT read it back: a
        # ``scalar_one_or_none`` over a pair of matching rows raises ``MultipleResultsFound``,
        # so from the moment the race is lost, *every* subsequent upload of that file — including
        # the re-upload that is the documented recovery for a scan that would not read — answers
        # 500. The chart is left holding one document twice, and the only way to put the file in
        # again is to change its bytes.
        #
        # Partial on ``is_deleted`` for the reason ``uq_lab_results_observation`` is: a withdrawn
        # document must not permanently forbid re-uploading the file it held. Both dialects
        # support partial indexes, and the matching predicate is already on the dedup read.
        Index(
            "uq_documents_patient_hash",
            "patient_id",
            "storage_hash_sha256",
            unique=True,
            sqlite_where=text("is_deleted = 0"),
            postgresql_where=text("is_deleted = false"),
        ),
    )

    # No single-column index: ix_documents_patient_created leads with patient_id.
    # See migration 0009.
    patient_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("patients.id"), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[str] = mapped_column(String(50), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    storage_hash_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    extraction_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default="pending"
    )
    extraction_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    extraction_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    extraction_metadata: Mapped[dict] = mapped_column(
        JSONBType, nullable=False, default=dict, server_default="{}"
    )
    extraction_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extraction_prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ocr_fallback_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
