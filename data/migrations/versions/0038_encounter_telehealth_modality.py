"""How a remote consultation was conducted, and what the visit is billed as.

``teleconsultation`` has been an accepted ``encounters.encounter_type`` since the table existed,
and it carried no consequence whatever: the visit was recorded as remote and then treated
identically to one conducted in clinic. ``telehealth_modality`` is what closes that — a video
call and a telephone call differ in what the prescriber has *seen*, India's Telemedicine
Practice Guidelines draw the line in exactly that place, and ``app.core.telehealth`` turns the
column into prescribing flags. ``billing_code`` rides along because it is the other thing a
consultation record was missing and it belongs to the same row.

Three decisions worth not re-deriving:

**The modality is constrained to a teleconsultation in both directions.**
``ck_encounters_telehealth_modality_matches_type`` is a biconditional, not a one-sided "only on
a teleconsultation". A remote visit that does not say how it was conducted cannot be checked
against the telemedicine rules at all — which is worse than the state before this revision,
because the encounter now *claims* to have been checked — and a modality on an in-clinic visit
is a contradiction rather than extra information.

**No backfill, and none is possible.** A teleconsultation recorded before this revision does not
say whether it was video or audio, and nothing in the record implies it. Defaulting them to
``'video'`` would invent the clinical fact the column exists to carry — the permissive one, at
that. So the constraint is added ``NOT VALID`` first and then validated: a database holding such
a row stops here with the row still present to look at, rather than having it guessed at by a
repair step nobody reviewed. The production database holds no encounter rows at all, so this
validates immediately there.

**The freeze function is rebuilt.** Both columns are in ``FROZEN_ON_SIGN`` — the modality is a
clinical fact the clinician attested to, and the billing code is a financial assertion, which is
if anything the one nobody should be able to alter quietly after the note was signed. 0031 built
``fn_encounters_signed_frozen`` from ``FROZEN_ON_SIGN`` as it stood then, so a database already
stamped at 0031 carries a function that does not mention either column. Adding a frozen column
without replacing that function leaves a signed encounter editable through it.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.db.migration_guards import column_exists
from app.models.encounter import (
    FROZEN_ON_SIGN,
    SIGNED_STATUSES,
    TELEHEALTH_MODALITIES,
    TELEHEALTH_MODALITY_MATCHES_TYPE,
)

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "encounters"

# Taken from the model rather than restated, so the rule this migration installs and the one
# ``create_all`` builds on a fresh database cannot drift into two different rules.
_MODALITY_LIST = ", ".join(f"'{m}'" for m in TELEHEALTH_MODALITIES)
_SIGNED_LIST = ", ".join(f"'{s}'" for s in SIGNED_STATUSES)

_NEW_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    (
        "telehealth_modality",
        sa.Column("telehealth_modality", sa.String(length=20), nullable=True),
    ),
    ("billing_code", sa.Column("billing_code", sa.String(length=50), nullable=True)),
)

_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ck_encounters_telehealth_modality",
        f"telehealth_modality IS NULL OR telehealth_modality IN ({_MODALITY_LIST})",
    ),
    ("ck_encounters_telehealth_modality_matches_type", TELEHEALTH_MODALITY_MATCHES_TYPE),
)

# Rebuilt from the model's current list, exactly as 0031 built it from the list of the day. Same
# construction, so the two revisions cannot produce differently-shaped guards.
_FROZEN_COMPARISON = "\n           OR ".join(
    f"NEW.{column} IS DISTINCT FROM OLD.{column}" for column in FROZEN_ON_SIGN
)

_FREEZE_FN = f"""
CREATE OR REPLACE FUNCTION fn_{_TABLE}_signed_frozen()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status NOT IN ({_SIGNED_LIST}) THEN
        RETURN NEW;
    END IF;
    IF {_FROZEN_COMPARISON}
    THEN
        RAISE EXCEPTION 'Encounter % is %, so its content and signature are immutable. '
                        'Record an amendment instead.', OLD.id, OLD.status;
    END IF;
    IF NEW.status NOT IN ({_SIGNED_LIST}) THEN
        RAISE EXCEPTION 'Encounter % is %, so it cannot return to %.',
                        OLD.id, OLD.status, NEW.status;
    END IF;
    IF OLD.status = 'amended' AND NEW.status <> 'amended' THEN
        RAISE EXCEPTION 'Encounter % has already been amended.', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for name, column in _NEW_COLUMNS:
        # A database created today comes out of 0001 already carrying these, because 0001 builds
        # the schema from live ORM metadata. See app.db.migration_guards.
        if not column_exists(bind, _TABLE, name):
            op.add_column(_TABLE, column)

    for name, predicate in _CHECKS:
        # Dropped first rather than skipped-if-present: a fresh database arrives here with the
        # constraint already built by ``create_all`` and a deployed one without it, and both
        # lineages have to converge on *this* revision's definition. See
        # tests/test_migration_chain_postgres.py, which is the reason this pattern exists.
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {name} CHECK ({predicate}) NOT VALID")
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {name}")

    # Replaced, not created: 0031 already installed this function and its trigger, built from the
    # frozen-column list as it stood then. The trigger keeps pointing at the same name.
    op.execute(_FREEZE_FN)


def downgrade() -> None:
    """Drops both columns, and with them every recorded modality and billing code.

    The freeze function is rebuilt from the frozen-column list *minus* the two columns being
    dropped, because a function that names a column the table no longer has raises at the next
    UPDATE of any signed encounter — which would leave amendments failing on a schema that
    otherwise looks fine.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    dropped = {name for name, _column in _NEW_COLUMNS}
    remaining = "\n           OR ".join(
        f"NEW.{column} IS DISTINCT FROM OLD.{column}"
        for column in FROZEN_ON_SIGN
        if column not in dropped
    )
    op.execute(_FREEZE_FN.replace(_FROZEN_COMPARISON, remaining))

    for name, _predicate in _CHECKS:
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {name}")
    for name, _column in reversed(_NEW_COLUMNS):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
