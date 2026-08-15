"""Widen drug_safety_checks.check_type for 'unverified_drug_name' and 'implausible_dose'.

Every check type before these is a statement about the patient: this drug against that chart.
These two are statements about a recommendation the system itself generated — that it named a
drug nothing in the vocabulary knows, or a dose that could not be a dose of the drug it named.

They exist because the recommendation is model-written. ``SafetyService.screen_text`` resolves
the drugs a management option names and evaluates each against the chart; an option naming a
drug that does not exist resolves to nothing, every rule is then keyed on an empty set, and the
empty flag list that comes back is indistinguishable from a clean screen. The option reaches the
clinician at the case autonomy tier, carrying a guideline citation, beside options that really
were checked.

See app.core.dose_text and app.core.safety.check_dose_integrity.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SHARED = (
    "check_type IN ('drug_interaction', 'contraindication', 'allergy_conflict', "
    "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation', "
    "'unevaluated_medication', 'unevaluated_allergy', 'hepatic_severity', "
    "'hepatotoxic_burden', 'unevaluated_condition', 'bleeding_burden', "
    "'geriatric_caution', 'stale_medication'"
)
_OLD_CHECK = _SHARED + ")"
_NEW_CHECK = _SHARED + ", 'unverified_drug_name', 'implausible_dose')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("ck_dsc_check_type", "drug_safety_checks", type_="check")
    op.create_check_constraint("ck_dsc_check_type", "drug_safety_checks", _NEW_CHECK)


def downgrade() -> None:
    """Narrowing again would orphan any row already written with the new types.

    ``drug_safety_checks`` is append-only, so those rows cannot be rewritten or deleted to make
    the old constraint hold — the downgrade deletes nothing and simply restores the predicate,
    which fails loudly if such a row exists rather than silently dropping clinical history. Same
    judgement as 0023.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("ck_dsc_check_type", "drug_safety_checks", type_="check")
    op.create_check_constraint("ck_dsc_check_type", "drug_safety_checks", _OLD_CHECK)
