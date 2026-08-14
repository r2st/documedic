"""DrugSafetyCheck model — append-only results of deterministic drug-safety checks."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import GUID, JSONBType
from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class DrugSafetyCheck(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Result of a deterministic drug-safety check. Append-only (no updated_at/is_deleted)."""

    __tablename__ = "drug_safety_checks"
    __table_args__ = (
        CheckConstraint(
            "check_type IN "
            "('drug_interaction', 'contraindication', 'allergy_conflict', "
            "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation', "
            "'unevaluated_medication', 'unevaluated_allergy', 'hepatic_severity')",
            name="ck_dsc_check_type",
        ),
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical', 'hard_block')",
            name="ck_dsc_severity",
        ),
    )

    reasoning_session_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("patients.id"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("accounts.id"), nullable=False)
    drug_vocabulary_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("drug_vocabulary.id"), nullable=False
    )
    check_type: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(30), nullable=False)
    is_hard_block: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONBType, nullable=False)
    related_entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    drug_interaction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("drug_interactions.id"), nullable=True
    )
    contraindication_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("contraindications.id"), nullable=True
    )
    allergy_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("allergies.id"), nullable=True
    )
