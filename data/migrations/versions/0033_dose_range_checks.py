"""Widen drug_safety_checks.check_type for the three dose-range findings.

0028 gave the engine ``implausible_dose``, which judges a number in *model-written prose*
against the strengths a product is listed in, at a deliberately enormous 100x ceiling. It says
in its own module docstring that it attempts no judgement about whether a dose is right for a
patient. Nothing judged what a clinician typed or what an OCR pass read off a prescription, so
"Levothyroxine 100 mg once daily" — a thousand times the dose, and a fluent line — was charted,
run past every allergy, interaction and contraindication rule, and reported as checked.

``app.core.dose_range`` closes that, and emits three types rather than one because the
clinician's next action differs for each:

* ``dose_out_of_range`` — a number to confirm (above a maximum, below a minimum, or a weekly
  drug charted daily);
* ``dose_unit_mismatch`` — a *line to re-read against the source document*, which is usually a
  different task and often a different person's;
* ``unevaluated_dose`` — a gap in the chart rather than a finding about the prescription: a
  child on a weight-dosed drug with no weight recorded. The honest answer to "could not check"
  has to be distinguishable from "checked, and fine", which is the same argument
  ``unevaluated_medication``, ``unevaluated_allergy`` and ``unevaluated_condition`` already
  make.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHARED = (
    "check_type IN ('drug_interaction', 'contraindication', 'allergy_conflict', "
    "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation', "
    "'unevaluated_medication', 'unevaluated_allergy', 'hepatic_severity', "
    "'hepatotoxic_burden', 'unevaluated_condition', 'bleeding_burden', "
    "'geriatric_caution', 'stale_medication', 'unverified_drug_name', "
    "'implausible_dose', 'paediatric_caution'"
)
_OLD_CHECK = _SHARED + ")"
_NEW_CHECK = _SHARED + ", 'dose_out_of_range', 'dose_unit_mismatch', 'unevaluated_dose')"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("ck_dsc_check_type", "drug_safety_checks", type_="check")
    op.create_check_constraint("ck_dsc_check_type", "drug_safety_checks", _NEW_CHECK)


def downgrade() -> None:
    """Narrowing again would orphan any row already written with one of the new types.

    ``drug_safety_checks`` is append-only, so those rows cannot be rewritten or deleted to make
    the old constraint hold — the downgrade deletes nothing and simply restores the predicate,
    which fails loudly if such a row exists rather than silently dropping clinical history. Same
    judgement as 0023, 0028 and 0029.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("ck_dsc_check_type", "drug_safety_checks", type_="check")
    op.create_check_constraint("ck_dsc_check_type", "drug_safety_checks", _OLD_CHECK)
