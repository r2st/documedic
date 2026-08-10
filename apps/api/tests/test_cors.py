"""CORS behaviour at runtime.

The production config guard (test_config_production.py) checks what an operator is *allowed*
to configure. This file checks what the middleware actually does with a valid configuration,
because the guard is worthless if the wiring below it does something else.

The stakes: this API answers with decrypted patient records to any request carrying a bearer
token. A permissive CORS policy turns any page a signed-in clinician visits into a reader of
that data, so "which origins get an Access-Control-Allow-Origin" is a PHI question.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import create_app

ALLOWED = "http://localhost:3000"
FOREIGN = "https://evil.example.com"


@pytest.fixture
def cors_app(monkeypatch):
    """A fresh app whose allowed origin list is exactly ``[ALLOWED]``.

    Built inside the fixture because CORSMiddleware snapshots the origin list at
    ``add_middleware`` time — patching settings after ``create_app`` would change nothing.
    """
    monkeypatch.setattr(settings, "cors_origins", ALLOWED)
    return create_app()


@pytest.fixture
def cors_client(cors_app):
    return AsyncClient(transport=ASGITransport(app=cors_app), base_url="http://test")


@pytest.mark.asyncio
async def test_preflight_from_an_allowed_origin_is_approved(cors_client):
    async with cors_client as client:
        resp = await client.options(
            "/api/v1/patients",
            headers={
                "Origin": ALLOWED,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ALLOWED
    assert resp.headers["access-control-allow-credentials"] == "true"


@pytest.mark.asyncio
async def test_preflight_from_a_foreign_origin_is_not_approved(cors_client):
    async with cors_client as client:
        resp = await client.options(
            "/api/v1/patients",
            headers={
                "Origin": FOREIGN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
    # Starlette answers the preflight with an explicit disallowed response and, crucially,
    # never emits an Access-Control-Allow-Origin the browser could act on.
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.asyncio
async def test_a_foreign_origin_gets_no_allow_origin_on_a_real_response(cors_client):
    """The header is what a browser enforces on; without it the response is unreadable.

    The request itself still executes (CORS is not server-side authorization — the bearer
    token is), so this asserts the only thing that actually protects the body.
    """
    async with cors_client as client:
        resp = await client.get("/health", headers={"Origin": FOREIGN})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.asyncio
async def test_an_allowed_origin_gets_the_allow_origin_header(cors_client):
    async with cors_client as client:
        resp = await client.get("/health", headers={"Origin": ALLOWED})
    assert resp.headers["access-control-allow-origin"] == ALLOWED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:3000.evil.example.com",  # allowed origin as a subdomain prefix
        "http://localhost:30000",  # port extension
        "https://localhost:3000",  # scheme swap
        "http://LOCALHOST:3000",  # case variation
        "null",  # sandboxed iframe / file://
    ],
)
async def test_near_miss_origins_are_not_treated_as_the_allowed_one(cors_client, origin):
    """Matching must be exact-string, never prefix/suffix/substring.

    ``http://localhost:3000.evil.example.com`` is a domain an attacker can register, and a
    naive ``startswith`` check would hand it every patient record the clinician can read.
    """
    async with cors_client as client:
        resp = await client.get("/health", headers={"Origin": origin})
    assert resp.headers.get("access-control-allow-origin") != origin
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.asyncio
async def test_the_request_id_header_is_the_only_thing_exposed_cross_origin(cors_client):
    """Expose-Headers governs what JS can read off the response.

    X-Request-Id is deliberately exposed for support correlation; nothing else should be,
    since response headers are a channel that bypasses the response-body review.
    """
    async with cors_client as client:
        resp = await client.get("/health", headers={"Origin": ALLOWED})
    exposed = resp.headers.get("access-control-expose-headers", "")
    assert [h.strip() for h in exposed.split(",") if h.strip()] == ["X-Request-Id"]
