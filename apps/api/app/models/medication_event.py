"""MedicationEvent model — drug start/stop/change events, source-linked."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin

MED_EVENT_TYPES = ("start", "stop", "change", "continue", "one_time")


class MedicationEvent(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A drug event. brand_name_raw preserves original text; drug_vocabulary_id normalizes."""

    __tablename__ = "medication_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('start', 'stop', 'change', 'continue', 'one_time')",
            name="ck_medication_events_event_type",
        ),
        # Serves the patient_id lookup for the longitudinal-record read, and carries that
        # read's sort columns (current medications first, then most recent). Measured on
        # PostgreSQL 16 with 1200 events for one patient when 0008 landed, the planner
        # bitmap-scanned on patient_id and sorted -- correct at the time, because
        # RecordService.assemble took the whole set with no LIMIT and so had nothing to stop
        # early on. The trailing columns were kept for "if this read ever gained a LIMIT".
        #
        # It has: assemble pages every section now. Same caveat as
        # ix_lab_results_patient_sample_date -- the read's ORDER BY ends in the primary key so
        # that paging over ties is stable, this index does not carry it, and whether the
        # planner takes an incremental sort over the prefix or falls back to the bitmap scan
        # is unmeasured at production row counts.
        Index(
            "ix_medication_events_patient_current_date",
            "patient_id",
            text("is_current DESC"),
            text("event_date DESC"),
        ),
    )

    # No single-column index: ix_medication_events_patient_current_date leads with patient_id.
    # See migration 0009.
    patient_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("patients.id"), nullable=False)
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("encounters.id"), nullable=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id"), nullable=True
    )
    drug_vocabulary_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("drug_vocabulary.id"), nullable=True, index=True
    )
    brand_name_raw: Mapped[str | None] = mapped_column(String(500), nullable=True)
    generic_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    dose: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dose_unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(100), nullable=True)
    route: Mapped[str | None] = mapped_column(String(50), nullable=True)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    duration_text: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prescriber_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
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
