"""A field carrying a refresh token is bounded, and the refusal costs nothing.

Three request fields accepted a string of any length: ``RefreshRequest.refresh_token`` and the
``keep_current_refresh_token`` on ``LogoutAllRequest`` and ``PasswordChangeRequest``. They were
the last unbounded strings in a request body — every other one was given a ``max_length`` in an
earlier round — and two of the routes reading them (``POST /auth/refresh``, ``POST /auth/logout``)
take no credential at all, so the only ceiling was the 24 MB whole-body limit that applies to
any request.

The cost per request is not dramatic: a few megabytes read, validated, SHA-256'd, and used as a
lookup key against a token that was never going to be found. The point is that it was unbounded
and unattributable — on the two routes where nothing identifies the caller well enough to charge
it to an account, so no per-account ceiling applies.

A token this API issues is 64 hex characters (``generate_refresh_token``), so the limit is
generous by a factor of four and no real client can reach it.
"""

from __future__ import annotations

import pytest

from app.core.security import generate_refresh_token
from app.schemas.auth import MAX_REFRESH_TOKEN_CHARS

OVERSIZED = "a" * (MAX_REFRESH_TOKEN_CHARS + 1)


def test_a_real_token_is_far_inside_the_limit():
    """If a token this API issues were near the ceiling, the ceiling would be a latent outage."""
    assert len(generate_refresh_token()) * 4 <= MAX_REFRESH_TOKEN_CHARS


@pytest.mark.asyncio
async def test_refresh_refuses_an_oversized_token(client):
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": OVERSIZED})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_logout_refuses_an_oversized_token(client):
    resp = await client.post("/api/v1/auth/logout", json={"refresh_token": OVERSIZED})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_an_oversized_token_is_never_hashed_or_looked_up(client, monkeypatch):
    """Refused by the schema, before the value reaches the work.

    A ``max_length`` that let the body through and rejected it later would bound the field and
    not the cost, which is the thing being bounded.
    """
    from app.core import security

    calls: list[str] = []
    original = security.hash_token
    monkeypatch.setattr(
        security, "hash_token", lambda token: (calls.append(token), original(token))[1]
    )

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": OVERSIZED})

    assert resp.status_code == 422
    assert not [call for call in calls if len(call) > MAX_REFRESH_TOKEN_CHARS]


@pytest.mark.asyncio
async def test_an_empty_refresh_token_is_a_validation_error_not_a_lookup(client):
    """``min_length=1``: an empty string is not a token, and hashing it produces a perfectly
    valid digest to go looking for."""
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_well_formed_token_still_works_end_to_end(client):
    """The ceiling must not be in the way of the ordinary path."""
    signup = await client.post(
        "/api/v1/auth/signup",
        json={"email": "bounds@example.com", "password": "password123"},
    )
    assert signup.status_code == 201
    refresh_token = signup.json()["refresh_token"]
    assert len(refresh_token) <= MAX_REFRESH_TOKEN_CHARS

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_logout_all_refuses_an_oversized_keep_token(auth_client):
    resp = await auth_client.post(
        "/api/v1/auth/logout-all", json={"keep_current_refresh_token": OVERSIZED}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_password_change_refuses_an_oversized_keep_token(auth_client):
    resp = await auth_client.post(
        "/api/v1/auth/password",
        json={
            "current_password": "password123",
            "new_password": "a-new-password-1",
            "keep_current_refresh_token": OVERSIZED,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_the_keep_token_stays_optional(auth_client):
    """Omitting it means "sign me out everywhere, including here" — a real choice, not a
    missing field."""
    resp = await auth_client.post("/api/v1/auth/logout-all", json={})
    assert resp.status_code == 200, resp.text
