"""Auth abuse controls: login lockout, refresh-token reuse detection, enumeration timing.

These are the controls that sit between an attacker and patient data, so each one is tested
for its security property (does it actually stop the attack) rather than just its happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.exceptions import InvalidCredentialsError
from app.models.audit_log import AuditLog
from app.models.user import Account, Session


@pytest.fixture(autouse=True)
def small_lockout(monkeypatch):
    """Shrink the attempt budget so tests don't have to make eight failing requests."""
    monkeypatch.setattr(settings, "login_max_failed_attempts", 3)
    monkeypatch.setattr(settings, "login_attempt_window_minutes", 15)
    monkeypatch.setattr(settings, "login_lockout_minutes", 15)


async def _signup(client, email="lock@example.com", password="password123"):
    resp = await client.post("/api/v1/auth/signup", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _fail_login(client, email="lock@example.com", password="wrong-password"):
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


async def _actions(db) -> list[str]:
    rows = (await db.execute(select(AuditLog).order_by(AuditLog.sequence))).scalars().all()
    return [r.action for r in rows]


# --- Login lockout ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_locks_out_after_budget_exhausted(client):
    await _signup(client)
    for _ in range(3):
        assert (await _fail_login(client)).status_code == 401

    resp = await _fail_login(client)
    assert resp.status_code == 429
    assert resp.json()["code"] == "too_many_attempts"


@pytest.mark.asyncio
async def test_lockout_blocks_the_correct_password_too(client):
    """The whole point: once locked out, knowing the password must not help."""
    await _signup(client)
    for _ in range(3):
        await _fail_login(client)

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "lock@example.com", "password": "password123"}
    )
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_lockout_is_audited(client, db):
    await _signup(client)
    for _ in range(4):
        await _fail_login(client)
    assert "auth_login_locked_out" in await _actions(db)


@pytest.mark.asyncio
async def test_lockout_does_not_leak_across_accounts(client):
    """Failures against one email must not lock a different account on the same IP.

    The IP arm of the check exists for distributed guessing, but the test client always
    reports the same host, so this pins the intended precedence: an untouched account with
    the right password still gets in.
    """
    await _signup(client, email="victim@example.com")
    await _signup(client, email="other@example.com")
    for _ in range(2):
        await _fail_login(client, email="victim@example.com")

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "other@example.com", "password": "password123"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_successful_login_resets_the_budget(client):
    await _signup(client)
    for _ in range(2):
        await _fail_login(client)

    ok = await client.post(
        "/api/v1/auth/login", json={"email": "lock@example.com", "password": "password123"}
    )
    assert ok.status_code == 200

    # Budget cleared: two more failures would have tripped the old count of 2+2 >= 3.
    for _ in range(2):
        assert (await _fail_login(client)).status_code == 401


@pytest.mark.asyncio
async def test_failures_outside_the_window_do_not_count(client, db):
    await _signup(client)
    for _ in range(3):
        await _fail_login(client)

    # Age every failure past the attempt window.
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "auth_login_failed")))
        .scalars()
        .all()
    )
    stale = datetime.now(UTC) - timedelta(minutes=settings.login_attempt_window_minutes + 5)
    for row in rows:
        row.created_at = stale
    await db.commit()

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "lock@example.com", "password": "password123"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_lockout_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failed_attempts", 0)
    await _signup(client)
    for _ in range(6):
        assert (await _fail_login(client)).status_code == 401


@pytest.mark.asyncio
async def test_unknown_email_still_returns_invalid_credentials(client):
    """Enumeration defence: an unknown email gets the same status/code as a wrong password."""
    await _signup(client)
    unknown = await _fail_login(client, email="nobody@example.com", password="whatever123")
    known = await _fail_login(client)
    assert unknown.status_code == known.status_code == 401
    assert unknown.json()["code"] == known.json()["code"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_unknown_email_performs_password_verification(monkeypatch, db):
    """The dummy-hash path must actually call bcrypt, or the timing oracle comes back."""
    from app.services import auth_service as auth_module

    calls: list[str] = []
    real_verify = auth_module.verify_password

    def spy(plain, hashed):
        calls.append(hashed)
        return real_verify(plain, hashed)

    monkeypatch.setattr(auth_module, "verify_password", spy)

    with pytest.raises(InvalidCredentialsError):
        await auth_module.AuthService(db).login("ghost@example.com", "password123")

    assert calls == [auth_module._DUMMY_PASSWORD_HASH]


# --- Refresh-token reuse detection -----------------------------------------------------


@pytest.mark.asyncio
async def test_rotated_refresh_token_cannot_be_replayed(client):
    tokens = await _signup(client, email="rotate@example.com")
    first = tokens["refresh_token"]

    rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": first})
    assert rotated.status_code == 200

    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": first})
    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_token"


@pytest.mark.asyncio
async def test_reuse_revokes_the_whole_session_family(client, db):
    """A replayed token means it was captured — the attacker's *and* the user's live
    sessions must both die, including the one minted by the legitimate rotation."""
    tokens = await _signup(client, email="theft@example.com")
    stolen = tokens["refresh_token"]

    rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": stolen})
    live_token = rotated.json()["refresh_token"]

    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": stolen})
    assert replay.status_code == 401

    # The legitimate token issued by the rotation is now dead too.
    after = await client.post("/api/v1/auth/refresh", json={"refresh_token": live_token})
    assert after.status_code == 401

    account = (
        await db.execute(select(Account).where(Account.email == "theft@example.com"))
    ).scalar_one()
    sessions = (
        (await db.execute(select(Session).where(Session.account_id == account.id))).scalars().all()
    )
    assert sessions and all(s.is_revoked for s in sessions)


@pytest.mark.asyncio
async def test_reuse_detection_is_audited(client, db):
    tokens = await _signup(client, email="audit-reuse@example.com")
    stolen = tokens["refresh_token"]
    await client.post("/api/v1/auth/refresh", json={"refresh_token": stolen})
    await client.post("/api/v1/auth/refresh", json={"refresh_token": stolen})

    actions = await _actions(db)
    assert "auth_refresh_token_reuse_detected" in actions


@pytest.mark.asyncio
async def test_logged_out_token_replay_triggers_reuse_detection(client, db):
    """Logout revokes the token, so presenting it afterwards is indistinguishable from
    theft and gets the same treatment."""
    tokens = await _signup(client, email="logout-replay@example.com")
    refresh = tokens["refresh_token"]

    logout = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    assert logout.status_code == 200

    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert replay.status_code == 401
    assert "auth_refresh_token_reuse_detected" in await _actions(db)


@pytest.mark.asyncio
async def test_unknown_refresh_token_does_not_trigger_reuse_machinery(client, db):
    """A garbage token is just invalid — it must not mass-revoke anything."""
    await _signup(client, email="garbage@example.com")
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert resp.status_code == 401
    assert "auth_refresh_token_reuse_detected" not in await _actions(db)
