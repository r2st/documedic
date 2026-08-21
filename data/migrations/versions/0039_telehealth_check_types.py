"""Widen drug_safety_checks.check_type for the three telemedicine findings.

``app.core.telehealth`` produces flags about the *format* of the consultation rather than about
the drug: an injection cannot be given down a telephone line, a baseline INR cannot be in hand
at the moment a remote prescription is written, and on an audio-only call the prescriber has not
seen the patient. Those three types are persisted alongside every other finding in
``drug_safety_checks``, and this table said two things that stopped them.

**The CHECK constraint did not list them**, which is the ordinary half — the same widening 0004,
0011–0018, 0023, 0028, 0029, 0033 and 0034 each performed for their own new type.

**The column was ``VARCHAR(30)``**, which is the half worth recording.
``telehealth_baseline_monitoring_required`` is 39 characters. SQLite does not enforce a VARCHAR
length, so the entire test suite stored the value happily; PostgreSQL would have raised ``value
too long for type character varying(30)`` at the moment a clinician ran a drug check on a
teleconsultation — a route that had never been exercised against the production dialect. Widened
to 50 rather than to exactly 39, and ``tests/test_telehealth_prescribing.py`` now measures every
member of the ``CheckType`` Literal against the column so the next long type fails in the suite
instead of in the clinic.

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "drug_safety_checks"
_CONSTRAINT = "ck_dsc_check_type"

_SHARED = (
    "check_type IN ('drug_interaction', 'contraindication', 'allergy_conflict', "
    "'renal_dose', 'hepatic_dose', 'duplicate_therapy', 'guideline_deviation', "
    "'unevaluated_medication', 'unevaluated_allergy', 'hepatic_severity', "
    "'hepatotoxic_burden', 'unevaluated_condition', 'bleeding_burden', "
    "'geriatric_caution', 'stale_medication', 'unverified_drug_name', "
    "'implausible_dose', 'paediatric_caution', 'dose_out_of_range', "
    "'dose_unit_mismatch', 'unevaluated_dose', 'stale_weight'"
)
_OLD_CHECK = _SHARED + ")"
_NEW_CHECK = _SHARED + (
    ", 'telehealth_in_person_required', 'telehealth_baseline_monitoring_required', "
    "'telehealth_audio_only_initiation')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Widened before the constraint is replaced: a type the column cannot hold is not made
    # storable by permitting it.
    op.alter_column(
        _TABLE,
        "check_type",
        existing_type=sa.String(length=30),
        type_=sa.String(length=50),
        existing_nullable=False,
    )

    # Dropped by name rather than skipped-if-present, because a database created today already
    # carries this revision's version of the constraint from ``create_all`` and a deployed one
    # carries the previous version. See tests/test_migration_chain_postgres.py.
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _NEW_CHECK)


def downgrade() -> None:
    """Narrowing again would orphan any row already written with one of the new types.

    ``drug_safety_checks`` is append-only, so those rows cannot be rewritten or deleted to make
    the old constraint hold — this deletes nothing and simply restores the predicate and the
    column width, which fails loudly if such a row exists rather than silently dropping clinical
    history. Same judgement as 0023, 0028, 0029, 0033 and 0034.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _OLD_CHECK)
    op.alter_column(
        _TABLE,
        "check_type",
        existing_type=sa.String(length=50),
        type_=sa.String(length=30),
        existing_nullable=False,
    )
