"""Route-level authorization sweep.

Two guarantees that are easy to lose when adding an endpoint:

1. Every route that is not explicitly public rejects an anonymous request.
2. Every patient-scoped route rejects a *different* account's patient id, so one
   clinician can never read or mutate another clinician's panel (DPDP purpose
   limitation, and the base multi-tenancy invariant).

Both are enumerated from the live FastAPI router table rather than hand-listed, so a
new endpoint is covered the moment it is registered.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient

from tests.conftest import create_patient

# Routes intentionally reachable without an Authorization header. Anything not listed
# here must reject anonymous callers.
PUBLIC_PATHS = {
    "/api/v1/auth/signup",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",  # takes the refresh token in the body, not a bearer header
    # Probes are mounted outside the versioned prefix for the load balancer.
    "/health",
    "/health/live",
    "/health/ready",
    "/health/dependencies",
}

# The SSE stream authenticates from a `token` query parameter because EventSource cannot
# set headers; it must still reject a request that carries no token at all.
QUERY_TOKEN_PATHS = {"/api/v1/reasoning/{session_id}/stream"}

PLACEHOLDER = {
    "patient_id": lambda ctx: ctx["patient_id"],
    "doc_id": lambda _ctx: str(uuid.uuid4()),
    "session_id": lambda _ctx: str(uuid.uuid4()),
    "suggestion_id": lambda _ctx: str(uuid.uuid4()),
    "run_id": lambda _ctx: str(uuid.uuid4()),
    "condition_name": lambda _ctx: "dengue",
}


def _routes(app) -> list[tuple[str, str, str]]:
    """(method, path_template, concrete_path_with_placeholders_unfilled) for real routes."""
    out: list[tuple[str, str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, route.path, route.path))
    return out


def _fill(path: str, ctx: dict) -> str:
    for name, factory in PLACEHOLDER.items():
        path = path.replace("{" + name + "}", str(factory(ctx)))
    return path


async def _call(client: AsyncClient, method: str, path: str):
    return await client.request(method, path, json={})


@pytest.mark.asyncio
async def test_every_non_public_route_rejects_anonymous_requests(app, client):
    """No clinical endpoint may be reachable without authentication."""
    ctx = {"patient_id": str(uuid.uuid4())}
    leaked: list[str] = []

    for method, template, _ in _routes(app):
        if template in PUBLIC_PATHS:
            continue
        resp = await _call(client, method, _fill(template, ctx))
        # 401/403 = rejected. 422 means validation ran before auth on a body we faked,
        # which still never reaches clinical data; anything else is a leak.
        if resp.status_code not in (401, 403, 422):
            leaked.append(f"{method} {template} -> {resp.status_code}")

    assert not leaked, "routes reachable without authentication: " + "; ".join(leaked)


@pytest.mark.asyncio
async def test_public_route_allowlist_matches_the_router_table(app):
    """Guard against a typo in PUBLIC_PATHS silently exempting a real endpoint."""
    registered = {template for _, template, _ in _routes(app)}
    stale = PUBLIC_PATHS - registered
    assert not stale, f"allowlist names routes that no longer exist: {sorted(stale)}"


@pytest.mark.asyncio
async def test_sse_stream_rejects_a_request_with_no_token(client):
    """EventSource cannot send headers, so the query-token path must still be enforced."""
    for path in QUERY_TOKEN_PATHS:
        resp = await client.get(_fill(path, {"patient_id": str(uuid.uuid4())}))
        assert resp.status_code in (401, 403), f"{path} -> {resp.status_code}"


@pytest.mark.asyncio
async def test_sse_stream_rejects_a_forged_query_token(client):
    resp = await client.get(
        f"/api/v1/reasoning/{uuid.uuid4()}/stream?token=not-a-real-jwt",
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_patient_scoped_routes_reject_another_accounts_patient(app, client):
    """A patient id belonging to account A must 404 for account B on every route."""
    # Account A owns the patient.
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "owner@example.com", "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    patient = await create_patient(client)

    # Account B is a different clinician on the same deployment.
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "intruder@example.com", "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    ctx = {"patient_id": patient["id"]}
    leaked: list[str] = []
    for method, template, _ in _routes(app):
        if "{patient_id}" not in template:
            continue
        resp = await _call(client, method, _fill(template, ctx))
        # 404 (not found for this account) or 422 (body validation) are both safe;
        # a 2xx means account B touched account A's patient.
        if resp.status_code < 400:
            leaked.append(f"{method} {template} -> {resp.status_code}")

    assert not leaked, "cross-account access: " + "; ".join(leaked)


@pytest.mark.asyncio
async def test_a_malformed_bearer_token_is_rejected_everywhere(app, client):
    """Garbage in the Authorization header must never fall through to the anonymous path."""
    client.headers["Authorization"] = "Bearer garbage.token.value"
    ctx = {"patient_id": str(uuid.uuid4())}
    leaked: list[str] = []

    for method, template, _ in _routes(app):
        if template in PUBLIC_PATHS:
            continue
        resp = await _call(client, method, _fill(template, ctx))
        if resp.status_code not in (401, 403, 422):
            leaked.append(f"{method} {template} -> {resp.status_code}")

    assert not leaked, "routes accepting a forged token: " + "; ".join(leaked)


@pytest.mark.asyncio
async def test_a_refresh_token_is_not_accepted_as_an_access_token(client):
    """Token type confusion: the long-lived refresh token must not authenticate requests."""
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "typeconf@example.com", "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    refresh = resp.json()["refresh_token"]

    client.headers["Authorization"] = f"Bearer {refresh}"
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code in (401, 403)
