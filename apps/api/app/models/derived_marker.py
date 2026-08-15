"""DerivedMarker model — computed clinical values (e.g. eGFR), fully reproducible."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class DerivedMarker(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A computed clinical value. Reproducible via formula_name + version + input_values."""

    __tablename__ = "derived_markers"
    __table_args__ = (
        # Every marker this table holds is a computed quantity that is strictly positive by
        # construction — eGFR is the only one today, and it is the sole input to the metformin
        # and renal-dose hard blocks. A zero or negative value is not a low result, it is
        # arithmetic on an input no assay produces (``GraphService._compute_derived_markers``
        # refuses it for that reason), and it would hard-block off a number the formula never
        # supported. Enforced here so the refusal is a property of the chart rather than of one
        # branch in one function.
        CheckConstraint("value_numeric > 0", name="ck_derived_markers_value_positive"),
        # The same inverted-interval invariant ``lab_results`` carries, for the same reason:
        # ``is_abnormal`` is computed against these ends and is read by the record summary the
        # agents reason from.
        CheckConstraint(
            "reference_range_low IS NULL OR reference_range_high IS NULL "
            "OR reference_range_low <= reference_range_high",
            name="ck_derived_markers_reference_range_order",
        ),
    )

    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False, index=True
    )
    source_lab_result_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("lab_results.id"), nullable=True
    )
    marker_name: Mapped[str] = mapped_column(String(255), nullable=False)
    marker_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    value_numeric: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    formula_name: Mapped[str] = mapped_column(String(100), nullable=False)
    formula_version: Mapped[str] = mapped_column(String(20), nullable=False)
    input_values: Mapped[dict] = mapped_column(JSONBType, nullable=False)
    reference_range_low: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    reference_range_high: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    is_abnormal: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
