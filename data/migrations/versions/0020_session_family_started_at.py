"""Add sessions.family_started_at so a refresh-token family has an absolute lifetime.

``JWT_REFRESH_TTL_DAYS`` reads like the longest a sign-in can last without re-entering a
password. It was not. Rotation issued a brand-new ``sessions`` row with ``expires_at`` computed
from *now*, so a client that refreshed inside the idle timeout pushed the absolute expiry
forward every time and the session never ended: the seven-day ceiling applied to a token, not
to a sign-in. A refresh token lifted off a device therefore kept working indefinitely as long
as the holder kept rotating it — reuse detection only fires if the *legitimate* client presents
the stale token, which a device that has been put away never does.

``family_started_at`` is the moment the family was created by a password sign-in and is carried
unchanged across every rotation, so ``expires_at`` becomes ``family_started_at + TTL`` and the
absolute-expiry check already in ``AuthService.refresh`` enforces the ceiling that was always
advertised.

Backfilled from ``created_at``: for a row written before this column, the row's own creation is
the only start-of-family the database knows, which dates existing sessions no later than the
truth and expires them no later than the old behaviour would have.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.migration_guards import column_exists

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "sessions"
_COLUMN = "family_started_at"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 0001 builds the schema from the live ORM models, so a database created today already has
    # this column and a bare ADD COLUMN would abort the upgrade. Same guard as 0005 and 0019.
    if column_exists(bind, _TABLE, _COLUMN):
        return
    # Added nullable, backfilled, then made NOT NULL — three statements rather than one
    # ADD COLUMN NOT NULL, so the existing rows are dated from their own creation instead of
    # every historic session claiming to have started at deploy time.
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.DateTime(timezone=True), nullable=True))
    op.execute(f"UPDATE {_TABLE} SET {_COLUMN} = created_at WHERE {_COLUMN} IS NULL")
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not column_exists(bind, _TABLE, _COLUMN):
        return
    op.drop_column(_TABLE, _COLUMN)
