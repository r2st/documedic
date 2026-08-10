"""DrugSafetyOverride model — immutable record of a clinician overriding a hard block."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID
from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class DrugSafetyOverride(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """A clinician's documented override of a hard-blocked drug-safety check.

    CLAUDE.md rule #3: hard blocks cannot be silently bypassed -- prescribing past one
    requires this record (who, which check, and why). Append-only, same as
    clinical_suggestions/clinician_decisions: never UPDATE or DELETE.
    """

    __tablename__ = "drug_safety_overrides"

    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False, index=True
    )
    drug_vocabulary_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("drug_vocabulary.id"), nullable=False
    )
    drug_safety_check_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("drug_safety_checks.id"), nullable=False, index=True
    )
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
