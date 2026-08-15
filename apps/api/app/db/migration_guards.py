"""Schema-introspection guards for Alembic migrations.

Revision 0001 does not spell its tables out — it builds the whole schema from the live ORM
models. That is convenient and it has one consequence every later revision has to cope with:
a database created *today* comes out of 0001 already carrying every column the models declare,
including the ones revisions 0002..N were written to add. A bare ``ADD COLUMN`` in one of those
revisions therefore aborts on a fresh database with ``DuplicateColumnError``, halfway up the
chain, having already stamped the revisions before it.

That is not a hypothetical: ``alembic upgrade head`` against an empty PostgreSQL failed at 0010
and left the deployment on 0009. Nothing caught it because the test suite runs on SQLite, where
every PostgreSQL-only migration returns before doing anything, and the migration tests are
deliberately static.

So a migration that adds a column asks the database whether it is already there. Portable
across both dialects on purpose: some revisions run on SQLite too, and ``information_schema``
does not exist there.

Held to the code by ``tests/test_migration_chain_postgres.py``, which runs the chain against a
scratch PostgreSQL and is skipped when none is reachable.
"""

from __future__ import annotations

import sqlalchemy as sa


def column_exists(bind: sa.engine.Connection, table: str, column: str) -> bool:
    """True when ``table.column`` is already present in the database ``bind`` is connected to.

    A missing *table* reads as a missing column rather than raising: the caller is about to
    add a column to it and would fail on its own terms, which is the more legible error.
    """
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return False
    return any(col["name"] == column for col in inspector.get_columns(table))


def index_exists(bind: sa.engine.Connection, table: str, index: str) -> bool:
    """True when ``index`` already exists on ``table``."""
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return False
    return any(existing["name"] == index for existing in inspector.get_indexes(table))
