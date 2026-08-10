"""Add sessions.last_used_at for idle-session timeout enforcement.

Session management: a refresh token idle for longer than
``settings.session_idle_timeout_minutes`` is rejected on next use even if its absolute
``jwt_refresh_ttl_days`` expiry hasn't passed. Existing rows backfill from created_at so
already-issued sessions don't appear to have just been used.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 0001 builds the schema with Base.metadata.create_all() off the *live* ORM models, so a
    # database created today already has this column and a bare ADD COLUMN aborts the whole
    # upgrade with DuplicateColumnError. Databases stamped before this revision still need it.
    # (0007 solves the same problem with checkfirst=True, 0008 with IF NOT EXISTS.)
    if _column_exists(bind, "sessions", "last_used_at"):
        return
    op.add_column(
        "sessions",
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute("UPDATE sessions SET last_used_at = created_at")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not _column_exists(bind, "sessions", "last_used_at"):
        return
    op.drop_column("sessions", "last_used_at")


def _column_exists(bind: sa.engine.Connection, table: str, column: str) -> bool:
    return bool(
        bind.exec_driver_sql(
            "SELECT 1 FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND column_name = '{column}'"
        ).scalar()
    )
