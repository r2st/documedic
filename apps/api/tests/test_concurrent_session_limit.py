"""One account may not accumulate unbounded live sign-ins.

``sessions`` had no ceiling. A row was created per sign-in and never ended except by its own
absolute expiry, an explicit sign-out, or a clinician noticing it in `GET /auth/sessions` — so
an account used across a ward's shared terminals collected live refresh tokens indefinitely,
one per machine nobody remembers signing out of. ``jwt_refresh_ttl_days`` was the only thing
collecting them, and it never fires for a clinician who signs in daily.

The design decision worth pinning is *evict, don't refuse*. Rejecting the sign-in that would
exceed the cap is the obvious reading and the wrong one: it hands anyone who learns a password
a way to lock the clinician out of their own account by filling the cap, and it fails in the
direction that keeps someone out of a patient record mid-consultation. So the person with the
correct password always gets in, and the least recently *used* session gives way — which is the
forgotten terminal the control exists to close, while the phone refreshed an hour ago is not.

Rotation is not a sign-in and must not evict, or a clinician sitting at one workstation would
walk the cap down over an afternoon of ordinary token refreshes.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.user import Account, Session

PREFIX = "/api/v1/auth"

EMAIL = "clinician@example.org"
PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _small_cap(monkeypatch):
    """Three, so a test can show the fourth sign-in evicting without twelve round trips."""
    monkeypatch.setattr(settings, "session_max_concurrent", 3)


async def _signup(client, email: str = EMAIL) -> dict:
    resp = await client.post(
        f"{PREFIX}/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Dr X"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login(client, email: str = EMAIL) -> dict:
    resp = await client.post(f"{PREFIX}/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _live_sessions(db, email: str = EMAIL) -> list[Session]:
    account = (await db.execute(select(Account).where(Account.email == email))).scalars().one()
    rows = (
        (
            await db.execute(
                select(Session).where(
                    Session.account_id == account.id, Session.is_revoked.is_(False)
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


@pytest.mark.asyncio
async def test_signing_in_past_the_ceiling_does_not_grow_the_live_set(client, db):
    """The cap itself. Six sign-ins, three live."""
    await _signup(client)  # signup signs in, so this is the first
    for _ in range(5):
        await _login(client)

    assert len(await _live_sessions(db)) == settings.session_max_concurrent


@pytest.mark.asyncio
async def test_the_newest_sign_in_is_never_the_one_evicted(client):
    """Evict, don't refuse: the clinician at the keyboard with the correct password gets in,
    and the token they were just handed is the one that works."""
    await _signup(client)
    await _login(client)
    await _login(client)
    newest = await _login(client)

    me = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {newest['access_token']}"}
    )
    assert me.status_code == 200, "the sign-in that tripped the cap was the one refused"

    rotated = await client.post(
        f"{PREFIX}/refresh", json={"refresh_token": newest["refresh_token"]}
    )
    assert rotated.status_code == 200, rotated.text


@pytest.mark.asyncio
async def test_the_least_recently_used_session_is_the_one_that_gives_way(client):
    """The oldest sign-in loses, and it loses *both* its credentials — the refresh token it
    holds and the access token already in its hands, because eviction revokes the session row
    that `assert_auth_session_live` reads."""
    oldest = await _signup(client)
    await _login(client)
    await _login(client)
    await _login(client)  # trips the cap

    refreshed = await client.post(
        f"{PREFIX}/refresh", json={"refresh_token": oldest["refresh_token"]}
    )
    assert refreshed.status_code == 401, "the evicted session could still rotate"

    me = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {oldest['access_token']}"}
    )
    assert me.status_code == 401, "the evicted device kept reading with its access token"


@pytest.mark.asyncio
async def test_a_session_refreshed_recently_outlives_one_created_later(client):
    """Least recently *used*, not oldest created. A workstation signed in months ago but
    refreshed this morning is a device in use; a tab opened at lunch and untouched is not."""
    early = await _signup(client)
    idle = await _login(client)

    # `early` is the oldest by creation. Rotating it makes it the most recently used, and
    # rotation replaces its row — so it is `idle` that must lose when the cap is tripped.
    rotated = await client.post(f"{PREFIX}/refresh", json={"refresh_token": early["refresh_token"]})
    assert rotated.status_code == 200, rotated.text

    await _login(client)
    await _login(client)  # trips the cap

    still_good = await client.post(
        f"{PREFIX}/refresh", json={"refresh_token": rotated.json()["refresh_token"]}
    )
    assert still_good.status_code == 200, "the recently used session was evicted"

    evicted = await client.post(f"{PREFIX}/refresh", json={"refresh_token": idle["refresh_token"]})
    assert evicted.status_code == 401, "the idle session survived the cap"


@pytest.mark.asyncio
async def test_rotation_does_not_evict(client, db):
    """A clinician sitting at one workstation refreshes every half hour all afternoon. If
    rotation counted as a sign-in, that alone would walk the cap down and sign out their
    colleagues' terminals — and eventually thrash a single user against their own ceiling."""
    await _signup(client)
    second = await _login(client)
    third = await _login(client)

    token = third["refresh_token"]
    for _ in range(6):
        resp = await client.post(f"{PREFIX}/refresh", json={"refresh_token": token})
        assert resp.status_code == 200, resp.text
        token = resp.json()["refresh_token"]

    assert len(await _live_sessions(db)) == 3, "rotation moved the live-session count"

    survived = await client.post(
        f"{PREFIX}/refresh", json={"refresh_token": second["refresh_token"]}
    )
    assert survived.status_code == 200, "another device was evicted by someone else's refreshes"


@pytest.mark.asyncio
async def test_the_eviction_is_audited_as_something_nobody_asked_for(client, db):
    """Its own action, not `auth_logout_all`. Months later the trail has to distinguish a
    sign-out the clinician performed from one the ceiling performed on their behalf."""
    await _signup(client)
    for _ in range(3):
        await _login(client)

    rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action == "auth_session_evicted")
                .order_by(AuditLog.sequence)
            )
        )
        .scalars()
        .all()
    )
    assert rows, "the ceiling ended a sign-in and left no record"
    assert rows[-1].payload["limit"] == 3
    assert rows[-1].payload["evicted_count"] >= 1


@pytest.mark.asyncio
async def test_one_account_s_ceiling_does_not_reach_another_s_sessions(client, db):
    """Scoped by account — otherwise a busy clinician signing in would sign out the ward."""
    colleague = await _signup(client, "colleague@example.org")
    await _signup(client)
    for _ in range(5):
        await _login(client)

    assert len(await _live_sessions(db, "colleague@example.org")) == 1
    me = await client.get(
        f"{PREFIX}/me", headers={"Authorization": f"Bearer {colleague['access_token']}"}
    )
    assert me.status_code == 200


@pytest.mark.asyncio
async def test_a_zero_cap_disables_the_ceiling(client, db, monkeypatch):
    """The escape hatch, for a deployment whose access pattern this control does not suit."""
    monkeypatch.setattr(settings, "session_max_concurrent", 0)
    await _signup(client)
    for _ in range(4):
        await _login(client)

    assert len(await _live_sessions(db)) == 5
