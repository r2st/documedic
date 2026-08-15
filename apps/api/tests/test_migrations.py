"""Static checks on the Alembic migration scripts.

These run without a database. They exist because migration bugs only surface on the production
driver (asyncpg on PostgreSQL) while the test suite runs on SQLite — the `if not is_pg: return`
guard in every migration means the PostgreSQL-only DDL is never executed here at all. A real
deploy hit exactly that gap: `op.execute()` given two SQL commands in one string raised
``asyncpg.exceptions.PostgresSyntaxError: cannot insert multiple commands into a prepared
statement`` and aborted the upgrade, because SQLAlchemy's asyncpg dialect issues DDL through a
prepared statement (which is single-command by protocol).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "data" / "migrations" / "versions"

# PostgreSQL dollar-quoted string bodies (function bodies) legitimately contain `;` characters
# without being multiple top-level commands.
_DOLLAR_QUOTED = re.compile(r"\$\$.*?\$\$", re.S)


def _migration_files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.glob("[0-9]*.py"))


def _static_sql_value(node: ast.AST) -> str | None:
    """Best-effort literal SQL text for an op.execute() argument.

    Returns None when the argument is not statically knowable (a bare name, a .format() call,
    etc.) — those are checked via the module-level constants they reference instead.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string: keep only the literal segments
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def _execute_sql_literals(tree: ast.AST) -> list[str]:
    """Every statically-known SQL string passed to op.execute(), plus module-level SQL constants.

    Module-level string constants are included because migrations hold their CREATE FUNCTION
    bodies in constants (`_IMMUTABLE_FN`) and pass them by name or via .format().
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            sql = _static_sql_value(node.args[0])
            if sql is not None:
                found.append(sql)
        elif isinstance(node, ast.Assign):
            value = _static_sql_value(node.value)
            if value is not None and re.search(
                r"\b(CREATE|DROP|ALTER|UPDATE|INSERT|DELETE)\b", value, re.I
            ):
                found.append(value)
    return found


def _command_count(sql: str) -> int:
    """Number of top-level SQL commands in `sql`, ignoring dollar-quoted function bodies."""
    stripped = _DOLLAR_QUOTED.sub("", sql)
    return len([part for part in stripped.split(";") if part.strip()])


def test_migration_files_are_discovered():
    """Guard the guard: a bad path would make every check below vacuously pass."""
    files = _migration_files()
    assert len(files) >= 7, f"expected the migration chain in {MIGRATIONS_DIR}, found {files}"


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_no_multi_command_execute(path: Path):
    """Each op.execute() must carry exactly one SQL command (asyncpg prepared-statement limit)."""
    tree = ast.parse(path.read_text())
    offenders = [sql for sql in _execute_sql_literals(tree) if _command_count(sql) > 1]
    assert not offenders, (
        f"{path.name} passes multiple SQL commands to a single op.execute(); asyncpg rejects "
        "this with 'cannot insert multiple commands into a prepared statement'. Split each "
        f"command into its own op.execute() call. Offending SQL: {offenders}"
    )


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_revision_chain_is_declared(path: Path):
    """Every migration declares a revision id and a down_revision (0001 revises None)."""
    tree = ast.parse(path.read_text())
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    assert "revision" in assigned, f"{path.name} declares no revision id"
    assert "down_revision" in assigned, f"{path.name} declares no down_revision"


def test_revision_chain_is_linear_and_complete():
    """The chain links every migration exactly once with no forks or gaps."""
    revisions: dict[str, str | None] = {}
    for path in _migration_files():
        tree = ast.parse(path.read_text())
        values: dict[str, str | None] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets: Sequence[ast.expr] = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                continue
            if not isinstance(value, ast.Constant):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    values[target.id] = value.value

        rev = values.get("revision")
        assert isinstance(rev, str), f"{path.name} has a missing or non-literal revision id"
        revisions[rev] = values.get("down_revision")

    roots = [rev for rev, down in revisions.items() if down is None]
    assert len(roots) == 1, f"expected exactly one root migration, got {roots}"

    parents = [down for down in revisions.values() if down is not None]
    assert len(parents) == len(set(parents)), f"a revision is revised twice (fork): {parents}"

    # Walk the whole chain from the root; every revision must be reachable.
    children = {down: rev for rev, down in revisions.items() if down is not None}
    seen, cursor = {roots[0]}, roots[0]
    while cursor in children:
        cursor = children[cursor]
        assert cursor not in seen, f"cycle in the migration chain at {cursor}"
        seen.add(cursor)
    assert seen == set(revisions), f"migrations unreachable from the root: {set(revisions) - seen}"


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_no_mutation_of_immutable_clinical_tables(path: Path):
    """CLAUDE.md rule #7: no UPDATE/DELETE against append-only clinical tables.

    Migrations may create, index, or widen these tables; they must never rewrite or remove
    logged clinical rows.
    """
    immutable = ("clinical_suggestions", "clinician_decisions", "drug_safety_overrides")
    tree = ast.parse(path.read_text())
    for sql in _execute_sql_literals(tree):
        body = _DOLLAR_QUOTED.sub("", sql)  # RAISE EXCEPTION text mentions these words
        for table in immutable:
            forbidden = re.search(rf"\b(DELETE\s+FROM|UPDATE)\s+{table}\b", body, re.I)
            assert not forbidden, (
                f"{path.name} mutates the append-only table {table}: {forbidden.group(0)!r}"
            )


def _called_functions(tree: ast.AST) -> set[str]:
    """Every function name called anywhere in the module, bare or attribute-qualified."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_a_migration_that_adds_a_column_checks_whether_it_is_already_there(path: Path):
    """A bare ADD COLUMN aborts `alembic upgrade head` on a *fresh* database.

    0001 does not spell its tables out — it builds the schema from the live ORM models — so a
    database created today already carries every column the later revisions were written to
    add. Three revisions added one without asking, and the consequence was not subtle: against
    an empty PostgreSQL the upgrade died at 0010 with `DuplicateColumnError` and left the
    deployment stamped 0009. Nothing caught it, because the suite runs on SQLite where the
    PostgreSQL-only revisions return before doing anything and these migration checks are
    deliberately static.

    So this is the static check that stands in for the database: a module that calls
    `op.add_column` must also call `column_exists`. See app.db.migration_guards.
    """
    called = _called_functions(ast.parse(path.read_text()))
    if "add_column" not in called:
        return
    assert "column_exists" in called, (
        f"{path.name} adds a column without checking whether the database already has it. "
        "A database created by 0001 today already carries it (0001 builds from the ORM "
        "models), so `alembic upgrade head` aborts here on a fresh deployment. Guard the "
        "add with app.db.migration_guards.column_exists."
    )
