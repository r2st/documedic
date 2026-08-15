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

from tests.postgres_required import unavailable

pytestmark = pytest.mark.postgres

DEFAULT_URL = "postgresql+asyncpg://aether:aether@localhost:55432/aether_clinician"
SCRATCH_DATABASE = "documedic_migration_chain_test"
REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "data" / "migrations" / "alembic.ini"
API_ROOT = REPO_ROOT / "apps" / "api"
VERSIONS_DIR = REPO_ROOT / "data" / "migrations" / "versions"

# The one revision that does not roll back, and says so in its own module. The rollback sweep
# descends to it and stops; see ``test_the_irreversible_revision_refuses_before_it_changes
# _anything`` for what happens if it is asked to go further.
IRREVERSIBLE_FLOOR = "0006"


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
        unavailable(f"no PostgreSQL to build a scratch database on ({type(exc).__name__}): {exc}")

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


def _revisions() -> list[str]:
    """Every revision id, oldest first, read off the filenames rather than the chain.

    ``tests/test_migrations.py::test_revision_chain_is_linear_and_complete`` already proves the
    filename order and the ``down_revision`` links agree, so this can stay a sort.
    """
    return [path.name.split("_", 1)[0] for path in sorted(VERSIONS_DIR.glob("[0-9]*.py"))]


IRREVERSIBLE_FLOOR_INDEX = _revisions().index(IRREVERSIBLE_FLOOR)


def _current_revision(database_url: str) -> str:
    """The revision the database is stamped at, as `alembic current` reports it.

    The command prints its INFO lines to stderr and the revision on its own line, as either
    ``0028`` or ``0028 (head)``, so the first token of a line that names a known revision is
    the answer. An empty string means "stamped at nothing", which is base.
    """
    result = _alembic(database_url, "current")
    known = set(_revisions())
    for line in (result.stdout + result.stderr).splitlines():
        head, _, _ = line.strip().partition(" ")
        if head in known:
            return head
    return ""


async def _schema_snapshot(database_url: str) -> dict[str, object]:
    """Columns, types, nullability and indexes for the whole public schema.

    What a rollback has to restore, in a form two runs can be compared on. Deliberately not the
    ORM's view of the schema: the question is what the migration chain built, which is the thing
    that can differ from what the models say.

    ``alembic_version`` is excluded — it holds the revision pointer, which is exactly what a
    downgrade is supposed to change.
    """
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            columns = await conn.execute(
                text(
                    "SELECT table_name, column_name, data_type, is_nullable, "
                    "       character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name <> 'alembic_version' "
                    "ORDER BY table_name, column_name"
                )
            )
            indexes = await conn.execute(
                text(
                    "SELECT tablename, indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version' "
                    "ORDER BY tablename, indexname"
                )
            )
            return {
                "columns": [tuple(row) for row in columns],
                "indexes": [tuple(row) for row in indexes],
            }
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
    """Rollback safety for the revision this release adds: a bad deploy has to be undoable.

    Deliberately says nothing about *which* revision is newest. This test used to assert on
    ``sessions.family_started_at``, the column revision 0020 adds, and it kept asserting that
    through 0021-0028 — eight revisions whose rollback was therefore never checked, while the
    test itself stayed green by skipping wherever no PostgreSQL was reachable. A test that has
    to be edited to keep testing the right thing is a test that stops testing the right thing.

    The schema snapshot is what makes that unnecessary: down and back up has to land on exactly
    the schema it left, whatever the newest revision happens to do.
    """
    assert _alembic(empty_database, "upgrade", "head").returncode == 0
    at_head = await _schema_snapshot(empty_database)

    down = _alembic(empty_database, "downgrade", "-1")
    assert down.returncode == 0, f"the newest revision does not roll back:\n{down.stderr}"

    up = _alembic(empty_database, "upgrade", "head")
    assert up.returncode == 0, f"the newest revision does not re-apply:\n{up.stderr}"
    assert await _schema_snapshot(empty_database) == at_head, (
        "the newest revision does not restore the schema it rolled back from"
    )


@pytest.mark.asyncio
async def test_every_reversible_revision_rolls_back_and_forward(empty_database):
    """The whole descent, not just one step — and back up again onto the same schema.

    One revision at a time rather than a single `downgrade 0006`, so a failure names the
    revision that broke instead of the range that contains it.

    Stops at 0006, which is irreversible by construction and says so
    (``0006_encrypt_patient_pii.IRREVERSIBLE``): its columns hold ciphertext, and narrowing
    them back to DATE and VARCHAR(20) is a decryption rather than a type change. Everything
    above it is ordinary DDL that has to be undoable, because a bad release is rolled back at
    the point where nobody is in a position to hand-write DDL.
    """
    assert _alembic(empty_database, "upgrade", "head").returncode == 0
    at_head = await _schema_snapshot(empty_database)

    for step in range(len(_revisions()) - IRREVERSIBLE_FLOOR_INDEX - 1):
        current = _current_revision(empty_database)
        down = _alembic(empty_database, "downgrade", "-1")
        assert down.returncode == 0, (
            f"revision {current} does not roll back (step {step}):\n{down.stderr}"
        )

    assert _current_revision(empty_database) == IRREVERSIBLE_FLOOR

    up = _alembic(empty_database, "upgrade", "head")
    assert up.returncode == 0, f"the chain does not re-apply after a rollback:\n{up.stderr}"
    assert await _schema_snapshot(empty_database) == at_head, (
        "coming back up from a rollback does not reproduce the schema at head"
    )


@pytest.mark.asyncio
async def test_the_irreversible_revision_refuses_before_it_changes_anything(empty_database):
    """0006 stops with an explanation and an intact table, not partway through the DDL.

    It could never have rolled back: PostgreSQL refuses TEXT -> DATE without a USING clause
    even on an empty table. What it *did* was narrow ``full_name`` first and then abort on
    ``date_of_birth``, leaving an operator mid-rollback holding a driver message about casting.
    """
    assert _alembic(empty_database, "upgrade", "head").returncode == 0
    for _ in range(len(_revisions()) - IRREVERSIBLE_FLOOR_INDEX - 1):
        assert _alembic(empty_database, "downgrade", "-1").returncode == 0
    before = await _schema_snapshot(empty_database)

    down = _alembic(empty_database, "downgrade", "-1")
    assert down.returncode != 0, "0006 now claims to roll back; check it actually does"
    assert "cannot be reversed automatically" in down.stderr, (
        f"the refusal does not explain itself:\n{down.stderr}"
    )
    assert "app.core.crypto" in down.stderr, "the refusal does not say what to do instead"

    assert _current_revision(empty_database) == IRREVERSIBLE_FLOOR
    assert await _schema_snapshot(empty_database) == before, (
        "the refused downgrade still changed the schema"
    )
