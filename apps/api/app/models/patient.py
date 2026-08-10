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

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, EncryptedDate, EncryptedString
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Patient(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Core patient record. One account manages many patients."""

    __tablename__ = "patients"
    __table_args__ = (
        CheckConstraint("sex IN ('male', 'female', 'other', 'unknown')", name="ck_patients_sex"),
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
    phone: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    address_text: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    notes: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)
    consent_given: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    consent_given_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
