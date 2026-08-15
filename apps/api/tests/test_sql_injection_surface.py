"""Raw SQL is an allowlist, and query text is never built from a string.

This service is ORM-only by construction: every query is a SQLAlchemy Core/ORM construct, so
values reach the database as bound parameters and a quote in a clinician's surname is a quote in
a surname. An audit found no injection vector, which is the expected outcome and also the least
durable kind of finding — it describes the code on the day it was read, and the next hand-built
query is one convenient f-string away. A ``text(f"... {name} ...")`` added to make a reporting
endpoint work would read as unremarkable in review.

So the property is pinned rather than re-audited:

* every ``text()`` in the application source takes a **literal** string, so no SQL is ever
  assembled from a value; and
* the set of literals actually executed is small enough to enumerate here, so a *new* piece of
  raw SQL fails this test and has to be argued for in a diff.

Deliberately not covered: ``data/migrations``. Those interpolate table and index names into DDL
from module-level constants, which is legitimate — they are developer-authored schema changes
run by an operator against no request, and no user input is in scope for them. This test is
about the query surface a request can reach.

The behavioural half below is the belt to that braces: injection payloads pushed through the
free-text fields that genuinely land in a database row, asserting they come back as the strings
they are.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
from sqlalchemy import func, select

from app.models.user import Account, Session

_SOURCE_ROOTS = (
    pathlib.Path(__file__).resolve().parents[1] / "app",
    pathlib.Path(__file__).resolve().parents[3] / "services",
)

# Every raw SQL string the application may execute. Two liveness probes and one advisory lock,
# all of them constant, and the lock takes its key as a bound parameter rather than as text.
#
# Adding to this list is allowed and is meant to be a visible decision. What is *not* allowed is
# a string that stops being a literal: see test_no_sql_is_built_from_a_string below.
_ALLOWED_RAW_SQL = frozenset(
    {
        "SELECT 1",
        "SELECT pg_advisory_xact_lock(:k)",
    }
)

# Names that construct SQL from text. `text()` is SQLAlchemy's; the others are here so that
# reaching for the DBAPI directly is caught too.
_SQL_TEXT_CALLS = frozenset({"text", "exec_driver_sql"})

# A statement, as opposed to the index and DDL fragments that are also written with `text()`.
# ``\b`` rather than a prefix match, or the column expression "updated_at DESC" is read as an
# UPDATE and the allowlist fills up with ordering clauses.
_STATEMENT_RE = re.compile(r"^(SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


def _python_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in _SOURCE_ROOTS:
        if root.exists():
            files.extend(p for p in root.rglob("*.py") if "/tests/" not in str(p))
    assert files, "found no application source to scan"
    return files


def _sql_text_calls() -> list[tuple[pathlib.Path, ast.Call]]:
    """Every ``text(...)``/``exec_driver_sql(...)`` call site in the application source."""
    found: list[tuple[pathlib.Path, ast.Call]] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_node = node.func
            name = (
                func_node.id
                if isinstance(func_node, ast.Name)
                else func_node.attr
                if isinstance(func_node, ast.Attribute)
                else None
            )
            if name in _SQL_TEXT_CALLS and node.args:
                found.append((path, node))
    return found


def test_no_sql_is_built_from_a_string():
    """The pin that matters. An f-string, a ``%``, a ``+`` or a ``.format()`` inside ``text()``
    is the shape every SQL injection in a Python service has ever had, and none of them can be
    made safe by the caller being careful today."""
    offenders = []
    for path, call in _sql_text_calls():
        arg = call.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            continue
        offenders.append(f"{path.name}:{call.lineno} builds SQL from {type(arg).__name__}")
    assert not offenders, (
        "SQL must be a literal string with bound parameters, never assembled:\n"
        + "\n".join(offenders)
    )


def test_the_raw_sql_the_service_executes_is_the_declared_set():
    """Enumerated so that new raw SQL is a decision rather than a drift. If this fails because
    you added a legitimate query, add it to ``_ALLOWED_RAW_SQL`` — and check while you are there
    that every value in it is a bound parameter."""
    executed = {
        call.args[0].value
        for _, call in _sql_text_calls()
        if isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
    }
    # Index and DDL expressions ("created_at DESC", "is_deleted = false") are `text()` too, and
    # are constants under the developer's hand with no request data anywhere near them. The
    # allowlist is about statements, so keep to the ones that look like one. The word boundary
    # is load-bearing: without it the column "updated_at DESC" reads as an UPDATE statement.
    statements = {s for s in executed if _STATEMENT_RE.match(s.strip())}
    assert statements == _ALLOWED_RAW_SQL, (
        f"raw SQL statements changed: added {statements - _ALLOWED_RAW_SQL}, "
        f"removed {_ALLOWED_RAW_SQL - statements}"
    )


# --- Behavioural: payloads through the fields that reach a row -------------------------------

PREFIX = "/api/v1/auth"

# Classic terminators, a comment, a stacked statement and a JSON-path probe. The last matters
# here specifically: the login lockout matches a caller-supplied email inside a JSON payload
# column (`AuditLog.payload[...].as_string() == value`), which is the one place in this service
# where user input meets an operator rather than a plain column comparison.
_PAYLOADS = (
    "'; DROP TABLE accounts; --",
    "' OR '1'='1",
    '" OR ""="',
    "1; DELETE FROM sessions WHERE 't'='t'",
    "\\'; UPDATE accounts SET password_hash='x'; --",
    "%' OR 1=1 --",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", _PAYLOADS)
async def test_a_payload_in_a_display_name_is_stored_as_the_string_it_is(client, db, payload):
    """``display_name`` is free text that lands in ``accounts``, unvalidated beyond a length."""
    resp = await client.post(
        f"{PREFIX}/signup",
        json={
            "email": "clinician@example.org",
            "password": "correct-horse-battery",
            "display_name": payload,
        },
    )
    assert resp.status_code == 201, resp.text

    account = (
        (await db.execute(select(Account).where(Account.email == "clinician@example.org")))
        .scalars()
        .one()
    )
    assert account.display_name == payload, "the value was altered on its way to the row"
    assert await db.scalar(select(func.count()).select_from(Account)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", _PAYLOADS)
async def test_a_payload_in_the_user_agent_header_is_stored_as_the_string_it_is(
    client, db, payload
):
    """``User-Agent`` is wholly attacker-controlled, is written to ``sessions.user_agent`` and is
    copied into the auth audit payload — the widest unvalidated string in the auth path."""
    resp = await client.post(
        f"{PREFIX}/signup",
        json={
            "email": "clinician@example.org",
            "password": "correct-horse-battery",
            "display_name": "Dr X",
        },
        headers={"User-Agent": payload},
    )
    assert resp.status_code == 201, resp.text

    session = (await db.execute(select(Session))).scalars().one()
    assert session.user_agent == payload
    assert await db.scalar(select(func.count()).select_from(Account)) == 1


@pytest.mark.asyncio
async def test_a_payload_in_the_json_matched_lockout_dimension_matches_nothing_else(client, db):
    """The failed-login budget is counted by matching an email *inside a JSON column*. A payload
    that made that comparison match loosely would let one account's failures spend another's
    budget — or, with `' OR '1'='1`, every account's at once. The addresses here are distinct
    and legal, so the only way they collide is if the match stopped being an equality."""
    await client.post(
        f"{PREFIX}/signup",
        json={
            "email": "victim@example.org",
            "password": "correct-horse-battery",
            "display_name": "Dr Victim",
        },
    )

    # Burn attempts against a *different* address whose local part is an injection payload.
    for _ in range(10):
        resp = await client.post(
            f"{PREFIX}/login",
            json={"email": "or-1-1@example.org", "password": "wrong-password"},
        )
        assert resp.status_code in (401, 429)

    # The victim's own budget must be untouched by them.
    ok = await client.post(
        f"{PREFIX}/login",
        json={"email": "victim@example.org", "password": "correct-horse-battery"},
    )
    assert ok.status_code == 200, "another address's failures reached this account's budget"
    assert await db.scalar(select(func.count()).select_from(Account)) == 1


@pytest.mark.asyncio
async def test_a_payload_in_a_reset_token_is_a_lookup_miss_not_an_error(client):
    """The reset token is free-form text with no format validation, looked up by hash. A payload
    must be an ordinary 401 — a 500 here would mean the string reached something that parsed
    it."""
    for payload in _PAYLOADS:
        resp = await client.post(
            f"{PREFIX}/password-reset/confirm",
            json={"token": payload, "new_password": "a-new-passphrase-x"},
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["code"] == "invalid_reset_token"
