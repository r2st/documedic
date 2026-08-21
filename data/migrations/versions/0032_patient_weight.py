"""Body weight on the patient record — the input a paediatric dose is calculated from.

``app.core.dose_range`` judges a charted dose against a curated therapeutic range. For an adult
that is a comparison against a fixed ceiling; for a child it is milligrams per kilogram per day,
and there was no kilogram anywhere in this schema. The check therefore had exactly two possible
behaviours for a child, and both were wrong: apply the adult ceiling (which clears a dangerous
dose for a nine-year-old) or say nothing (which is the fail-open shape this codebase has closed
four times already). It now says "this dose could not be checked, record a weight" — which is
only useful if there is somewhere to record one.

Two columns, because a weight is a measurement with a date and not a property of a person. A
paediatric dose calculated from a weight taken two years ago is calculated from a weight the
child has grown out of. Nothing reads ``weight_recorded_at`` yet; it exists now because a value
stored without its date can never gain a staleness rule later without a backfill that has
nothing to backfill from.

Unencrypted, unlike ``full_name``/``date_of_birth``/``phone``/``address_text``/``notes``. Those
are direct identifiers and DPDP-sensitive free text; a weight identifies nobody. Same judgement
``sex`` already takes.

The check constraint is the load-bearing part. Weight is a *divisor* — mg/kg/day — so a zero
produces an infinite dose figure and a negative one produces a dose that clears every ceiling.
The upper bound is a transcription guard rather than a clinical one: 650 kg is above the
heaviest human ever recorded, so past it the number is grams entered as kilograms, or a height
typed into the wrong field.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.db.migration_guards import column_exists

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "patients"
_CHECK = "ck_patients_weight_kg_plausible"
# Restated from ``app.models.patient`` deliberately *not* by import: the model spells this
# predicate once and this migration spells it once, and ``tests/test_migration_chain.py`` holds
# the two together. A migration that imports its own predicate from the model can never fail
# when the model changes, which is the one thing a migration test is for.
_PREDICATE = "weight_kg IS NULL OR (weight_kg > 0 AND weight_kg <= 650)"

_NEW_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    ("weight_kg", sa.Column("weight_kg", sa.Numeric(6, 2), nullable=True)),
    (
        "weight_recorded_at",
        sa.Column("weight_recorded_at", sa.DateTime(timezone=True), nullable=True),
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for name, column in _NEW_COLUMNS:
        # A database created today comes out of 0001 already carrying these: 0001 builds the
        # schema from live ORM metadata. See app.db.migration_guards.
        if not column_exists(bind, _TABLE, name):
            op.add_column(_TABLE, column)

    existing = {c["name"] for c in sa.inspect(bind).get_check_constraints(_TABLE) if c.get("name")}
    if _CHECK not in existing:
        # Every existing row has a NULL weight — the column did not exist a moment ago — so
        # this validates immediately. NOT VALID first anyway, so that a database which somehow
        # holds a violating row stops here with the row still there to look at.
        op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CHECK} CHECK ({_PREDICATE}) NOT VALID")
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_CHECK}")


def downgrade() -> None:
    """Drops the weights. A schema without the columns cannot hold them, and an orphan column
    left behind would be read by nothing and maintained by nobody.

    What is lost is every recorded weight; what is unaffected is every other patient column. An
    operator rolling back across this on a database where weights have been recorded should dump
    ``id, weight_kg, weight_recorded_at`` first.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CHECK}")
    for name, _column in reversed(_NEW_COLUMNS):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
