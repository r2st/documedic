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
    op.drop_column("sessions", "last_used_at")
