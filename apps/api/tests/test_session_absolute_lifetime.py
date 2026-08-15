"""A sign-in ends. Rotating a refresh token must not push its own expiry forward.

``JWT_REFRESH_TTL_DAYS`` reads as the longest a clinician can stay signed in without entering
a password, and every other control here assumes that: reuse detection needs the legitimate
client to present the stale token before it fires, and the idle timeout only ends sessions
nobody is using. A rotation that recomputes ``expires_at`` from *now* leaves the deployment
with no control that ends an actively-used session at all — so a refresh token lifted off a
device works for as long as its holder keeps rotating it, and the device it came from, put
away in a drawer, never presents the stale token that would give the theft away.

The fix is one column: ``family_started_at``, set at sign-in and carried unchanged through
every rotation, with ``expires_at`` anchored to it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.user import Session

PASSWORD = "password123"


async def _signup(client, email="lifetime@example.com"):
    resp = await client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _refresh(client, token):
    return await client.post("/api/v1/auth/refresh", json={"refresh_token": token})


async def _sessions(db) -> list[Session]:
    result = await db.execute(select(Session).order_by(Session.created_at))
    return list(result.scalars().all())


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_rotation_carries_the_family_start_forward(client, db):
    """The rotated pair belongs to the same sign-in, so it is dated from the same moment."""
    tokens = await _signup(client)
    first = (await _sessions(db))[0]
    started = _aware(first.family_started_at)

    resp = await _refresh(client, tokens["refresh_token"])
    assert resp.status_code == 200, resp.text

    rows = await _sessions(db)
    assert len(rows) == 2
    assert _aware(rows[1].family_started_at) == started


@pytest.mark.asyncio
async def test_rotation_does_not_move_the_absolute_expiry(client, db):
    """The regression: the new row must not be given a fresh full TTL."""
    tokens = await _signup(client)
    original_expiry = _aware((await _sessions(db))[0].expires_at)

    for _ in range(3):
        resp = await _refresh(client, tokens["refresh_token"])
        assert resp.status_code == 200, resp.text
        tokens = resp.json()

    rows = await _sessions(db)
    assert len(rows) == 4
    assert {_aware(row.expires_at) for row in rows} == {original_expiry}


@pytest.mark.asyncio
async def test_a_family_older_than_the_ttl_can_no_longer_be_rotated(client, db):
    """The ceiling actually bites: an old sign-in has to re-enter a password.

    The family is aged past ``jwt_refresh_ttl_days`` while staying well inside the idle
    timeout, so the only thing that can stop this rotation is the absolute expiry.
    """
    tokens = await _signup(client)
    rows = await _sessions(db)
    aged = datetime.now(UTC) - timedelta(days=settings.jwt_refresh_ttl_days, minutes=1)
    rows[0].family_started_at = aged
    rows[0].expires_at = aged + timedelta(days=settings.jwt_refresh_ttl_days)
    rows[0].last_used_at = datetime.now(UTC)
    await db.commit()

    resp = await _refresh(client, tokens["refresh_token"])
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_token"


@pytest.mark.asyncio
async def test_rotation_near_the_ceiling_cannot_reach_past_it(client, db):
    """A client refreshing on the last day gets a pair that still expires on schedule, not one
    that buys it another full week. This is the shape of the old bug: each rotation was
    individually reasonable and the sequence never ended."""
    tokens = await _signup(client)
    rows = await _sessions(db)
    nearly_over = (
        datetime.now(UTC) - timedelta(days=settings.jwt_refresh_ttl_days) + timedelta(hours=1)
    )
    rows[0].family_started_at = nearly_over
    rows[0].expires_at = nearly_over + timedelta(days=settings.jwt_refresh_ttl_days)
    rows[0].last_used_at = datetime.now(UTC)
    await db.commit()

    resp = await _refresh(client, tokens["refresh_token"])
    assert resp.status_code == 200, resp.text

    rotated = (await _sessions(db))[-1]
    assert _aware(rotated.expires_at) < datetime.now(UTC) + timedelta(hours=2)


@pytest.mark.asyncio
async def test_a_fresh_sign_in_starts_a_new_family(client, db):
    """Signing in again is how a clinician gets their full window back — the password is the
    thing the ceiling exists to require."""
    await _signup(client)
    first_expiry = _aware((await _sessions(db))[0].expires_at)
    rows = await _sessions(db)
    rows[0].family_started_at = datetime.now(UTC) - timedelta(days=3)
    rows[0].expires_at = rows[0].family_started_at + timedelta(days=settings.jwt_refresh_ttl_days)
    await db.commit()

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "lifetime@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text

    newest = (await _sessions(db))[-1]
    assert _aware(newest.expires_at) > first_expiry - timedelta(minutes=1)
    assert _aware(newest.family_started_at) > datetime.now(UTC) - timedelta(minutes=1)
