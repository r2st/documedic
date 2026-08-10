"""SSE stream authentication.

EventSource cannot set an Authorization header, so the SSE route also accepts ?token=.
A query parameter is recorded verbatim by nginx's default log format and kept in browser
history, so what travels there must be a narrowly scoped, short-lived credential — never
the account's real access token.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.config import settings
from app.core.security import create_access_token, create_stream_token, decode_token
from tests.conftest import create_patient


async def _session_id(auth_client) -> str:
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "fever and cough for three days"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["session"]["id"]


# --- Token shape ------------------------------------------------------------------------


def test_a_stream_token_is_scoped_to_one_session_and_expires_quickly():
    account_id, session_id = uuid.uuid4(), uuid.uuid4()
    payload = decode_token(create_stream_token(account_id, session_id))

    assert payload["type"] == "stream"
    assert payload["sub"] == str(account_id)
    assert payload["sid"] == str(session_id)

    ttl = payload["exp"] - payload["iat"]
    assert ttl == settings.stream_token_ttl_seconds
    assert ttl <= 300, "a token that reaches the access log must be short-lived"


def test_a_stream_token_is_shorter_lived_than_an_access_token():
    stream = decode_token(create_stream_token(uuid.uuid4(), uuid.uuid4()))
    access = decode_token(create_access_token(uuid.uuid4()))
    assert (stream["exp"] - stream["iat"]) < (access["exp"] - access["iat"])


# --- Minting ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minting_a_stream_token_requires_the_bearer_header(client, auth_client):
    session_id = await _session_id(auth_client)
    del client.headers["Authorization"]

    resp = await client.post(f"/api/v1/reasoning/{session_id}/stream-token")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_minting_returns_a_token_bound_to_the_requested_session(auth_client):
    session_id = await _session_id(auth_client)

    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/stream-token")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expires_in"] == settings.stream_token_ttl_seconds

    payload = decode_token(body["token"])
    assert payload["type"] == "stream"
    assert payload["sid"] == session_id


@pytest.mark.asyncio
async def test_minting_for_an_unknown_session_is_not_found(auth_client):
    resp = await auth_client.post(f"/api/v1/reasoning/{uuid.uuid4()}/stream-token")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_minting_for_another_accounts_session_is_not_found(client, auth_client):
    session_id = await _session_id(auth_client)

    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "stream-intruder@example.com", "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    resp = await client.post(f"/api/v1/reasoning/{session_id}/stream-token")
    assert resp.status_code == 404


# --- Stream authentication --------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_access_token_is_no_longer_accepted_in_the_query_string(client, auth_client):
    """The whole point of the change: a full-privilege token must never be URL-borne."""
    session_id = await _session_id(auth_client)
    access = auth_client.headers["Authorization"].split(" ", 1)[1]
    del client.headers["Authorization"]

    resp = await client.get(f"/api/v1/reasoning/{session_id}/stream?token={access}")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_a_stream_token_for_a_different_session_is_rejected(client, auth_client):
    session_id = await _session_id(auth_client)
    other = await _session_id(auth_client)
    assert session_id != other

    minted = await auth_client.post(f"/api/v1/reasoning/{other}/stream-token")
    token = minted.json()["token"]
    del client.headers["Authorization"]

    resp = await client.get(f"/api/v1/reasoning/{session_id}/stream?token={token}")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_an_expired_stream_token_is_rejected(client, auth_client):
    session_id = await _session_id(auth_client)
    stale = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "sid": session_id,
            "type": "stream",
            "iat": int((datetime.now(UTC) - timedelta(minutes=10)).timestamp()),
            "exp": int((datetime.now(UTC) - timedelta(minutes=9)).timestamp()),
        },
        settings.app_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    del client.headers["Authorization"]

    resp = await client.get(f"/api/v1/reasoning/{session_id}/stream?token={stale}")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_a_stream_token_forged_with_the_wrong_secret_is_rejected(client, auth_client):
    session_id = await _session_id(auth_client)
    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "sid": session_id,
            "type": "stream",
            "exp": int((datetime.now(UTC) + timedelta(minutes=1)).timestamp()),
        },
        "not-the-real-secret",
        algorithm=settings.jwt_algorithm,
    )
    del client.headers["Authorization"]

    resp = await client.get(f"/api/v1/reasoning/{session_id}/stream?token={forged}")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_a_stream_token_is_not_accepted_on_ordinary_endpoints(client, auth_client):
    """Scoping cuts both ways: the leaked-by-design token must open nothing else."""
    session_id = await _session_id(auth_client)
    minted = await auth_client.post(f"/api/v1/reasoning/{session_id}/stream-token")
    token = minted.json()["token"]

    client.headers["Authorization"] = f"Bearer {token}"
    for path in (
        "/api/v1/auth/me",
        "/api/v1/patients",
        f"/api/v1/reasoning/{session_id}/suggestions",
    ):
        resp = await client.get(path)
        assert resp.status_code in (401, 403), f"{path} accepted a stream token"


@pytest.mark.asyncio
async def test_a_stream_token_for_a_deleted_account_is_rejected(client, auth_client, db):
    """Revocation still works: the token is scoped, not a bearer bypass of account state."""
    from sqlalchemy import select

    from app.models.user import Account

    session_id = await _session_id(auth_client)
    minted = await auth_client.post(f"/api/v1/reasoning/{session_id}/stream-token")
    token = minted.json()["token"]

    account = (
        await db.execute(select(Account).where(Account.email == "doc@example.com"))
    ).scalar_one()
    account.is_deleted = True
    await db.commit()

    del client.headers["Authorization"]
    resp = await client.get(f"/api/v1/reasoning/{session_id}/stream?token={token}")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_the_bearer_header_still_authenticates_the_stream(auth_client):
    """Non-browser clients (and tests) can skip the minting round trip."""
    session_id = await _session_id(auth_client)
    async with auth_client.stream(
        "GET", f"/api/v1/reasoning/{session_id}/stream"
    ) as resp:
        assert resp.status_code == 200
