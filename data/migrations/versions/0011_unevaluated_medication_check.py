"""Widen drug_safety_checks.check_type to allow 'unevaluated_medication'.

Records that part of the chart could not be evaluated, rather than reporting it as clean. A
current medication whose name resolves to no vocabulary row is absent from the drug list every
interaction and duplicate-therapy rule is keyed on, so the check came back with no flags — the
same answer as a chart with nothing to find. See app.core.safety.check_unevaluated_medications.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CHECK = (
    "check_type IN ('drug_interaction', 'contraindication', 'allergy_conflict', "
    "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation')"
)
_NEW_CHECK = (
    "check_type IN ('drug_interaction', 'contraindication', 'allergy_conflict', "
    "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation', "
    "'unevaluated_medication')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("ck_dsc_check_type", "drug_safety_checks", type_="check")
    op.create_check_constraint("ck_dsc_check_type", "drug_safety_checks", _NEW_CHECK)


def downgrade() -> None:
    """Narrowing again would orphan any row already written with the new type.

    ``drug_safety_checks`` is append-only, so those rows cannot be rewritten or deleted to make
    the old constraint hold — the downgrade deletes nothing and simply restores the predicate,
    which fails loudly if such a row exists rather than silently dropping clinical history.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("ck_dsc_check_type", "drug_safety_checks", type_="check")
    op.create_check_constraint("ck_dsc_check_type", "drug_safety_checks", _OLD_CHECK)
