"""Patient model.

Direct identifiers and other DPDP-sensitive free text (full_name, date_of_birth, phone,
address_text, notes) are encrypted at rest via EncryptedString/EncryptedDate (app.db.types) —
transparent to the ORM layer and everything above it: reads decrypt automatically, writes
encrypt automatically. The one consequence is that these columns can no longer be filtered or
indexed at the database level (ciphertext is non-deterministic), so full-text patient search
(PatientService.list) filters in Python after decryption instead of via SQL ILIKE — see there
for the tradeoff.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
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

from app.db.types import GUID, EncryptedDate, EncryptedString
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Patient(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Core patient record. One account manages many patients."""

    __tablename__ = "patients"
    __table_args__ = (
        CheckConstraint("sex IN ('male', 'female', 'other', 'unknown')", name="ck_patients_sex"),
        # Body weight is a divisor: a paediatric dose is milligrams per kilogram per day, so a
        # zero would produce an infinite mg/kg figure and a negative one would produce a
        # negative dose that clears every ceiling. Refused at the table for the reason
        # ``ck_derived_markers_value_positive`` is — the safety engine reads this column, and a
        # value the arithmetic cannot support must not be a property of one write path. The
        # ceiling is a plausibility bound rather than a clinical one: 650 kg is above the
        # heaviest human ever recorded, so anything past it is a transcription error (grams
        # entered as kilograms, a height typed into the weight field) rather than a patient.
        CheckConstraint(
            "weight_kg IS NULL OR (weight_kg > 0 AND weight_kg <= 650)",
            name="ck_patients_weight_kg_plausible",
        ),
        # The patient list -- the most-hit endpoint -- reads
        # WHERE account_id = ? AND is_deleted = false ORDER BY updated_at DESC LIMIT/OFFSET.
        # Partial on the soft-delete flag so the index carries only live rows.
        Index(
            "ix_patients_account_updated_live",
            "account_id",
            text("updated_at DESC"),
            postgresql_where=text("is_deleted = false"),
        ),
    )

    # No single-column index: ix_patients_account_updated_live leads with account_id. It is
    # partial, so it only covers queries carrying `is_deleted = false` -- every account-scoped
    # patient query in the codebase does. See migration 0009.
    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    full_name: Mapped[str] = mapped_column(EncryptedString(), nullable=False)
    date_of_birth: Mapped[date | None] = mapped_column(EncryptedDate(), nullable=True)
    sex: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Body weight in kilograms, and the first measurement this product has ever stored about a
    # patient's body. It exists for one reader: ``app.core.dose_range``, which cannot judge a
    # child's dose without it and says so explicitly rather than passing the dose when it is
    # absent.
    #
    # Not encrypted, unlike the five columns above. Those are direct identifiers and DPDP-
    # sensitive free text; a weight identifies nobody, and encrypting it would cost the ability
    # to filter or aggregate on it for no privacy gained. It is the same judgement ``sex``
    # already takes.
    #
    # ``weight_recorded_at`` is beside it because a weight is a measurement with a date, not a
    # property of the person: a paediatric dose calculated from a weight taken two years ago is
    # calculated from a weight this child has grown out of. Nothing consumes the date yet — the
    # dose check uses the value as the best available — but a value stored with no date can
    # never gain a staleness rule afterwards without a backfill that has nothing to backfill
    # from.
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    weight_recorded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    phone: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    address_text: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    notes: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    consent_given: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    consent_given_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
