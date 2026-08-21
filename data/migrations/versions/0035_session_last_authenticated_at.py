"""When a person last typed the password, as distinct from when a client last used a token.

The step-up gate (``app.dependencies.require_recent_authentication``) refuses to delete a chart,
import charts in bulk, or export a whole record unless the password was proved recently. Nothing
in this schema recorded that. The three timestamps ``sessions`` already carried each answer a
different question and none of them answer this one:

* ``family_started_at`` — when the sign-in began. Correct at sign-in and never again.
* ``last_used_at`` — when a refresh token was last exchanged. Moves on a timer in any open
  browser tab, so it measures whether the client is alive, not whether a person is at it.
* ``expires_at`` — the absolute ceiling on the sign-in.

Backfilled from ``family_started_at`` rather than from ``now()``, because that is exactly what
the column means for a row that predates it: the sign-in was a password authentication, and the
moment it happened is on the row already. Stamping ``now()`` would have declared every session
in the estate freshly authenticated at deploy time — the one value that is certainly wrong, and
wrong in the permissive direction. Backfilling from ``family_started_at`` means a sign-in older
than the window is asked for a password on its next sensitive action, which is the outcome the
gate exists to produce.

NOT NULL with a ``now()`` default: the column is read on every gated request and a NULL would
have to mean something there. Both available meanings are bad — "never authenticated" locks out
sessions the deployment is fine with, "authenticated forever ago" is a silent hole — so the
column is simply never null and the gate has one branch instead of three.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.db.migration_guards import column_exists

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "sessions"
_COLUMN = "last_authenticated_at"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # A database created today comes out of 0001 already carrying this: 0001 builds the schema
    # from live ORM metadata. See app.db.migration_guards.
    if column_exists(bind, _TABLE, _COLUMN):
        return

    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
    )
    # The backfill, and the reason this is three statements rather than one. Adding the column
    # NOT NULL DEFAULT now() in a single step would have written now() into every existing row,
    # which is the wrong value: those sessions authenticated when they began, not at deploy.
    op.execute(f"UPDATE {_TABLE} SET {_COLUMN} = family_started_at WHERE {_COLUMN} IS NULL")
    op.alter_column(_TABLE, _COLUMN, nullable=False)


def downgrade() -> None:
    """Drops the column. What is lost is every re-authentication stamp; what is not lost is any
    sign-in — the sessions themselves are untouched, and a rollback simply returns the API to
    having no step-up gate to feed."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
