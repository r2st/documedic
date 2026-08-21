"""Widen drug_safety_checks.check_type for the weight-staleness finding.

0032 added ``patients.weight_kg`` and ``weight_recorded_at`` together, and the model's own
comment said why the second column was there: "a paediatric dose calculated from a weight taken
two years ago is calculated from a weight this child has grown out of". Nothing then read it.
``app.core.dose_range`` divided by the number whatever its age, so a nine-year-old weighed at
four cleared every mg/kg ceiling in the table by a wide margin — and cleared it silently, which
is the worst direction for a dose check to fail in: the flag that does not appear reads exactly
like the flag that was not needed.

``app.core.safety.check_weight_staleness`` is the date's reader, and ``stale_weight`` is what it
writes. One type rather than two for the dated and undated cases, because the clinician's next
action is identical — re-weigh the patient — and the details payload carries which of the two it
was.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHARED = (
    "check_type IN ('drug_interaction', 'contraindication', 'allergy_conflict', "
    "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation', "
    "'unevaluated_medication', 'unevaluated_allergy', 'hepatic_severity', "
    "'hepatotoxic_burden', 'unevaluated_condition', 'bleeding_burden', "
    "'geriatric_caution', 'stale_medication', 'unverified_drug_name', "
    "'implausible_dose', 'paediatric_caution', 'dose_out_of_range', "
    "'dose_unit_mismatch', 'unevaluated_dose'"
)
_OLD_CHECK = _SHARED + ")"
_NEW_CHECK = _SHARED + ", 'stale_weight')"


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
    judgement as 0023, 0028, 0029 and 0033.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("ck_dsc_check_type", "drug_safety_checks", type_="check")
    op.create_check_constraint("ck_dsc_check_type", "drug_safety_checks", _OLD_CHECK)
