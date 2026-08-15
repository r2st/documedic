"""Add reasoning_sessions.run_claimed_at for the single-run claim.

Two requests could start the eight-agent pipeline on one session at the same moment — a
double-clicked "Run", or the Reasoning Theatre's ``EventSource`` reconnecting while the first
run was still going. Nothing stopped the second: both ran the panel, both wrote a full set of
ClinicalSuggestion rows against the same session_id, and those rows are immutable by database
trigger, so the duplicates cannot be cleaned up afterwards. The session header (status,
autonomy_tier, case_state) is last-write-wins, so a session could end up reading ``suggestive``
while carrying a hard-blocked suggestion the other run produced.

``ReasoningService.run`` now compare-and-swaps on this column to claim the session, and refuses
with 409 ``reasoning_in_progress`` when the swap loses. A claim older than
``settings.reasoning_run_lease_minutes`` is treated as abandoned and can be taken over, so a run
killed mid-flight does not make the case permanently unrunnable.

Nullable with no backfill: NULL means "never claimed", which is the correct reading of every
session that predates this column, including one left in ``reasoning`` by a crash — it is
takeable, which is the forgiving direction.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_guards import column_exists

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 0001 builds the schema from the live ORM models, so a database created today already has
    # this column and a bare ADD COLUMN would abort the upgrade. Same guard as 0005.
    if column_exists(bind, "reasoning_sessions", "run_claimed_at"):
        return
    op.add_column(
        "reasoning_sessions",
        sa.Column("run_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not column_exists(bind, "reasoning_sessions", "run_claimed_at"):
        return
    op.drop_column("reasoning_sessions", "run_claimed_at")
