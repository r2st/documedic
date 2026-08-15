"""Auth API integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.user import Account, Session


async def _audit_actions(db, email: str) -> list[str]:
    """Every test gets an isolated in-memory DB, so this returns just this account's trail."""
    account = (await db.execute(select(Account).where(Account.email == email.lower()))).scalar_one()
    rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.account_id == account.id)
                .order_by(AuditLog.sequence)
            )
        )
        .scalars()
        .all()
    )
    return [r.action for r in rows]


@pytest.mark.asyncio
async def test_signup_returns_tokens(client):
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "a@b.com", "password": "password123", "display_name": "A"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_duplicate_email_rejected(client):
    await client.post("/api/v1/auth/signup", json={"email": "dup@b.com", "password": "password123"})
    resp = await client.post(
        "/api/v1/auth/signup", json={"email": "dup@b.com", "password": "password123"}
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "email_exists"


@pytest.mark.asyncio
async def test_login_and_me(client):
    await client.post("/api/v1/auth/signup", json={"email": "c@b.com", "password": "password123"})
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "c@b.com", "password": "password123"}
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "c@b.com"


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    await client.post("/api/v1/auth/signup", json={"email": "d@b.com", "password": "password123"})
    resp = await client.post("/api/v1/auth/login", json={"email": "d@b.com", "password": "nope"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_failed_login_is_audited_even_though_the_request_rolls_back(client, db):
    """A failed login raises (and the request session rolls back), but the audit trail must
    still record the attempt -- it's committed independently for exactly this reason."""
    await client.post(
        "/api/v1/auth/signup", json={"email": "audit1@b.com", "password": "password123"}
    )
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "audit1@b.com", "password": "wrong"}
    )
    assert resp.status_code == 401
    actions = await _audit_actions(db, "audit1@b.com")
    assert "auth_login_failed" in actions


@pytest.mark.asyncio
async def test_successful_login_is_audited(client, db):
    await client.post(
        "/api/v1/auth/signup", json={"email": "audit2@b.com", "password": "password123"}
    )
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "audit2@b.com", "password": "password123"}
    )
    assert resp.status_code == 200
    actions = await _audit_actions(db, "audit2@b.com")
    # Two successes: one from the router's internal login-after-signup, one from this call.
    assert actions.count("auth_login_success") == 2
    assert "auth_signup" in actions


@pytest.mark.asyncio
async def test_refresh_rotates_token(client):
    signup = await client.post(
        "/api/v1/auth/signup", json={"email": "e@b.com", "password": "password123"}
    )
    refresh = signup.json()["refresh_token"]
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    # The old refresh token is now revoked (rotation).
    reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert reuse.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_requires_token(client):
    resp = await client.get("/api/v1/patients")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_refresh(client):
    signup = await client.post(
        "/api/v1/auth/signup", json={"email": "f@b.com", "password": "password123"}
    )
    refresh = signup.json()["refresh_token"]
    await client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_is_audited(client, db):
    signup = await client.post(
        "/api/v1/auth/signup", json={"email": "audit3@b.com", "password": "password123"}
    )
    refresh = signup.json()["refresh_token"]
    await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    actions = await _audit_actions(db, "audit3@b.com")
    assert "auth_token_refreshed" in actions


@pytest.mark.asyncio
async def test_logout_is_audited(client, db):
    signup = await client.post(
        "/api/v1/auth/signup", json={"email": "audit4@b.com", "password": "password123"}
    )
    refresh = signup.json()["refresh_token"]
    await client.post("/api/v1/auth/logout", json={"refresh_token": refresh})
    actions = await _audit_actions(db, "audit4@b.com")
    assert "auth_logout" in actions


# --- Session management (list / revoke / revoke-all / idle timeout) ---


@pytest.mark.asyncio
async def test_list_sessions_shows_active_session(auth_client):
    resp = await auth_client.get("/api/v1/auth/sessions")
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) == 1
    assert "last_used_at" in sessions[0]


@pytest.mark.asyncio
async def test_revoke_session_logs_it_out(auth_client):
    """Revoking the session you are holding signs *you* out, immediately.

    This used to assert an empty session list on the next request, which was only readable
    because the next request still authenticated: revocation acted on the refresh token, and
    the access token in the header went on working until it expired. Now the access token is
    bound to the revoked session, so the follow-up is a 401 — the endpoint does what its
    summary says.
    """
    sessions = (await auth_client.get("/api/v1/auth/sessions")).json()
    session_id = sessions[0]["id"]
    resp = await auth_client.delete(f"/api/v1/auth/sessions/{session_id}")
    assert resp.status_code == 200

    after = await auth_client.get("/api/v1/auth/sessions")
    assert after.status_code == 401, after.text
    assert after.json()["code"] == "invalid_token"


@pytest.mark.asyncio
async def test_cannot_revoke_another_accounts_session(client):
    signup1 = await client.post(
        "/api/v1/auth/signup", json={"email": "sess1@b.com", "password": "password123"}
    )
    client.headers["Authorization"] = f"Bearer {signup1.json()['access_token']}"
    victim_session_id = (await client.get("/api/v1/auth/sessions")).json()[0]["id"]

    signup2 = await client.post(
        "/api/v1/auth/signup", json={"email": "sess2@b.com", "password": "password123"}
    )
    client.headers["Authorization"] = f"Bearer {signup2.json()['access_token']}"
    resp = await client.delete(f"/api/v1/auth/sessions/{victim_session_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_logout_all_revokes_every_session_except_current(client):
    signup = await client.post(
        "/api/v1/auth/signup", json={"email": "multi@b.com", "password": "password123"}
    )
    tokens1 = signup.json()
    client.headers["Authorization"] = f"Bearer {tokens1['access_token']}"
    login2 = await client.post(
        "/api/v1/auth/login", json={"email": "multi@b.com", "password": "password123"}
    )
    tokens2 = login2.json()

    assert len((await client.get("/api/v1/auth/sessions")).json()) == 2

    resp = await client.post(
        "/api/v1/auth/logout-all",
        json={"keep_current_refresh_token": tokens1["refresh_token"]},
    )
    assert resp.status_code == 200
    assert "1 session" in resp.json()["message"]

    remaining = (await client.get("/api/v1/auth/sessions")).json()
    assert len(remaining) == 1
    # The kept token still refreshes; the other one is dead.
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens1["refresh_token"]})
    ).status_code == 200
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens2["refresh_token"]})
    ).status_code == 401


@pytest.mark.asyncio
async def test_idle_refresh_token_is_rejected(client, db):
    """A refresh token unused for longer than the idle-timeout window is rejected even though
    its absolute expiry is far in the future."""
    signup = await client.post(
        "/api/v1/auth/signup", json={"email": "idle@b.com", "password": "password123"}
    )
    refresh_token = signup.json()["refresh_token"]

    account = (await db.execute(select(Account).where(Account.email == "idle@b.com"))).scalar_one()
    session = (
        await db.execute(select(Session).where(Session.account_id == account.id))
    ).scalar_one()
    stale_cutoff = datetime.now(UTC) - timedelta(minutes=settings.session_idle_timeout_minutes + 1)
    session.last_used_at = stale_cutoff
    await db.commit()

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_token"


@pytest.mark.asyncio
async def test_recently_used_refresh_token_is_not_idle_expired(client, db):
    signup = await client.post(
        "/api/v1/auth/signup", json={"email": "notidle@b.com", "password": "password123"}
    )
    refresh_token = signup.json()["refresh_token"]

    account = (
        await db.execute(select(Account).where(Account.email == "notidle@b.com"))
    ).scalar_one()
    session = (
        await db.execute(select(Session).where(Session.account_id == account.id))
    ).scalar_one()
    session.last_used_at = datetime.now(UTC) - timedelta(
        minutes=settings.session_idle_timeout_minutes - 1
    )
    await db.commit()

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
