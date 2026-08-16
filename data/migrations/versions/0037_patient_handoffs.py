"""SBAR handover, and the freeze that makes sending mean something.

There was no handover artefact in this schema at all. A patient leaving one clinician's care for
another's is the point at which information is most reliably lost, and the record held nothing
about it — not who took the patient on, not what they were told, not whether they confirmed
receipt. What survived a shift change was whatever the outgoing clinician remembered to say.

Four SBAR columns are the cheap half. The two decisions worth not re-deriving are:

**The checklist is computed from the chart, not typed.** ``handoffs.checklist`` is a snapshot of
what the chart was carrying at the moment of sending — unacknowledged panic values, hard blocks
standing against currently charted drugs, documented allergies, live medications, documents
still needing checking against the original — each with the clinician's confirmation. A
free-form checklist is a list of what the outgoing clinician remembered, which is exactly the
faculty handover is failing. The service recomputes it at send time and refuses a send whose
confirmation no longer matches, because a confirmation of a state that no longer holds is worse
than none: it is a signed assertion that somebody reviewed something they never saw.

**The trigger is the point of the revision**, as it was in 0031. ``ck_handoffs_*`` describe what
a well-formed row looks like; only ``trg_patient_handoffs_sent_frozen`` says what may *happen*
to one. Once ``status`` reaches ``sent``, the SBAR text, the two clinician names, the checklist
snapshot and the send timestamp can never change, and the status may only move forward
(``sent`` -> ``acknowledged``). Refused at the database rather than only in ``HandoffService``,
for the same reason ``encounters``, ``clinical_suggestions`` and ``drug_safety_overrides`` each
have one: a clinical statement a later writer — a new code path, a data fix, a psql prompt — can
quietly rewrite cannot answer the question the record exists to answer.

Deliberately not frozen: ``status`` itself, so an acknowledgement can land; the four
acknowledgement columns, which are written *after* the freeze and by the receiving clinician;
``updated_at``; and the soft-delete pair, because withdrawing a chart has to keep working on a
chart holding sent handoffs.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_guards import index_exists
from app.models.handoff import (
    ACKNOWLEDGEMENT_COMPLETE,
    HANDOFF_STATUSES,
    SEND_COMPLETE,
    SENT_STATUSES,
    FROZEN_ON_SEND,
)

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "patient_handoffs"
_LIST_INDEX = "ix_handoffs_patient_created_live"

# Taken from the model rather than restated, so the rule this migration installs and the one
# ``create_all`` builds on a fresh database cannot drift into two different rules.
_STATUS_LIST = ", ".join(f"'{s}'" for s in HANDOFF_STATUSES)
_SENT_LIST = ", ".join(f"'{s}'" for s in SENT_STATUSES)

# Built from the model's own list of frozen columns, so that adding a clinical column to
# ``PatientHandoff`` without deciding whether sending covers it is a visible omission rather
# than a silent hole. Same construction as 0031's.
_FROZEN_COMPARISON = "\n           OR ".join(
    f"NEW.{column} IS DISTINCT FROM OLD.{column}" for column in FROZEN_ON_SEND
)

_FREEZE_FN = f"""
CREATE OR REPLACE FUNCTION fn_{_TABLE}_sent_frozen()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status NOT IN ({_SENT_LIST}) THEN
        RETURN NEW;
    END IF;
    IF {_FROZEN_COMPARISON}
    THEN
        RAISE EXCEPTION 'Handoff % is %, so its content is immutable. '
                        'Record a new handoff instead.', OLD.id, OLD.status;
    END IF;
    IF NEW.status NOT IN ({_SENT_LIST}) THEN
        RAISE EXCEPTION 'Handoff % is %, so it cannot return to %.',
                        OLD.id, OLD.status, NEW.status;
    END IF;
    IF OLD.status = 'acknowledged' AND NEW.status <> 'acknowledged' THEN
        RAISE EXCEPTION 'Handoff % has already been acknowledged.', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# DELETE is refused outright for a sent row, exactly as it is for a signed encounter. The
# application soft-deletes and never issues one; this closes the hand-written path, where a
# removed handover takes with it the only evidence that a patient was handed over at all.
_DELETE_FN = f"""
CREATE OR REPLACE FUNCTION fn_{_TABLE}_sent_undeletable()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IN ({_SENT_LIST}) THEN
        RAISE EXCEPTION 'Handoff % is %, so it cannot be deleted. Withdraw the chart instead.',
                        OLD.id, OLD.status;
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
    # this table and a bare create would abort the upgrade. Same guard as 0021/0036.
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
            sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
            sa.Column("situation", sa.Text(), nullable=False),
            sa.Column("background", sa.Text(), nullable=False),
            sa.Column("assessment", sa.Text(), nullable=False),
            sa.Column("recommendation", sa.Text(), nullable=False),
            sa.Column("from_clinician", sa.String(length=200), nullable=True),
            sa.Column("to_clinician", sa.String(length=200), nullable=True),
            sa.Column(
                "checklist",
                sa.dialects.postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_by", sa.String(length=200), nullable=True),
            sa.Column(
                "acknowledged_by_account_id",
                sa.Uuid(),
                sa.ForeignKey("accounts.id"),
                nullable=True,
            ),
            sa.Column("acknowledgement_note", sa.Text(), nullable=True),
            sa.Column(
                "is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
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
            sa.CheckConstraint(f"status IN ({_STATUS_LIST})", name="ck_handoffs_status"),
            sa.CheckConstraint(SEND_COMPLETE, name="ck_handoffs_send_complete"),
            sa.CheckConstraint(ACKNOWLEDGEMENT_COMPLETE, name="ck_handoffs_ack_complete"),
        )

    if not index_exists(bind, _TABLE, _LIST_INDEX):
        op.execute(
            f"CREATE INDEX {_LIST_INDEX} ON {_TABLE} (patient_id, created_at DESC) "
            "WHERE is_deleted = false"
        )

    op.execute(_FREEZE_FN)
    # One command per op.execute: asyncpg runs DDL through a prepared statement, which rejects
    # multi-command strings.
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_sent_frozen ON {_TABLE}")
    op.execute(
        f"""
        CREATE TRIGGER trg_{_TABLE}_sent_frozen
            BEFORE UPDATE ON {_TABLE}
            FOR EACH ROW EXECUTE FUNCTION fn_{_TABLE}_sent_frozen()
        """
    )
    op.execute(_DELETE_FN)
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_sent_undeletable ON {_TABLE}")
    op.execute(
        f"""
        CREATE TRIGGER trg_{_TABLE}_sent_undeletable
            BEFORE DELETE ON {_TABLE}
            FOR EACH ROW EXECUTE FUNCTION fn_{_TABLE}_sent_undeletable()
        """
    )


def downgrade() -> None:
    """Drops the table, and with it every handover ever recorded.

    A downgrade to 0036 is a return to a schema with no concept of a handover, so there is
    nothing to preserve the rows in. An operator rolling back across this on a database in
    clinical use should dump the table first — the triggers are dropped before it, so a plain
    SELECT and a plain DROP both work.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_sent_undeletable ON {_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS fn_{_TABLE}_sent_undeletable() CASCADE")
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_sent_frozen ON {_TABLE}")
    op.execute(f"DROP FUNCTION IF EXISTS fn_{_TABLE}_sent_frozen() CASCADE")
    op.execute(f"DROP INDEX IF EXISTS {_LIST_INDEX}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
