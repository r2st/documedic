"""Migration 0027's three indexes: that they exist, lead correctly, and serve the right rows.

Migration 0008 indexed the patient-scoped hot paths and 0009 pruned what that made redundant.
Three predicates sat outside both sweeps and stayed on a sequential scan of a table nothing
prunes — two account-scoped list reads and the reset-token retention sweep. See the 0027
docstring for why each one matters.

Two halves, as in ``test_index_coverage``:

* **Structural** — the index is declared, leads with the filter column, and carries the sort
  column, so the ORM and the migration cannot drift into a state where a fresh ``create_all``
  deploy and an upgraded database have different schemas.
* **Behavioural** — the read each index serves returns the right rows in the right order and
  stays scoped to its owner. The query *plans* cannot be asserted here (the suite runs on
  SQLite, whose planner is not PostgreSQL's), which is the same limitation 0008 and 0009 record.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.models import Base
from app.models.patient import Patient
from app.models.user import Account, PasswordResetToken
from app.models.validation import SafetyReport, ValidationRun
from app.services.auth_service import AuthService
from app.services.safety_report_service import SafetyReportService
from app.services.validation_service import ValidationService


def _index_columns(table_name: str, index_name: str) -> tuple[str, ...]:
    table = Base.metadata.tables[table_name]
    for index in table.indexes:
        if index.name == index_name:
            return tuple(
                getattr(expr, "name", None) or str(expr).split()[0] for expr in index.expressions
            )
    raise AssertionError(
        f"{table_name} has no index {index_name}; declared: "
        f"{sorted(i.name or '?' for i in table.indexes)}"
    )


# --------------------------------------------------------------------------- structural


@pytest.mark.parametrize(
    ("table_name", "index_name", "expected"),
    [
        ("safety_reports", "ix_safety_reports_account_created", ("account_id", "created_at")),
        ("validation_runs", "ix_validation_runs_account_created", ("account_id", "created_at")),
        (
            "password_reset_tokens",
            "ix_password_reset_tokens_expires_at",
            ("expires_at",),
        ),
    ],
)
def test_index_is_declared_with_the_expected_columns(table_name, index_name, expected):
    """Filter column leads; the sort column follows it where there is one.

    The order is the point rather than the membership: an index on ``(created_at, account_id)``
    would exist, would be found by a naive "is there an index" check, and would do nothing for
    ``WHERE account_id = ? ORDER BY created_at DESC``.
    """
    assert _index_columns(table_name, index_name) == expected


def test_the_two_list_indexes_match_the_migration_definitions():
    """The ORM and migration 0027 must agree, or fresh and upgraded deploys diverge.

    Migration 0001 builds a new database from ``Base.metadata``; 0027 alters an existing one.
    Reading the definitions out of the migration keeps the two descriptions of the same index
    from being edited apart — the failure mode ``test_dropped_indexes_are_absent_from_models``
    guards from the opposite direction.
    """
    import ast
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[3]
        / "data"
        / "migrations"
        / "versions"
        / "0027_account_scoped_list_indexes.py"
    )
    tree = ast.parse(migration.read_text())
    declared: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "_INDEXES" for t in targets):
            continue
        value = getattr(node, "value", None)
        if not isinstance(value, ast.List):
            continue
        for entry in value.elts:
            assert isinstance(entry, ast.Tuple)
            name, table, definition = (element.value for element in entry.elts)
            declared[name] = (table, definition)
    assert declared, f"no _INDEXES list found in {migration.name}"

    for name, (table, definition) in declared.items():
        columns = tuple(
            part.strip().split()[0] for part in definition.strip("()").split(",") if part.strip()
        )
        assert _index_columns(table, name) == columns, (
            f"migration 0027 creates {name} on {table} {definition}, but the ORM declares "
            f"{_index_columns(table, name)} — a fresh create_all deploy would differ from an "
            "upgraded database"
        )


def test_the_new_indexes_introduce_no_redundancy():
    """Mirrors ``test_no_index_is_a_prefix_of_another`` for the three tables 0027 touches.

    Repeated narrowly here so a failure names 0027 rather than the whole schema. An index that
    is a leading-column prefix of another costs a write on every row change and buys nothing.
    """
    for table_name in ("safety_reports", "validation_runs", "password_reset_tokens"):
        table = Base.metadata.tables[table_name]
        indexes = list(table.indexes)
        for short in indexes:
            for long in indexes:
                if short is long or len(long.expressions) <= len(short.expressions):
                    continue
                short_cols = _index_columns(table_name, short.name or "")
                long_cols = _index_columns(table_name, long.name or "")
                assert long_cols[: len(short_cols)] != short_cols, (
                    f"{table_name}.{short.name} is now a prefix of {long.name}"
                )


def test_the_reset_token_sweep_column_is_indexed_like_the_session_one():
    """Both retention sweeps read ``expires_at < cutoff``; both need the index.

    Pinned as a pair rather than as a fact about one table, because the way this went missing
    was ``password_reset_tokens`` being added later with the same shape as ``sessions`` and not
    the same index. Asserting the symmetry is what catches the next table added that way.
    """
    for table_name in ("sessions", "password_reset_tokens"):
        leaders = {
            _index_columns(table_name, index.name or "")[0]
            for index in Base.metadata.tables[table_name].indexes
            if index.expressions
        }
        assert "expires_at" in leaders, (
            f"{table_name}.expires_at leads no index, so its retention sweep — which filters on "
            f"nothing else — scans the table. Present: {sorted(leaders)}"
        )


# --------------------------------------------------------------------------- behavioural


async def _second_account(db) -> Account:
    account = Account(email=f"other-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    return account


async def test_safety_reports_are_account_scoped_and_newest_first(db, auth_client):
    """The read ``ix_safety_reports_account_created`` serves, end to end."""
    account_id = uuid.UUID(
        (await auth_client.get("/api/v1/auth/me")).json()["id"],
    )
    other = await _second_account(db)

    for offset, description in enumerate(("oldest", "middle", "newest")):
        report = SafetyReport(
            account_id=account_id,
            category="near_miss",
            severity="near_miss",
            description=description,
        )
        db.add(report)
        await db.flush()
        report.created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=offset)
    db.add(
        SafetyReport(
            account_id=other.id,
            category="near_miss",
            severity="serious",
            description="not yours",
        )
    )
    await db.commit()

    rows = await SafetyReportService(db).list_reports(account_id)

    assert [r.description for r in rows] == ["newest", "middle", "oldest"]
    assert "not yours" not in [r.description for r in rows], (
        "another account's safety report leaked into the register"
    )


async def test_validation_runs_are_account_scoped_and_newest_first(db, auth_client):
    """The read ``ix_validation_runs_account_created`` serves, end to end."""
    account_id = uuid.UUID((await auth_client.get("/api/v1/auth/me")).json()["id"])
    other = await _second_account(db)

    for offset, note in enumerate(("first", "second", "third")):
        run = ValidationRun(account_id=account_id, vignette_count=1, notes=note)
        db.add(run)
        await db.flush()
        run.created_at = datetime(2026, 2, 1, tzinfo=UTC) + timedelta(days=offset)
    db.add(ValidationRun(account_id=other.id, vignette_count=1, notes="theirs"))
    await db.commit()

    rows = await ValidationService(db).list_runs(account_id)

    assert [r.notes for r in rows] == ["third", "second", "first"]
    assert "theirs" not in [r.notes for r in rows]


async def test_the_reset_token_sweep_removes_only_rows_past_the_retention_window(db):
    """``purge_spent_reset_tokens`` still selects the right rows off the new index.

    The index changes how the rows are found, not which — this is the assertion that keeps the
    two from drifting. A live token and one that expired inside the window both survive; only
    the one whose expiry is older than the retention window goes.
    """
    account = await _second_account(db)
    now = datetime.now(UTC)
    retention = timedelta(days=settings.session_retention_days)

    def _token(label: str, expires_at: datetime) -> PasswordResetToken:
        return PasswordResetToken(
            account_id=account.id,
            token_hash=f"hash-{label}",
            expires_at=expires_at,
        )

    live = _token("live", now + timedelta(hours=1))
    recently_expired = _token("recent", now - timedelta(minutes=5))
    long_expired = _token("ancient", now - retention - timedelta(days=1))
    db.add_all([live, recently_expired, long_expired])
    await db.commit()

    removed = await AuthService(db).purge_spent_reset_tokens(now=now)
    await db.commit()

    assert removed == 1
    from sqlalchemy import select

    surviving = {
        row.token_hash
        for row in (await db.execute(select(PasswordResetToken))).scalars().all()
        if row.account_id == account.id
    }
    assert surviving == {"hash-live", "hash-recent"}


async def test_the_reset_token_sweep_is_scoped_by_expiry_not_by_account(db):
    """One account's expired tokens must not shelter another's — the sweep is table-wide.

    Worth pinning alongside the index because ``account_id`` is the *other* indexed column on
    this table, and "filter by the column that has an index" is exactly the wrong instinct to
    have here.
    """
    first = await _second_account(db)
    second = await _second_account(db)
    now = datetime.now(UTC)
    stale = now - timedelta(days=settings.session_retention_days) - timedelta(days=1)
    db.add_all(
        [
            PasswordResetToken(account_id=first.id, token_hash="a", expires_at=stale),
            PasswordResetToken(account_id=second.id, token_hash="b", expires_at=stale),
        ]
    )
    await db.commit()

    removed = await AuthService(db).purge_spent_reset_tokens(now=now)
    await db.commit()

    assert removed == 2


async def test_a_patient_scoped_safety_report_still_reaches_its_account_register(db, auth_client):
    """The new index leads with ``account_id``; the patient-scoped one must still be filled.

    A report filed against a chart carries both ids, and the register is read by account. This
    is the row that would go missing if the filter and the index were ever reconciled the wrong
    way round.
    """
    from tests.conftest import create_patient

    account_id = uuid.UUID((await auth_client.get("/api/v1/auth/me")).json()["id"])
    patient = await create_patient(auth_client, full_name="Register Patient")

    resp = await auth_client.post(
        "/api/v1/safety-reports",
        json={
            "patient_id": patient["id"],
            "category": "near_miss",
            "severity": "near_miss",
            "description": "A suggestion was shown that the chart did not support.",
        },
    )
    assert resp.status_code in (200, 201), resp.text

    rows = await SafetyReportService(db).list_reports(account_id)
    assert [str(r.patient_id) for r in rows] == [patient["id"]]
    assert isinstance((await db.get(Patient, uuid.UUID(patient["id"]))), Patient), (
        "the chart the report references must still resolve"
    )
