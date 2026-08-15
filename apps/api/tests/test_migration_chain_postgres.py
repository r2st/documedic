"""`alembic upgrade head` against an empty PostgreSQL — the check nothing else performs.

Every other migration test in this suite is static (``tests/test_migrations.py``) or runs on
SQLite, where the ``if bind.dialect.name != "postgresql": return`` guard at the top of nearly
every revision means the DDL is never executed at all. So the suite could be entirely green
while the one command a deployment runs before serving traffic did not work.

It did not work. Against an empty database the chain aborted at 0010 with
``DuplicateColumnError: column "dedup_key" of relation "lab_results" already exists``, because
revision 0001 builds the schema from the live ORM models and therefore hands 0010 a table that
already has the column it was written to add. The deployment was left stamped 0009 with the
newer revisions unapplied.

Runs only when a PostgreSQL is reachable::

    docker compose up -d postgres
    pytest tests/test_migration_chain_postgres.py

Point it elsewhere with ``TEST_POSTGRES_URL``. It works in a database of its own, created and
dropped by the fixture, so it can never touch an existing one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres

DEFAULT_URL = "postgresql+asyncpg://aether:aether@localhost:55432/aether_clinician"
SCRATCH_DATABASE = "documedic_migration_chain_test"
REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "data" / "migrations" / "alembic.ini"
API_ROOT = REPO_ROOT / "apps" / "api"


def _swap_database(url: str, database: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{database}"


async def _admin_execute(url: str, statement: str) -> None:
    """Run one statement outside a transaction (CREATE/DROP DATABASE cannot be in one)."""
    engine = create_async_engine(_swap_database(url, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(statement))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def empty_database() -> str:
    """A freshly created, completely empty database — or a skip when none can be made.

    A database rather than a schema, because migrations issue unqualified DDL and the point of
    this test is to run them exactly as a deployment does.
    """
    url = os.environ.get("TEST_POSTGRES_URL", DEFAULT_URL)
    try:
        await _admin_execute(url, f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}"')
        await _admin_execute(url, f'CREATE DATABASE "{SCRATCH_DATABASE}"')
    except Exception as exc:  # noqa: BLE001 — unreachable, or no privilege to create one
        pytest.skip(f"no PostgreSQL to build a scratch database on ({type(exc).__name__}): {exc}")

    try:
        yield _swap_database(url, SCRATCH_DATABASE)
    finally:
        await _admin_execute(url, f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}"')


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    """Run the alembic CLI the way a deployment does: its own process, its own event loop.

    ``data/migrations/env.py`` ends in ``asyncio.run(...)`` at import time, so it cannot be
    driven from inside an async test — and a subprocess is the more faithful test anyway,
    since it is literally the command that runs before the API starts serving.
    """
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "DATABASE_URL": database_url,
            "APP_ENV": "development",
            "PYTHONPATH": str(API_ROOT),
        },
        capture_output=True,
        text=True,
    )


async def _columns(database_url: str, table: str) -> set[str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": table},
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_whole_chain_applies_to_an_empty_database(empty_database):
    """The regression. A deployment's first command must reach head, not stop partway."""
    result = _alembic(empty_database, "upgrade", "head")
    assert result.returncode == 0, (
        "`alembic upgrade head` failed on an empty database — a fresh deployment cannot bring "
        f"its schema up.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    current = _alembic(empty_database, "current")
    assert "(head)" in current.stdout + current.stderr, (
        f"the chain did not reach head:\n{current.stdout}\n{current.stderr}"
    )


@pytest.mark.asyncio
async def test_running_the_upgrade_twice_changes_nothing(empty_database):
    """Deploy scripts re-run migrations on every release; a second run must be a no-op rather
    than an error that fails the deploy."""
    assert _alembic(empty_database, "upgrade", "head").returncode == 0
    second = _alembic(empty_database, "upgrade", "head")
    assert second.returncode == 0, f"a repeated upgrade failed:\n{second.stderr}"


@pytest.mark.asyncio
async def test_the_newest_revision_rolls_back_and_forward(empty_database):
    """Rollback safety for the revision this release adds: a bad deploy has to be undoable."""
    assert _alembic(empty_database, "upgrade", "head").returncode == 0
    assert "family_started_at" in await _columns(empty_database, "sessions")

    down = _alembic(empty_database, "downgrade", "-1")
    assert down.returncode == 0, f"the newest revision does not roll back:\n{down.stderr}"
    assert "family_started_at" not in await _columns(empty_database, "sessions")

    up = _alembic(empty_database, "upgrade", "head")
    assert up.returncode == 0, f"the newest revision does not re-apply:\n{up.stderr}"
    assert "family_started_at" in await _columns(empty_database, "sessions")
