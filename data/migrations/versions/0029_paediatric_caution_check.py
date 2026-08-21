"""Widen drug_safety_checks.check_type for 'paediatric_caution'.

The engine has had an age axis since 0018, and it only ever pointed one way. ``geriatric_caution``
answers "this patient is 82 and this drug is on the published criteria for older adults". Nothing
answered the same question about a four-year-old, so a chart carrying aspirin for a child's fever
— an over-the-counter decision in this market, frequently already made before the consultation —
produced no age-related finding at all.

See app.core.safety.check_paediatric_cautions, which is the mirror of check_geriatric_cautions
and, like it, never produces a hard block.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHARED = (
    "check_type IN ('drug_interaction', 'contraindication', 'allergy_conflict', "
    "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation', "
    "'unevaluated_medication', 'unevaluated_allergy', 'hepatic_severity', "
    "'hepatotoxic_burden', 'unevaluated_condition', 'bleeding_burden', "
    "'geriatric_caution', 'stale_medication', 'unverified_drug_name', "
    "'implausible_dose'"
)
_OLD_CHECK = _SHARED + ")"
_NEW_CHECK = _SHARED + ", 'paediatric_caution')"


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
    which fails loudly if such a row exists rather than silently dropping clinical history. Same
    judgement as 0023 and 0028.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("ck_dsc_check_type", "drug_safety_checks", type_="check")
    op.create_check_constraint("ck_dsc_check_type", "drug_safety_checks", _OLD_CHECK)
