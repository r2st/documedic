"""LabResult model — lab test results with reference ranges, source-linked."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class LabResult(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Lab test result. Supports both numeric and qualitative (text) values."""

    __tablename__ = "lab_results"
    __table_args__ = (
        CheckConstraint(
            "abnormality_direction IS NULL OR abnormality_direction IN "
            "('high', 'low', 'critical_high', 'critical_low')",
            name="ck_lab_results_abnormality_direction",
        ),
        # Serves the patient_id lookup for the longitudinal-record and lab-safety reads. Labs
        # accumulate per document ingested, so this is the set that grows fastest for a
        # long-running patient.
        #
        # The trailing sort columns were measured NOT to pay when 0008 landed: with 3000 labs
        # for one patient, PostgreSQL 16 bitmap-scanned on patient_id and sorted rather than
        # reading the index in order. That was the correct choice while RecordService.assemble
        # took the whole set -- with no LIMIT there is nothing to stop early on, and the bitmap
        # scan reads the heap sequentially and sorts 3000 rows in 445 kB of work_mem where an
        # ordered index scan would fetch every heap row in index order (random I/O). They were
        # kept for exactly the case the comment named: "if this read ever gains a LIMIT".
        #
        # It has. RecordService.assemble pages every section, so the ordering now has something
        # to stop early on. One caveat before trusting that: the read's ORDER BY ends in the
        # primary key -- a total order is what makes LIMIT/OFFSET a partition rather than a
        # lottery over ties -- and this index does not carry it, so the planner is choosing
        # between an incremental sort over the index prefix and the old bitmap-scan-and-sort.
        # Which it picks at production row counts has NOT been re-measured on PostgreSQL 16.
        #
        # The NULLS LAST is required either way: both readers sort NULLS LAST (sample_date is
        # nullable) and a DESC index in PostgreSQL is NULLS FIRST unless told otherwise.
        # SQLite's parser rejects NULLS LAST inside a CREATE INDEX at any version, hence the
        # PostgreSQL-only emission and the sibling below.
        Index(
            "ix_lab_results_patient_sample_date",
            "patient_id",
            text("sample_date DESC NULLS LAST"),
            "marker_name",
        ).ddl_if(dialect="postgresql"),
        # SQLite sibling of the above, minus the NULLS LAST its parser rejects. It exists so
        # that dropping the redundant single-column ix_lab_results_patient_id (migration 0009)
        # does not leave the dev/test database with patient_id unindexed altogether -- the
        # PostgreSQL-only index above is what covers that lookup in production.
        Index(
            "ix_lab_results_patient_sample_date",
            "patient_id",
            text("sample_date DESC"),
            "marker_name",
        ).ddl_if(dialect="sqlite"),
    )

    # No single-column index: the composite above leads with patient_id on both dialects.
    # See migration 0009.
    patient_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("patients.id"), nullable=False)
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("encounters.id"), nullable=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True
    )
    marker_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    marker_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    value_numeric: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_text: Mapped[str | None] = mapped_column(String(500), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reference_range_low: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    reference_range_high: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    reference_range_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_abnormal: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    abnormality_direction: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sample_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reported_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lab_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extraction_region: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    extraction_confidence: Mapped[dict] = mapped_column(
        JSONBType, nullable=False, default=dict, server_default="{}"
    )
    clinician_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    clinician_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
