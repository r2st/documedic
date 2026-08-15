"""An access token must not outlive the sign-in it was minted under.

A JWT is valid until its ``exp``, so on its own an access token is a credential nothing can
withdraw. Every revocation this service offers acted only on the *refresh* token, which made
each of them a promise kept ``JWT_ACCESS_TTL_MINUTES`` late — fifteen minutes, by default, of
continued read/write access to patient charts after the clinician was told the device was
signed out.

The four ways a sign-in ends are covered here, and they are not variations on one theme; each
is a different clinical story:

* "Sign out everywhere", after a laptop is lost.
* "Sign out this other device", from the session list.
* A password change, which ``AuthService.change_password`` documents as the thing you do when
  a workstation was left unlocked — so the intruder's live token is the entire point.
* A password reset, where the clinician has proved possession of a mailed token and *not* of
  the old password, so no session may be assumed to be theirs.

Plus the two ways a sign-in ends without anyone asking: refresh-token reuse detection (which
revokes the family precisely because one holder is an attacker) and the absolute expiry that
``_issue_tokens`` anchors to the start of the token family.

Each test asserts on a *patient* route rather than on an auth route, because reading a chart
is what the token being alive actually costs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import create_access_token, decode_token
from app.models.user import Session as AuthSession

PATIENTS = "/api/v1/patients"


async def _signin(client: AsyncClient, email: str, password: str = "password123") -> dict:
    """A fresh sign-in for an existing account, returning its token pair."""
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _second_device(
    app, email: str, password: str = "password123"
) -> tuple[AsyncClient, dict]:
    """A second signed-in client on the same account — a clinician's other machine."""
    ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    tokens = await _signin(ac, email, password)
    ac.headers["Authorization"] = f"Bearer {tokens['access_token']}"
    return ac, tokens


# --- The claim itself ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_access_token_names_the_session_that_minted_it(auth_client, db):
    """The binding is a claim on the token, matching a real row — not an inference."""
    tokens = await _signin(auth_client, "doc@example.com")
    payload = decode_token(tokens["access_token"])

    assert "asid" in payload, "the access token carries no sign-in to check against"
    session = await db.get(AuthSession, uuid.UUID(payload["asid"]))
    assert session is not None, "asid names no session row"
    assert session.is_revoked is False


@pytest.mark.asyncio
async def test_a_token_naming_no_session_is_refused(auth_client, db):
    """A token with no ``asid`` is refused rather than waved through.

    This is what keeps the claim from being optional in practice: were a missing claim treated
    as "nothing to check", forging one would be as easy as omitting a field. It is also the
    shape of every token minted before this claim existed, and refusing them is deliberate —
    the client answers a 401 by rotating its refresh token, so the cost at deploy is one extra
    round-trip per signed-in browser.
    """
    account_id = (await db.execute(select(AuthSession.account_id))).scalars().first()
    assert account_id is not None

    resp = await auth_client.get(PATIENTS, headers=_bearer(create_access_token(account_id)))

    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_a_token_naming_a_session_that_does_not_exist_is_refused(auth_client, db):
    """A well-formed but invented ``asid`` must not authenticate anything."""
    account_id = (await db.execute(select(AuthSession.account_id))).scalars().first()
    forged = create_access_token(account_id, auth_session_id=uuid.uuid4())

    resp = await auth_client.get(PATIENTS, headers=_bearer(forged))

    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_a_malformed_asid_is_refused_not_crashed(auth_client, db):
    """A non-uuid ``asid`` is a 401, not a 500 — it is attacker-controlled input."""
    account_id = (await db.execute(select(AuthSession.account_id))).scalars().first()
    forged = create_access_token(account_id, auth_session_id="not-a-uuid")

    resp = await auth_client.get(PATIENTS, headers=_bearer(forged))

    assert resp.status_code == 401, resp.text


# --- The four deliberate revocations -------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_out_everywhere_stops_the_other_devices_access_token(app, auth_client):
    """The lost-laptop case. Its access token must stop working on the next request."""
    other, _ = await _second_device(app, "doc@example.com")
    try:
        assert (await other.get(PATIENTS)).status_code == 200, "precondition: the device is in"

        signed_out = await auth_client.post("/api/v1/auth/logout-all", json={})
        assert signed_out.status_code == 200, signed_out.text

        assert (await other.get(PATIENTS)).status_code == 401, (
            "'sign out everywhere' left the other device's access token reading patient charts"
        )
    finally:
        await other.aclose()


@pytest.mark.asyncio
async def test_signing_out_one_device_leaves_the_others_alone(app, auth_client, db):
    """The revocation must be exactly as wide as the clinician asked for.

    A fix that signed *every* device out on any revocation would pass the test above and be
    unusable: a clinician signing out a phone would lose the consultation open in front of
    them.
    """
    other, other_tokens = await _second_device(app, "doc@example.com")
    try:
        other_asid = decode_token(other_tokens["access_token"])["asid"]

        revoked = await auth_client.delete(f"/api/v1/auth/sessions/{other_asid}")
        assert revoked.status_code == 200, revoked.text

        assert (await other.get(PATIENTS)).status_code == 401, "the named device is still signed in"
        assert (await auth_client.get(PATIENTS)).status_code == 200, (
            "signing out another device signed out the clinician doing it"
        )
    finally:
        await other.aclose()


@pytest.mark.asyncio
async def test_a_password_change_stops_the_other_devices_access_token(app, auth_client):
    """The unlocked-workstation case, which is what ``change_password`` exists for.

    The clinician keeps their own tab (they pass ``keep_current_refresh_token``); the intruder's
    device must be out immediately, not at the end of its token's TTL.
    """
    intruder, _ = await _second_device(app, "doc@example.com")
    try:
        assert (await intruder.get(PATIENTS)).status_code == 200

        me = await _signin(auth_client, "doc@example.com")
        auth_client.headers["Authorization"] = f"Bearer {me['access_token']}"
        changed = await auth_client.post(
            "/api/v1/auth/password",
            json={
                "current_password": "password123",
                "new_password": "a-much-better-password",
                "keep_current_refresh_token": me["refresh_token"],
            },
        )
        assert changed.status_code == 200, changed.text

        assert (await intruder.get(PATIENTS)).status_code == 401, (
            "the password change left the borrowed workstation reading the chart"
        )
        assert (await auth_client.get(PATIENTS)).status_code == 200, (
            "changing your own password signed you out of the tab you did it from"
        )
    finally:
        await intruder.aclose()


@pytest.mark.asyncio
async def test_a_password_reset_stops_every_access_token(app, client, db, monkeypatch):
    """A reset proves possession of a token, not of the old password — so nothing survives."""
    from app.config import settings

    # The token is delivered out of band by default; `response` puts it in the body, which is
    # how the rest of the reset suite drives this flow end to end.
    monkeypatch.setattr(settings, "password_reset_delivery", "response")
    email = "reset-token@example.com"
    signup = await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "password123"}
    )
    assert signup.status_code == 201, signup.text
    device, _ = await _second_device(app, email)
    try:
        assert (await device.get(PATIENTS)).status_code == 200

        asked = await client.post("/api/v1/auth/password-reset/request", json={"email": email})
        assert asked.status_code == 202, asked.text
        token = asked.json().get("reset_token")
        assert token, "demo mode returns the token in the body; without it this cannot proceed"

        done = await client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "a-much-better-password"},
        )
        assert done.status_code == 200, done.text

        assert (await device.get(PATIENTS)).status_code == 401, (
            "the reset left an already-signed-in device reading patient charts"
        )
    finally:
        await device.aclose()


# --- The two revocations nobody asks for ---------------------------------------------------


@pytest.mark.asyncio
async def test_detected_refresh_token_reuse_stops_the_access_tokens_too(app, client):
    """Reuse detection revokes the family because one of the two holders is an attacker.

    Leaving their access token alive for its remaining TTL is the part of the response that was
    missing: the whole point of the family revocation is that we cannot tell which holder is
    which, so both must lose access at the same moment.
    """
    email = "reuse@example.com"
    signup = await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "password123"}
    )
    assert signup.status_code == 201, signup.text
    stolen_refresh = signup.json()["refresh_token"]

    device, _ = await _second_device(app, email)
    try:
        assert (await device.get(PATIENTS)).status_code == 200

        # The legitimate client rotates once, then the captured copy is replayed.
        rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": stolen_refresh})
        assert rotated.status_code == 200, rotated.text
        replayed = await client.post("/api/v1/auth/refresh", json={"refresh_token": stolen_refresh})
        assert replayed.status_code == 401, "reuse was not detected, so this proves nothing"

        rotated_client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        rotated_client.headers["Authorization"] = f"Bearer {rotated.json()['access_token']}"
        try:
            assert (await device.get(PATIENTS)).status_code == 401
            assert (await rotated_client.get(PATIENTS)).status_code == 401, (
                "the family was revoked but its newest access token still reads charts"
            )
        finally:
            await rotated_client.aclose()
    finally:
        await device.aclose()


@pytest.mark.asyncio
async def test_an_access_token_does_not_outlive_its_sessions_absolute_expiry(auth_client, db):
    """``expires_at`` is the ceiling on a sign-in, so the last access token is inside it too.

    Distinct from the revoked check and not implied by it: this row is never revoked, it simply
    ran out. Without the expiry half, a family that reached its absolute ceiling would still
    have one working access token for the remainder of its own TTL.
    """
    tokens = await _signin(auth_client, "doc@example.com")
    asid = uuid.UUID(decode_token(tokens["access_token"])["asid"])
    headers = _bearer(tokens["access_token"])

    assert (await auth_client.get(PATIENTS, headers=headers)).status_code == 200

    session = await db.get(AuthSession, asid)
    session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    resp = await auth_client.get(PATIENTS, headers=headers)
    assert resp.status_code == 401, "an access token outlived its session's absolute expiry"


# --- The stream token, which authenticates from the query string -------------------------


@pytest.mark.asyncio
async def test_a_stream_token_is_bound_to_the_sign_in_that_minted_it(auth_client, db):
    """The SSE endpoint holds its connection open for a whole reasoning run.

    That makes it the endpoint where "the token has not expired" is furthest from "this device
    is still signed in", so the stream token inherits the sign-in of the access token that
    asked for it.
    """
    from tests.conftest import create_patient

    patient = await create_patient(auth_client, full_name="Stream Revocation")
    started = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "chest pain for two days"},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session"]["id"]

    minted = await auth_client.post(f"/api/v1/reasoning/{session_id}/stream-token", json={})
    assert minted.status_code == 200, minted.text
    stream_token = minted.json()["token"]
    payload = decode_token(stream_token)

    assert payload["sid"] == session_id, "the reasoning-session binding must be untouched"
    assert "asid" in payload, "the stream token names no sign-in, so nothing can withdraw it"

    session = await db.get(AuthSession, uuid.UUID(payload["asid"]))
    session.is_revoked = True
    await db.commit()

    resp = await auth_client.get(
        f"/api/v1/reasoning/{session_id}/stream?token={stream_token}",
        headers={"Authorization": ""},
    )
    assert resp.status_code == 401, (
        "a stream token kept streaming a signed-out device's reasoning run"
    )
