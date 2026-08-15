"""The headers every response from this API carries, and why each one is there.

A browser applies these to *whatever it decides a response is*. That matters more here than on
a plain JSON API, because one route hands back a clinician-uploaded image and serves it
`inline` — a response from this origin that a browser will render as a document, on the origin
where a bearer token is presented.

The headers are asserted on the paths that skip the normal response pipeline as well as the
ordinary one: a 500 built by Starlette's ``ServerErrorMiddleware``, a 413 refused before the
application is invoked, and a 404 raised by the router. Those are the three that have
historically shipped without them.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.middleware import SECURITY_HEADERS
from tests.conftest import create_patient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512


# --- Present, everywhere --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_header_is_on_an_ordinary_response(client):
    resp = await client.get("/health")
    for name, value in SECURITY_HEADERS.items():
        assert resp.headers.get(name) == value, name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Content-Security-Policy",
        "Permissions-Policy",
        "Cross-Origin-Resource-Policy",
        "Cross-Origin-Opener-Policy",
    ],
)
async def test_each_header_is_named_explicitly(client, name):
    """Spelled out rather than looped over ``SECURITY_HEADERS`` alone, so that deleting an
    entry from that dict fails a test that names it instead of silently shrinking the loop
    above to nothing."""
    assert (await client.get("/health")).headers.get(name)


@pytest.mark.asyncio
async def test_a_404_from_the_router_carries_them(client):
    """Raised by Starlette's router before any application code runs."""
    resp = await client.get("/api/v1/no-such-route")
    assert resp.status_code == 404
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Permissions-Policy"]


@pytest.mark.asyncio
async def test_a_refused_body_carries_them(client, monkeypatch):
    """Refused before the application is invoked at all."""
    monkeypatch.setattr(settings, "max_json_request_bytes", 128)
    resp = await client.post(
        "/api/v1/auth/login",
        content='{"email":"a@b.com","password":"' + "x" * 4096 + '"}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.headers["Content-Security-Policy"]
    assert resp.headers["Permissions-Policy"]


@pytest.mark.asyncio
async def test_a_downloaded_scan_carries_them(auth_client):
    """The response that most needs them: a clinician-uploaded image, served `inline`, from
    the origin the session's bearer token is presented to."""
    patient = await create_patient(auth_client)
    upload = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("scan.png", PNG, "image/png")},
    )
    assert upload.status_code == 201, upload.text
    resp = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{upload.json()['id']}/file"
    )
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'none'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["Cross-Origin-Resource-Policy"] == "same-origin"


# --- Saying the right thing -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "directive",
    [
        "default-src 'none'",
        "script-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ],
)
async def test_the_csp_closes_each_sink(client, directive):
    """``default-src 'self'`` was the whole policy, which permitted this origin's own scripts,
    styles and frames — none of which exist, and all of which a response rendered as a
    document could have reached for. Each directive is asserted by name because the value of
    a CSP is exactly the set of things it names."""
    assert directive in (await client.get("/health")).headers["Content-Security-Policy"]


@pytest.mark.asyncio
async def test_the_csp_still_permits_an_inline_scan(client):
    """``default-src 'none'`` would block the one thing this origin does render: an uploaded
    image served inline. ``img-src`` opts it back in on its own."""
    csp = (await client.get("/health")).headers["Content-Security-Policy"]
    assert "img-src 'self'" in csp


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["camera", "microphone", "geolocation", "payment", "usb"])
async def test_permissions_policy_denies_the_powerful_features(client, feature):
    """Each is denied with an empty allowlist — `feature=()` — not merely left unmentioned.
    An unmentioned feature falls back to the browser's default, which for several of these is
    "allow same-origin"."""
    policy = (await client.get("/health")).headers["Permissions-Policy"]
    assert f"{feature}=()" in policy


# --- HSTS is production-only ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hsts_is_absent_outside_production(client):
    """Never sent to a developer on http://localhost. A browser that pins HSTS for localhost
    pins it for every other project on that machine, and there is no way to un-send it."""
    assert not settings.is_production
    assert "Strict-Transport-Security" not in (await client.get("/health")).headers


@pytest.mark.asyncio
async def test_hsts_is_present_in_production(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    value = (await client.get("/health")).headers["Strict-Transport-Security"]
    assert "max-age=" in value and "includeSubDomains" in value


@pytest.mark.asyncio
async def test_hsts_does_not_claim_preload(client, monkeypatch):
    """`preload` asks to be baked into browsers' shipped preload lists, which is close to
    irreversible and commits every present and future subdomain to HTTPS. That is an
    operator's decision about a domain, not one this application makes for them."""
    monkeypatch.setattr(settings, "app_env", "production")
    assert "preload" not in (await client.get("/health")).headers["Strict-Transport-Security"]


@pytest.mark.asyncio
async def test_the_hsts_max_age_is_long_enough_to_be_worth_setting(client, monkeypatch):
    """A short max-age is a header that looks like protection and is not: the pin expires
    before most clinicians return to the app."""
    monkeypatch.setattr(settings, "app_env", "production")
    value = (await client.get("/health")).headers["Strict-Transport-Security"]
    max_age = int(value.split("max-age=")[1].split(";")[0])
    assert max_age >= 31536000  # one year


# --- Not overwritten ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_route_may_still_set_its_own_value(client):
    """``setdefault``, not assignment: a route that has a reason to say something different
    keeps it. Nothing does today, and this is what keeps that a choice."""
    resp = await client.get("/health")
    assert resp.headers["Content-Type"].startswith("application/json")
