"""Add password_reset_tokens, and index sessions.expires_at for the retention sweep.

There was no way for a clinician to change or recover a password: ``accounts.password_hash``
was written once at signup and by nothing else. A forgotten password meant a new account (and a
new audit identity), and a *compromised* one meant the credential stayed valid — revoking every
session, which is all the API could do, leaves the attacker able to sign straight back in.

The table stores SHA-256 hashes, never raw tokens, for the same reason ``sessions`` does: a
reader of this table must not come away able to take an account over. ``used_at`` and
``invalidated_at`` are kept apart so a replayed token (a signal) and one superseded by a later
request (routine) read differently in the trail.

The ``sessions.expires_at`` index is for ``AuthService.purge_expired_sessions``. Without it the
retention sweep — which runs opportunistically on the sign-in path — scans the whole table, and
the whole table is precisely what has grown unboundedly: one row per sign-in *and per refresh*,
never removed.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.db.migration_guards import index_exists

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "password_reset_tokens"
_SESSION_INDEX = "ix_sessions_expires_at"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 0001 builds the schema from the live ORM models, so a database created today already
    # carries this table and a bare create would abort the upgrade. Same guard as 0005/0019/0020,
    # applied to a table rather than a column.
    if not sa.inspect(bind).has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "account_id",
                sa.Uuid(),
                sa.ForeignKey("accounts.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("token_hash", sa.String(length=255), nullable=False, index=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("requested_ip", sa.dialects.postgresql.INET(), nullable=True),
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
        )
    if not index_exists(bind, "sessions", _SESSION_INDEX):
        op.create_index(_SESSION_INDEX, "sessions", ["expires_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if index_exists(bind, "sessions", _SESSION_INDEX):
        op.drop_index(_SESSION_INDEX, table_name="sessions")
    if sa.inspect(bind).has_table(_TABLE):
        op.drop_table(_TABLE)
