"""Drug safety reference data: vocabulary, interactions, contraindications."""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import JSONBType
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DrugVocabulary(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Maps Indian brand names -> generic names -> reference identifiers."""

    __tablename__ = "drug_vocabulary"
    __table_args__ = (UniqueConstraint("reference_id", name="uq_drug_vocabulary_reference_id"),)

    brand_name: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    generic_name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    reference_id: Mapped[str] = mapped_column(String(100), nullable=False)
    atc_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    drug_class: Mapped[str | None] = mapped_column(String(255), nullable=True)
    strength: Mapped[str | None] = mapped_column(String(100), nullable=True)
    form: Mapped[str | None] = mapped_column(String(100), nullable=True)
    manufacturer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Known potential for drug-induced liver injury, as a curated tier — 'established' for the
    # drugs with a well-documented DILI signal, 'dose_dependent' for the ones whose injury is
    # predictable from exposure. NULL means "not curated here", never "safe for the liver":
    # absence of evidence in a fifty-drug seed file is not evidence of absence, and
    # ``check_hepatotoxic_burden`` is written so that a NULL contributes nothing rather than
    # counting as a clean drug.
    hepatotoxicity: Mapped[str | None] = mapped_column(String(30), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    source: Mapped[str] = mapped_column(
        String(100), nullable=False, default="curated", server_default="curated"
    )
    source_version: Mapped[str | None] = mapped_column(String(50), nullable=True)


class DrugInteraction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Known drug-drug interactions. severity=contraindicated => HARD BLOCK."""

    __tablename__ = "drug_interactions"
    __table_args__ = (
        UniqueConstraint(
            "drug_a_reference_id", "drug_b_reference_id", name="uq_drug_interactions_pair"
        ),
        CheckConstraint(
            "severity IN ('minor', 'moderate', 'major', 'contraindicated')",
            name="ck_drug_interactions_severity",
        ),
        CheckConstraint(
            "evidence_level IS NULL OR evidence_level IN "
            "('established', 'probable', 'suspected', 'theoretical')",
            name="ck_drug_interactions_evidence_level",
        ),
    )

    # No single-column index: uq_drug_interactions_pair leads with drug_a_reference_id.
    # See migration 0009.
    drug_a_reference_id: Mapped[str] = mapped_column(String(100), nullable=False)
    drug_b_reference_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(30), nullable=False)
    interaction_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    clinical_effect: Mapped[str | None] = mapped_column(Text, nullable=True)
    management: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_level: Mapped[str | None] = mapped_column(String(30), nullable=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


class Contraindication(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Drug-condition contraindications. is_absolute=True => HARD BLOCK."""

    __tablename__ = "contraindications"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('relative', 'absolute', 'dose_adjustment_required')",
            name="ck_contraindications_severity",
        ),
    )

    drug_reference_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    condition_name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    icd10_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    severity: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    is_absolute: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    renal_threshold: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    hepatic_threshold: Mapped[dict | None] = mapped_column(JSONBType, nullable=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
