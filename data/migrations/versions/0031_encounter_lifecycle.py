"""The encounter lifecycle, and the freeze that makes a signature mean something.

``encounters`` held a visit's date, type, complaint and notes and nothing about whether anyone
had attested to them. Every row was editable forever, by anything holding the session, with no
record of who wrote what or when it stopped being a working draft. That is the gap this closes:
six columns, five check constraints, two indexes and one trigger.

**The trigger is the point of the revision.** ``ck_encounters_*`` describe what a well-formed
row looks like; only ``trg_encounters_signed_frozen`` says what may *happen* to one. Once
``status`` reaches ``signed`` or ``amended``, the visit's clinical content, the signature and
the amendment link can never change again, and the status itself may only move forward
(``signed`` -> ``amended``). Refused at the database, not only in ``EncounterService``, for the
reason ``clinical_suggestions`` and ``drug_safety_overrides`` already have their own
immutability triggers: an attested clinical statement that a later writer — a new code path, a
data fix, a psql prompt — can quietly rewrite cannot answer the question the record exists to
answer, which is what the chart said at the time a decision was made.

What is deliberately *not* frozen: ``status`` itself, so an amendment can supersede its
original; ``updated_at``, which moves with either; and ``is_deleted``/``deleted_at``, because
withdrawing a chart has to keep working on a chart that holds signed visits. Withdrawal is not
erasure of the note, it is the chart leaving use.

Existing rows backfill to ``draft``. They were never signed — there was no signing — and a
backfill to ``signed`` would both invent an attestation nobody gave and freeze every one of
them against correction.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.db.migration_guards import column_exists, index_exists
from app.db.types import GUID
from app.models.encounter import (
    AMENDED_AT_SET,
    AMENDMENT_COMPLETE,
    ENCOUNTER_STATUSES,
    FROZEN_ON_SIGN,
    SIGNATURE_COMPLETE,
    SIGNED_STATUSES,
)

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "encounters"

# Taken from the model rather than restated, so the constraint the migration installs and the
# one ``create_all`` builds on a fresh database cannot drift into two different rules. Migration
# 0001 builds the schema from live ORM metadata (see the chain test's docstring), which means a
# fresh database arrives here already carrying all of this — hence the existence checks below.
_STATUS_LIST = ", ".join(f"'{s}'" for s in ENCOUNTER_STATUSES)
_SIGNED_LIST = ", ".join(f"'{s}'" for s in SIGNED_STATUSES)

_NEW_COLUMNS: tuple[tuple[str, sa.Column], ...] = (
    (
        "status",
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
    ),
    ("signed_at", sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True)),
    (
        "signed_by_account_id",
        sa.Column("signed_by_account_id", GUID(), nullable=True),
    ),
    ("amended_at", sa.Column("amended_at", sa.DateTime(timezone=True), nullable=True)),
    ("amends_encounter_id", sa.Column("amends_encounter_id", GUID(), nullable=True)),
    ("amendment_reason", sa.Column("amendment_reason", sa.Text(), nullable=True)),
)

_CHECKS: tuple[tuple[str, str], ...] = (
    ("ck_encounters_status", f"status IN ({_STATUS_LIST})"),
    ("ck_encounters_signature_complete", SIGNATURE_COMPLETE),
    ("ck_encounters_amendment_complete", AMENDMENT_COMPLETE),
    ("ck_encounters_amended_at", AMENDED_AT_SET),
    (
        "ck_encounters_amendment_not_self",
        "amends_encounter_id IS NULL OR amends_encounter_id <> id",
    ),
)

_FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("fk_encounters_signed_by_account", "signed_by_account_id", "accounts", "id"),
    ("fk_encounters_amends_encounter", "amends_encounter_id", "encounters", "id"),
)

# One UPDATE guard, built from the model's own list of frozen columns so that adding a clinical
# column to ``Encounter`` without deciding whether a signature covers it is a visible omission
# rather than a silent hole.
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

# DELETE is refused outright for a signed row. The application soft-deletes and never issues
# one, so this closes the hand-written path: a signed note removed from the table takes with it
# the only evidence of what the chart said, and the audit trail's hash chain references the row.
_DELETE_FN = f"""
CREATE OR REPLACE FUNCTION fn_{_TABLE}_signed_undeletable()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IN ({_SIGNED_LIST}) THEN
        RAISE EXCEPTION 'Encounter % is %, so it cannot be deleted. Withdraw the chart instead.',
                        OLD.id, OLD.status;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""

_AMENDMENT_INDEX = "uq_encounters_one_signed_amendment"
_LIST_INDEX = "ix_encounters_patient_date_live"


def _existing_constraints(bind) -> set[str]:
    inspector = sa.inspect(bind)
    return {c["name"] for c in inspector.get_check_constraints(_TABLE) if c.get("name")} | {
        c["name"] for c in inspector.get_foreign_keys(_TABLE) if c.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for name, column in _NEW_COLUMNS:
        # A database created today comes out of 0001 already carrying these, because 0001 builds
        # the schema from live ORM metadata. See app.db.migration_guards.
        if not column_exists(bind, _TABLE, name):
            op.add_column(_TABLE, column)

    # Every pre-existing visit is a draft: nothing could have been signed, because until this
    # revision there was nothing to sign with. The server default covers rows written from here
    # on; this covers the ones already there when the column was added as nullable-with-default
    # on a table that may hold NULLs from a partially-applied earlier attempt.
    op.execute(f"UPDATE {_TABLE} SET status = 'draft' WHERE status IS NULL")

    constraints = _existing_constraints(bind)
    for name, predicate in _CHECKS:
        if name not in constraints:
            # NOT VALID then VALIDATE: a deployment carrying a row that violates one of these
            # stops at the validate step with the row still present to look at, rather than
            # having it rewritten by a repair step nobody reviewed. Backfilled rows are all
            # plain drafts, so in practice every one of these validates immediately.
            op.execute(f"ALTER TABLE {_TABLE} ADD CONSTRAINT {name} CHECK ({predicate}) NOT VALID")
            op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {name}")

    for name, column, target, target_column in _FOREIGN_KEYS:
        if name not in constraints:
            op.create_foreign_key(name, _TABLE, target, [column], [target_column])

    if not index_exists(bind, _TABLE, _AMENDMENT_INDEX):
        op.execute(
            f"CREATE UNIQUE INDEX {_AMENDMENT_INDEX} ON {_TABLE} (amends_encounter_id) "
            f"WHERE amends_encounter_id IS NOT NULL AND status IN ({_SIGNED_LIST}) "
            "AND is_deleted = false"
        )
    if not index_exists(bind, _TABLE, _LIST_INDEX):
        op.execute(
            f"CREATE INDEX {_LIST_INDEX} ON {_TABLE} (patient_id, encounter_date DESC) "
            "WHERE is_deleted = false"
        )

    op.execute(_FREEZE_FN)
    # One command per op.execute: asyncpg runs DDL through a prepared statement, which rejects
    # multi-command strings.
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_signed_frozen ON {_TABLE}")
    op.execute(
        f"""
        CREATE TRIGGER trg_{_TABLE}_signed_frozen
            BEFORE UPDATE ON {_TABLE}
            FOR EACH ROW EXECUTE FUNCTION fn_{_TABLE}_signed_frozen()
        """
    )
    op.execute(_DELETE_FN)
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_signed_undeletable ON {_TABLE}")
    op.execute(
        f"""
        CREATE TRIGGER trg_{_TABLE}_signed_undeletable
            BEFORE DELETE ON {_TABLE}
            FOR EACH ROW EXECUTE FUNCTION fn_{_TABLE}_signed_undeletable()
        """
    )


def downgrade() -> None:
    """Reversible, and it drops signatures with the columns that hold them.

    That is not a defect to work around — a downgrade to 0030 is a return to a schema with no
    concept of a signed encounter, and the honest thing is to say so here rather than to leave
    an orphan column behind. What survives is every encounter and every word of its content;
    what is lost is who attested and when, and the amendment links. An operator rolling back
    across this revision on a database where visits have been signed should dump the six columns
    first — the trigger is dropped before them, so a plain SELECT works.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_signed_undeletable ON {_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS fn_{_TABLE}_signed_undeletable() CASCADE")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_signed_frozen ON {_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS fn_{_TABLE}_signed_frozen() CASCADE")

    op.execute(f"DROP INDEX IF EXISTS {_AMENDMENT_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_LIST_INDEX}")

    for name, _column, _target, _target_column in _FOREIGN_KEYS:
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {name}")
    for name, _predicate in _CHECKS:
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {name}")
    for name, _column in reversed(_NEW_COLUMNS):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
