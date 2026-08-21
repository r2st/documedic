"""The discharge, as an artefact — and the freeze that makes finalising it mean something.

The schema recorded an admission, what was prescribed during it, and a handover between
clinicians. It recorded nothing at all about the transition that produces the discrepancies: the
patient leaving. What they actually went home on was recoverable only by inferring it from
whichever ``medication_events`` rows happened to be ``is_current`` — which is exactly the list
that is wrong when a discharge goes badly, because nothing in the old flow ever wrote to it.

``discharge_summaries`` holds the narrative, the take-home medication list, and the two
snapshots that justified letting it out: the reconciliation against the chart and the readiness
assessment. ``medication_event_ids`` holds the rows finalising wrote, so the chart change and
the document that caused it point at each other in both directions.

Three constraints and two triggers carry the design:

* ``ck_discharge_finalization_complete`` — a finalised row names a clinician and carries a time,
  or it is a draft. A finalised summary missing its finaliser is indistinguishable, on read,
  from one nobody attested to, and who sent this patient home on this list is the whole clinical
  meaning of the row.
* ``ck_discharge_correction_complete`` — a correction says what it supersedes *and* why, or
  neither. A supersedes pointer with no reason is a replacement nobody had to justify.
* ``uq_discharge_summaries_one_final_per_encounter`` — one finalised account of one admission,
  so two clinicians finalising concurrently cannot leave the chart with two competing versions
  of what the patient went home on. Drafts are unconstrained; several people may start one.
* ``trg_discharge_summaries_final_frozen`` — the point of the revision, as it was in 0031 and
  0037. The check constraints describe what a well-formed row looks like; only the trigger says
  what may *happen* to one. Once ``status`` is ``finalized`` the narrative, the medication list,
  both snapshots, the chart rows it wrote and the attestation can never change. Refused at the
  database rather than only in ``DischargeService``, because a discharge summary a later writer
  — a new code path, a data fix, a psql prompt — can quietly rewrite is not evidence of what the
  patient was told (Critical Safety Rule #7).
* ``trg_discharge_summaries_final_undeletable`` — and it cannot be deleted, for the same reason
  a sent handover cannot: a removed discharge summary takes with it the only record that the
  patient was discharged on anything in particular.

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.core.discharge import DISCHARGE_STATUSES
from app.db.migration_guards import index_exists
from app.models.discharge_summary import (
    CORRECTION_COMPLETE,
    FINAL_STATUSES,
    FINALIZATION_COMPLETE,
    FROZEN_ON_FINALIZE,
)

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "discharge_summaries"
_LIST_INDEX = "ix_discharge_patient_created_live"
_ENCOUNTER_INDEX = "uq_discharge_summaries_one_final_per_encounter"

# Taken from the model rather than restated, so the rule this migration installs and the one
# ``create_all`` builds on a fresh database cannot drift into two different rules.
_STATUS_LIST = ", ".join(f"'{s}'" for s in DISCHARGE_STATUSES)
_FINAL_LIST = ", ".join(f"'{s}'" for s in FINAL_STATUSES)

# Built from the model's own list of frozen columns, so that adding a clinical column to
# ``DischargeSummary`` without deciding whether finalising covers it is a visible omission
# rather than a silent hole. Same construction as 0031's and 0037's.
_FROZEN_COMPARISON = "\n           OR ".join(
    f"NEW.{column} IS DISTINCT FROM OLD.{column}" for column in FROZEN_ON_FINALIZE
)

_FREEZE_FN = f"""
CREATE OR REPLACE FUNCTION fn_{_TABLE}_final_frozen()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status NOT IN ({_FINAL_LIST}) THEN
        RETURN NEW;
    END IF;
    IF {_FROZEN_COMPARISON}
    THEN
        RAISE EXCEPTION 'Discharge summary % is %, so its content is immutable. '
                        'Record a correction that supersedes it instead.', OLD.id, OLD.status;
    END IF;
    IF NEW.status NOT IN ({_FINAL_LIST}) THEN
        RAISE EXCEPTION 'Discharge summary % is %, so it cannot return to %.',
                        OLD.id, OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_DELETE_FN = f"""
CREATE OR REPLACE FUNCTION fn_{_TABLE}_final_undeletable()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IN ({_FINAL_LIST}) THEN
        RAISE EXCEPTION 'Discharge summary % is %, so it cannot be deleted. '
                        'Withdraw the chart instead.', OLD.id, OLD.status;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 0001 builds the schema from live ORM metadata, so a database created today already carries
    # this table and a bare create would abort the upgrade. Same guard as 0021/0036/0037.
    if not sa.inspect(bind).has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column(
                "patient_id",
                sa.Uuid(),
                sa.ForeignKey("patients.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("encounter_id", sa.Uuid(), sa.ForeignKey("encounters.id"), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
            sa.Column("admission_reason", sa.Text(), nullable=True),
            sa.Column("hospital_course", sa.Text(), nullable=True),
            sa.Column("discharge_diagnosis", sa.Text(), nullable=True),
            sa.Column("follow_up_instructions", sa.Text(), nullable=True),
            sa.Column("patient_instructions", sa.Text(), nullable=True),
            sa.Column(
                "discharge_medications",
                sa.dialects.postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "reconciliation",
                sa.dialects.postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "readiness",
                sa.dialects.postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "confirmed_stops",
                sa.dialects.postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "medication_event_ids",
                sa.dialects.postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finalized_by", sa.String(length=200), nullable=True),
            sa.Column(
                "finalized_by_account_id",
                sa.Uuid(),
                sa.ForeignKey("accounts.id"),
                nullable=True,
            ),
            sa.Column(
                "supersedes_id",
                sa.Uuid(),
                sa.ForeignKey("discharge_summaries.id"),
                nullable=True,
            ),
            sa.Column("correction_reason", sa.Text(), nullable=True),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.CheckConstraint(f"status IN ({_STATUS_LIST})", name="ck_discharge_status"),
            sa.CheckConstraint(FINALIZATION_COMPLETE, name="ck_discharge_finalization_complete"),
            sa.CheckConstraint(CORRECTION_COMPLETE, name="ck_discharge_correction_complete"),
        )

    if not index_exists(bind, _TABLE, _LIST_INDEX):
        op.execute(
            f"CREATE INDEX {_LIST_INDEX} ON {_TABLE} (patient_id, created_at DESC) "
            "WHERE is_deleted = false"
        )
    if not index_exists(bind, _TABLE, _ENCOUNTER_INDEX):
        op.execute(
            f"CREATE UNIQUE INDEX {_ENCOUNTER_INDEX} ON {_TABLE} (encounter_id) "
            "WHERE encounter_id IS NOT NULL AND status = 'finalized' AND is_deleted = false"
        )

    op.execute(_FREEZE_FN)
    # One command per op.execute: asyncpg runs DDL through a prepared statement, which rejects
    # multi-command strings.
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_final_frozen ON {_TABLE}")
    op.execute(
        f"""
        CREATE TRIGGER trg_{_TABLE}_final_frozen
            BEFORE UPDATE ON {_TABLE}
            FOR EACH ROW EXECUTE FUNCTION fn_{_TABLE}_final_frozen()
        """
    )
    op.execute(_DELETE_FN)
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_final_undeletable ON {_TABLE}")
    op.execute(
        f"""
        CREATE TRIGGER trg_{_TABLE}_final_undeletable
            BEFORE DELETE ON {_TABLE}
            FOR EACH ROW EXECUTE FUNCTION fn_{_TABLE}_final_undeletable()
        """
    )


def downgrade() -> None:
    """Drops the table, and with it every discharge summary ever finalised.

    A downgrade to 0043 is a return to a schema with no concept of a discharge, so there is
    nothing to preserve the rows in. The medication events finalising wrote are ordinary chart
    rows and survive; what is lost is the record that they were one decision. An operator rolling
    back across this on a database in clinical use should dump the table first — the triggers are
    dropped before it, so a plain SELECT and a plain DROP both work.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_final_undeletable ON {_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS fn_{_TABLE}_final_undeletable() CASCADE")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_final_frozen ON {_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS fn_{_TABLE}_final_frozen() CASCADE")
    op.execute(f"DROP INDEX IF EXISTS {_ENCOUNTER_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_LIST_INDEX}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
