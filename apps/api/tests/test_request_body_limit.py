"""The ceiling on any request body at all, refused before the application reads it.

The upload route streams its file and aborts one chunk past 20 MB, so that body has been
bounded for a while. Every other route buffers: FastAPI reads the whole body before Pydantic is
handed anything to validate, so the cost of a body is paid in full before the first rule that
could reject it runs. A 64 MB JSON body to ``POST /auth/login`` was read into memory and then
answered 422 — unauthenticated, so no per-account ceiling applies, and cheap to repeat.

``client_max_body_size 25m`` in the reference ``nginx.conf`` covered exactly the deployments
that run nginx in front and leave the container port unreachable. That is a property of a
deployment, not of this application.
"""

from __future__ import annotations

import pytest

from app.config import settings
from tests.conftest import create_patient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512


def _json_body(size: int) -> str:
    """A syntactically valid login body padded out to roughly ``size`` bytes."""
    return '{"email":"a@b.com","password":"' + "x" * size + '"}'


# --- Refused on the envelope ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_with_413(client, monkeypatch):
    """The regression. Unauthenticated, and previously buffered whole before the 422."""
    monkeypatch.setattr(settings, "max_request_bytes", 4096)
    resp = await client.post(
        "/api/v1/auth/login",
        content=_json_body(16384),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "request_too_large"


@pytest.mark.asyncio
async def test_the_body_is_never_read(client, monkeypatch):
    """What the fix is *for*. A declared length is refused without the bytes being consumed,
    so an oversized body costs one response rather than its own size in memory."""
    monkeypatch.setattr(settings, "max_request_bytes", 4096)
    seen = 0

    async def counting_body():
        nonlocal seen
        for _ in range(8):
            chunk = b"x" * 4096
            seen += len(chunk)
            yield chunk

    resp = await client.post(
        "/api/v1/auth/login",
        content=counting_body(),
        headers={"content-type": "application/json", "content-length": "32768"},
    )
    assert resp.status_code == 413
    assert seen == 0, f"{seen}B of the body was read before it was refused"


@pytest.mark.asyncio
async def test_a_chunked_body_is_aborted_mid_stream(client, monkeypatch):
    """A client that sends no Content-Length has declared nothing to check, so the bytes are
    counted as they arrive and refused the moment they pass the ceiling — the same shape the
    upload route uses. Without this the header check is trivially sidestepped."""
    monkeypatch.setattr(settings, "max_request_bytes", 4096)

    async def chunked():
        for _ in range(16):
            yield b"x" * 4096

    resp = await client.post(
        "/api/v1/auth/login",
        content=chunked(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "request_too_large"


# --- What must keep working ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ordinary_request_is_untouched(client):
    """The ceiling is orders of magnitude above every non-upload route. A normal sign-in must
    not notice it exists."""
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "bodylimit@example.com", "password": "password123"},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_a_document_upload_still_fits(auth_client):
    """The ceiling sits above max_upload_bytes on purpose: a 20 MB document arrives wrapped in
    multipart framing and must still reach the route that enforces its own 20 MB rule. A
    ceiling that clipped uploads would be a worse bug than the one it fixes."""
    assert settings.max_request_bytes > settings.max_upload_bytes
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("scan.png", PNG, "image/png")},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_the_refusal_carries_the_usual_response_furniture(client, monkeypatch):
    """413 is refused before the app runs, so it would be easy to ship it outside the
    middleware that puts a request id and the security headers on every other response. It is
    wired innermost precisely so it does not."""
    monkeypatch.setattr(settings, "max_request_bytes", 4096)
    resp = await client.post(
        "/api/v1/auth/login",
        content=_json_body(16384),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.headers.get("X-Request-Id")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


@pytest.mark.asyncio
async def test_a_get_with_no_body_is_unaffected(client, monkeypatch):
    """Nothing to count, and no Content-Length to read."""
    monkeypatch.setattr(settings, "max_request_bytes", 16)
    assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_a_malformed_content_length_is_not_this_middleware_s_error(client, monkeypatch):
    """An unparseable header is the server's to reject on its own terms. This must fall
    through to the counting path rather than raising out of the middleware."""
    monkeypatch.setattr(settings, "max_request_bytes", 4096)
    resp = await client.post(
        "/api/v1/auth/login",
        content=_json_body(64),
        headers={"content-type": "application/json", "content-length": "not-a-number"},
    )
    assert resp.status_code != 500
