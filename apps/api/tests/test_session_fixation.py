"""A sign-in cannot be handed to the server; it can only be minted by one.

Session fixation is the attack where the attacker chooses the session identifier *before*
authentication and the server keeps using it afterwards — classically by setting a cookie on the
victim's browser and waiting for them to log in. This service is not cookie-based and has no
pre-authentication session concept, so the attack has no purchase here. That is a claim worth
holding to the code rather than to the architecture diagram, because it stops being true the
first time someone accepts a caller-supplied identifier "so the client can resume a session".

What is asserted:

* every sign-in mints a fresh, unpredictable refresh token and a fresh ``sessions`` row, so
  nothing an attacker could have observed or planted before the sign-in survives it;
* no request may name the sign-in it wants — the identifier is only ever read out of a token the
  server signed;
* the two identities inside a token must agree. ``sub`` names the account and ``asid`` names the
  sign-in, and nothing may present a pair the server did not mint together.

The last one is an invariant rather than a repaired hole: ``_issue_tokens`` writes both claims
in one place from one account, so no mismatched pair exists today. It is checked because "no
call site does that yet" is a property of the current call sites, and an impersonation route or
a token helper added later would break it silently — the token would be signed, the account real
and the session live, and the request would be served against someone else's sign-in.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.security import create_access_token, decode_token
from app.models.user import Account, Session

PREFIX = "/api/v1/auth"
PASSWORD = "correct-horse-battery"


async def _signup(client, email: str) -> dict:
    resp = await client.post(
        f"{PREFIX}/signup",
        json={"email": email, "password": PASSWORD, "display_name": "Dr X"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _login(client, email: str) -> dict:
    resp = await client.post(f"{PREFIX}/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _me(client, access_token: str):
    return await client.get(f"{PREFIX}/me", headers={"Authorization": f"Bearer {access_token}"})


@pytest.mark.asyncio
async def test_each_sign_in_mints_a_new_session_rather_than_reusing_one(client, db):
    """The property fixation attacks: authenticating must not continue an identifier that
    existed beforehand."""
    first = await _signup(client, "clinician@example.org")
    second = await _login(client, "clinician@example.org")

    assert first["refresh_token"] != second["refresh_token"]
    assert (
        decode_token(first["access_token"])["asid"] != decode_token(second["access_token"])["asid"]
    )

    rows = (await db.execute(select(Session))).scalars().all()
    assert len({r.id for r in rows}) == 2
    assert len({r.token_hash for r in rows}) == 2, "two sign-ins shared a stored token"


@pytest.mark.asyncio
async def test_a_refresh_token_is_never_echoed_back_from_the_request(client):
    """A caller cannot propose the token it would like to be given. The classic fixation setup
    is exactly this: submit a chosen identifier alongside the credentials and have the server
    adopt it."""
    await _signup(client, "clinician@example.org")
    planted = "attacker-chosen-refresh-token-value"

    resp = await client.post(
        f"{PREFIX}/login",
        json={"email": "clinician@example.org", "password": PASSWORD, "refresh_token": planted},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["refresh_token"] != planted

    replayed = await client.post(f"{PREFIX}/refresh", json={"refresh_token": planted})
    assert replayed.status_code == 401


@pytest.mark.asyncio
async def test_the_session_named_by_a_token_must_belong_to_its_subject(client, db):
    """``sub`` and ``asid`` must be a pair the server minted together. Here they are both real —
    a real account, a real live session — and simply not each other's."""
    await _signup(client, "attacker@example.org")
    victim = await _signup(client, "victim@example.org")

    attacker_account = (
        (await db.execute(select(Account).where(Account.email == "attacker@example.org")))
        .scalars()
        .one()
    )
    victims_session_id = decode_token(victim["access_token"])["asid"]

    crossed = create_access_token(attacker_account.id, auth_session_id=victims_session_id)
    resp = await _me(client, crossed)
    assert resp.status_code == 401, "a token rode another account's live sign-in"


@pytest.mark.asyncio
async def test_the_crossed_token_is_refused_in_both_directions(client, db):
    """Stated the other way round, so the check cannot be satisfied by an ordering accident:
    the victim's subject paired with the attacker's own session is equally invalid."""
    attacker = await _signup(client, "attacker@example.org")
    await _signup(client, "victim@example.org")

    victim_account = (
        (await db.execute(select(Account).where(Account.email == "victim@example.org")))
        .scalars()
        .one()
    )
    attackers_session_id = decode_token(attacker["access_token"])["asid"]

    crossed = create_access_token(victim_account.id, auth_session_id=attackers_session_id)
    assert (await _me(client, crossed)).status_code == 401


@pytest.mark.asyncio
async def test_an_invented_session_id_is_refused(client, db):
    """A well-formed asid naming no row at all — the shape a fixation attempt takes when the
    attacker guesses rather than steals."""
    await _signup(client, "clinician@example.org")
    account = (
        (await db.execute(select(Account).where(Account.email == "clinician@example.org")))
        .scalars()
        .one()
    )

    invented = create_access_token(account.id, auth_session_id=uuid.uuid4())
    assert (await _me(client, invented)).status_code == 401


@pytest.mark.asyncio
async def test_the_matching_pair_still_works(client):
    """The control. Without this the four refusals above would also pass if the dependency
    rejected everything."""
    tokens = await _signup(client, "clinician@example.org")
    assert (await _me(client, tokens["access_token"])).status_code == 200
