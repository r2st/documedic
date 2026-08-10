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
        # Matches the longitudinal-record and lab-safety sorts; labs accumulate per document
        # ingested, so this is the set that grows fastest for a long-running patient.
        #
        # Both readers sort with NULLS LAST (sample_date is nullable), and a DESC index in
        # PostgreSQL is NULLS FIRST unless told otherwise -- so the null ordering has to be
        # spelled out for the index to satisfy the ORDER BY. SQLite's parser rejects
        # NULLS LAST inside a CREATE INDEX at any version, so this one is emitted for
        # PostgreSQL only; the test/dev SQLite database simply goes without it.
        Index(
            "ix_lab_results_patient_sample_date",
            "patient_id",
            text("sample_date DESC NULLS LAST"),
            "marker_name",
        ).ddl_if(dialect="postgresql"),
    )

    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False, index=True
    )
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
