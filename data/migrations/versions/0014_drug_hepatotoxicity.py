"""Add drug_vocabulary.hepatotoxicity and allow the 'hepatotoxic_burden' check type.

The curated hepatic_threshold rules cover six drugs, and every one of them is written against a
measured liver panel. Neither of those reaches the two cases this column is for: a drug with a
well-documented liver-injury signal and no threshold rule at all (amoxicillin-clavulanate is the
commonest cause of drug-induced liver injury in the published registries and had none), and
several such drugs stacked on one chart, which no pairwise interaction rule expresses.

NULL means "not curated", never "safe for the liver" — see app.core.safety.check_hepatotoxic_burden.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.db.migration_guards import column_exists

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CHECK = (
    "check_type IN ('drug_interaction', 'contraindication', 'allergy_conflict', "
    "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation', "
    "'unevaluated_medication', 'unevaluated_allergy', 'hepatic_severity')"
)
_NEW_CHECK = (
    "check_type IN ('drug_interaction', 'contraindication', 'allergy_conflict', "
    "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation', "
    "'unevaluated_medication', 'unevaluated_allergy', 'hepatic_severity', "
    "'hepatotoxic_burden')"
)


def upgrade() -> None:
    bind = op.get_bind()
    # 0001 builds the schema from the live ORM models, so a database created today already has
    # this column and a bare ADD COLUMN aborts the upgrade. See app.db.migration_guards.
    if not column_exists(bind, "drug_vocabulary", "hepatotoxicity"):
        op.add_column(
            "drug_vocabulary",
            sa.Column("hepatotoxicity", sa.String(length=30), nullable=True),
        )
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("ck_dsc_check_type", "drug_safety_checks", type_="check")
    op.create_check_constraint("ck_dsc_check_type", "drug_safety_checks", _NEW_CHECK)


def downgrade() -> None:
    """The column drops cleanly; the constraint narrowing does not, by design.

    ``drug_safety_checks`` is append-only, so a row already written as 'hepatotoxic_burden'
    cannot be rewritten or deleted to make the old predicate hold. Restoring it fails loudly if
    such a row exists rather than silently dropping clinical history.
    """
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint("ck_dsc_check_type", "drug_safety_checks", type_="check")
        op.create_check_constraint("ck_dsc_check_type", "drug_safety_checks", _OLD_CHECK)
    op.drop_column("drug_vocabulary", "hepatotoxicity")
