"""Acknowledgement of a critical lab value — the reader the critical-value screen never had.

``app.core.lab_safety`` has detected panic values correctly for several rounds. What was missing
was anyone being told. The screen runs on document approval, writes ``critical_lab_value_detected``
to the audit trail, and answers ``GET /patients/{id}/labs/critical-flags`` on demand — and every
one of those requires somebody to already have that chart open. A potassium of 6.8 extracted
from a report uploaded at 2am produced an audit entry nobody reads and a flag on a chart nobody
is looking at, and then waited for a clinician to guess which patient to check.

This table is what takes a finding *off* the new panel-wide queue
(``LabSafetyService.outstanding_critical_values``), and it is the only thing that does. No
expiry, no auto-dismiss, nothing inferred from having viewed the chart: a queue that empties
itself is a queue that can be empty because nobody looked.

Append-only, like ``clinical_suggestions`` and ``drug_safety_overrides`` (Critical Safety Rule
#7) — hence ``created_at`` with no ``updated_at`` and no soft-delete pair. A mistaken
acknowledgement is corrected by acknowledging again; both rows stay.

``marker_name``/``value``/``unit``/``severity`` duplicate what ``lab_results`` holds, on
purpose. A lab row can be superseded by a corrected result from the same document, and an
acknowledgement that only pointed at the row would then read as though the clinician had seen
the corrected figure. What a clinician attested to is what was in front of them.

``acknowledged_by`` is a name and is NOT ``account_id``. The deployment model here is one
practice login held signed in across a shift and several machines (``session_max_concurrent``
is 10 for exactly that reason), so the account identifies the practice rather than the person —
and the entire value of this row is that a *named* clinician takes responsibility. Same
reasoning as the step-up re-authentication gate in migration 0035.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_guards import index_exists

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "critical_lab_acknowledgements"
_QUEUE_INDEX = "ix_critical_lab_acks_account_lab"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 0001 builds the schema from live ORM metadata, so a database created today already carries
    # this table and a bare create would abort the upgrade. Same guard as 0021/0019/0005.
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
            sa.Column(
                "lab_result_id",
                sa.Uuid(),
                sa.ForeignKey("lab_results.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("marker_name", sa.String(length=200), nullable=False),
            # Numeric, not float: this column is read back and shown to a clinician as the value
            # they acknowledged, and a figure that renders as 6.8000000000000007 undermines the
            # one thing the row exists to record. Matches ``lab_results.value_numeric``.
            sa.Column("value", sa.Numeric(14, 4), nullable=False),
            sa.Column("unit", sa.String(length=50), nullable=True),
            sa.Column("severity", sa.String(length=20), nullable=False),
            sa.Column("acknowledged_by", sa.String(length=200), nullable=False),
            sa.Column("action_note", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
    # The queue's own predicate — "which of this account's critical values are already
    # acknowledged" — read on every queue request against a table that only grows.
    if not index_exists(bind, _TABLE, _QUEUE_INDEX):
        op.create_index(_QUEUE_INDEX, _TABLE, ["account_id", "lab_result_id"])


def downgrade() -> None:
    """Drops the table, and with it every acknowledgement.

    What is lost is the record of which clinician saw which panic value and when — which is
    precisely the record this migration exists to create, so an operator rolling back across it
    on a database in clinical use should dump the table first. Nothing else depends on it: the
    detection side is unaffected, and a queue with no acknowledgement table simply shows every
    critical value as outstanding.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {_QUEUE_INDEX}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
