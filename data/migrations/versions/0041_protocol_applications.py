"""The record of a curated order set being applied to a chart.

Applying a protocol template is the highest-leverage single request in this product: it can
chart several medications, list a workup and put a booking in the diary in one act. What it
leaves behind afterwards is ordinary rows — medication events indistinguishable from ones typed
in one at a time, an appointment indistinguishable from one booked by hand — and the question a
later reader asks is precisely the one those rows cannot answer: *were these five things one
decision?*

Two columns are the reason this is a table rather than an audit entry.

**``template_version``.** ``app.core.order_sets.VERSION`` moves whenever the curated content
changes. Without the version on the row, a template corrected next month silently rewrites the
history of every application made before the correction — the record would say the clinician
applied today's content. The same reasoning puts a corpus version on a guideline citation and
``extraction_prompt_version`` on a document.

**``selected_keys``.** What the clinician *kept*, which is not recoverable from the template
plus the charted rows: a deselected investigation leaves no trace anywhere. What somebody chose
not to order is a clinical decision, and this is the only place it is written down.

Append-only — ``created_at`` with no ``updated_at`` and no soft-delete pair — like
``clinical_suggestions``, ``drug_safety_overrides`` and ``critical_lab_acknowledgements``
(Critical Safety Rule #7). Undoing an application means stopping the medications and cancelling
the appointment, each its own recorded act; it does not mean deleting the statement that
somebody applied it.

No immutability *trigger*, unlike ``clinical_suggestions``. The trigger tables hold clinical
assertions a clinician attested to; this holds a provenance record of an action, and the
medication events and appointment it points at carry their own protections. Stated rather than
left as an omission, because "why does this append-only table not have the trigger the other
append-only tables have" is otherwise a question the next reader has to re-derive.

``ordered_investigations`` is a list of *orders*, and it lives here rather than in
``lab_results`` deliberately: that table holds observations, and inserting a row with no value
would put an investigation on the chart that reads as a test that came back empty. This product
has no order-entry integration to send a request to; what it can honestly record is that a
clinician decided to order these.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-15
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_guards import index_exists

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "protocol_applications"
_LIST_INDEX = "ix_protocol_applications_patient_created"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 0001 builds the schema from live ORM metadata, so a database created today already carries
    # this table and a bare create would abort the upgrade. Same guard as 0036/0037/0040.
    if not sa.inspect(bind).has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
            # No index of its own — the composite below leads with this column, so a bare
            # patient_id index would be its prefix and cost a write on every insert for nothing.
            sa.Column("patient_id", sa.Uuid(), sa.ForeignKey("patients.id"), nullable=False),
            sa.Column(
                "encounter_id", sa.Uuid(), sa.ForeignKey("encounters.id"), nullable=True
            ),
            sa.Column("template_key", sa.String(length=60), nullable=False),
            sa.Column("template_version", sa.String(length=20), nullable=False),
            sa.Column("template_title", sa.String(length=200), nullable=False),
            sa.Column(
                "selected_keys",
                sa.dialects.postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "ordered_investigations",
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
            sa.Column(
                "follow_up_appointment_id",
                sa.Uuid(),
                sa.ForeignKey("appointments.id"),
                nullable=True,
            ),
            sa.Column("applied_by", sa.String(length=200), nullable=True),
            sa.Column("warning_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )

    if not index_exists(bind, _TABLE, _LIST_INDEX):
        op.execute(f"CREATE INDEX {_LIST_INDEX} ON {_TABLE} (patient_id, created_at DESC)")


def downgrade() -> None:
    """Drops the table, and with it the record of which protocols were applied.

    The medications it charted and the appointments it booked survive — they are rows of their
    own on the chart. What is lost is the statement that they were one decision, which is
    exactly the thing this table exists to hold, so an operator rolling back across this on a
    database in clinical use should dump it first.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {_LIST_INDEX}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
