"""This API carries its session in an Authorization header and never in a cookie.

That is a design decision, not an accident, and it is the reason several defences this
codebase would otherwise need are absent. A cookie is attached by the browser to *every*
request to the origin, including one triggered by a third-party page — which is what CSRF is —
so a cookie-bearing API needs SameSite, an anti-CSRF token, or both. A bearer token is attached
only by code that deliberately reads it, so a cross-site form post to this API arrives
unauthenticated and there is nothing to forge.

``CORSMiddleware`` here is configured ``allow_credentials=True``, which is what makes the
combination worth pinning: the day a route sets a session cookie, that flag turns every
allowed origin into a CSRF surface, and nothing in the suite would have failed. So this file
asserts the absence — across the routes that would be the tempting place to add one — and
states what a future cookie would have to carry.
"""

from __future__ import annotations

import pytest

from tests.conftest import create_patient


@pytest.mark.asyncio
async def test_signing_up_sets_no_cookie(client):
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "cookie-signup@example.com", "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    assert "set-cookie" not in resp.headers


@pytest.mark.asyncio
async def test_signing_in_sets_no_cookie(client):
    """The obvious candidate. The token pair comes back in the JSON body, where only code
    that asks for it can see it."""
    await client.post(
        "/api/v1/auth/signup",
        json={"email": "cookie-login@example.com", "password": "password123"},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-login@example.com", "password": "password123"},
    )
    assert resp.status_code == 200, resp.text
    assert "set-cookie" not in resp.headers
    assert resp.json()["access_token"]


@pytest.mark.asyncio
async def test_refreshing_sets_no_cookie(client):
    """The second candidate: a refresh token is exactly the long-lived credential a
    cookie-based design would park in an HttpOnly cookie."""
    await client.post(
        "/api/v1/auth/signup",
        json={"email": "cookie-refresh@example.com", "password": "password123"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-refresh@example.com", "password": "password123"},
    )
    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert resp.status_code == 200, resp.text
    assert "set-cookie" not in resp.headers


@pytest.mark.asyncio
async def test_no_authenticated_route_sets_a_cookie(auth_client):
    """A sweep over the ordinary clinical routes, not just the auth ones. A cookie set
    anywhere in the API is a cookie the browser sends everywhere in it."""
    patient = await create_patient(auth_client)
    paths = [
        "/api/v1/patients",
        f"/api/v1/patients/{patient['id']}",
        f"/api/v1/patients/{patient['id']}/record",
        f"/api/v1/patients/{patient['id']}/documents",
        f"/api/v1/patients/{patient['id']}/safety/flags",
        "/health",
    ]
    for path in paths:
        resp = await auth_client.get(path)
        assert "set-cookie" not in resp.headers, path


@pytest.mark.asyncio
async def test_a_cookie_sent_by_a_client_authenticates_nothing(client):
    """The other half of the invariant. Even if something upstream — a proxy, a stale
    deployment, a browser extension — puts a token in a cookie, this API must not accept it:
    the moment it does, every cross-site request carries credentials."""
    await client.post(
        "/api/v1/auth/signup",
        json={"email": "cookie-auth@example.com", "password": "password123"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-auth@example.com", "password": "password123"},
    )
    token = login.json()["access_token"]
    resp = await client.get(
        "/api/v1/patients",
        headers={"Cookie": f"access_token={token}; session={token}; Authorization={token}"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_the_token_is_accepted_only_as_a_bearer_header(client):
    """...and the same token in the header does work, so the test above is proving the
    transport is rejected rather than the token being bad."""
    await client.post(
        "/api/v1/auth/signup",
        json={"email": "cookie-bearer@example.com", "password": "password123"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-bearer@example.com", "password": "password123"},
    )
    resp = await client.get(
        "/api/v1/patients",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert resp.status_code == 200, resp.text
