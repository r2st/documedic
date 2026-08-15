"""Widen drug_safety_checks.check_type to allow 'stale_medication'.

Nothing in this system ages a medication out. ``is_current`` is written at the merge that
created the row and only a later ``stop`` line clears it, so a five-day antibiotic course lifted
off a 2019 prescription is still current in 2026 — in ``current_meds``, interacting with
everything prescribed since, and shown to the clinician under a heading that says nothing about
when any of it was last documented.

The rows stay and every rule still runs against them: dropping aged medications from the
evaluation would silently shrink the list every hard block is computed from, and patients on
long-term therapy here routinely go a year between documented prescriptions. What is added is
the statement that the list is old, so an absence of flags is read as a fact about the chart
rather than about the patient.

See app.core.safety.check_stale_medications.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_CHECK = (
    "check_type IN ('drug_interaction', 'contraindication', 'allergy_conflict', "
    "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation', "
    "'unevaluated_medication', 'unevaluated_allergy', 'hepatic_severity', "
    "'hepatotoxic_burden', 'unevaluated_condition', 'bleeding_burden', "
    "'geriatric_caution')"
)
_NEW_CHECK = (
    "check_type IN ('drug_interaction', 'contraindication', 'allergy_conflict', "
    "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation', "
    "'unevaluated_medication', 'unevaluated_allergy', 'hepatic_severity', "
    "'hepatotoxic_burden', 'unevaluated_condition', 'bleeding_burden', "
    "'geriatric_caution', 'stale_medication')"
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
