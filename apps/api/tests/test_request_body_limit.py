"""What a request body may cost before the application reads a byte of it.

Three ceilings, all applied on the envelope by ``RequestBodyLimitMiddleware``:

* ``max_request_bytes`` (24 MB) on the one route that carries a scan.
* ``max_json_request_bytes`` (1 MB) on every other route. A single ceiling sized for a 20 MB
  upload is not a ceiling for ``POST /auth/login`` — 24 MB of JSON was buffered whole and
  parsed before the first Pydantic rule could answer 422, unauthenticated, with nothing in
  front of it and nothing to stop it being repeated.
* ``max_json_depth`` (32) on JSON bodies. The only thing bounding ``[[[[…`` was CPython's
  recursion limit, which surfaced as FastAPI's flat 400 with no ``code`` to match on.

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
    monkeypatch.setattr(settings, "max_json_request_bytes", 4096)
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
    monkeypatch.setattr(settings, "max_json_request_bytes", 4096)
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
    monkeypatch.setattr(settings, "max_json_request_bytes", 4096)

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


# --- The two ceilings are not the same ceiling ---------------------------------------------


@pytest.mark.asyncio
async def test_a_json_route_does_not_get_the_upload_ceiling(client):
    """The regression this split exists for. `POST /auth/login` used to be allowed 24 MB
    because the upload route needs 24 MB, so the sign-in route — unauthenticated, and the one
    with no per-account limit in front of it — inherited a ceiling sized for a scan."""
    assert settings.max_json_request_bytes < settings.max_request_bytes
    resp = await client.post(
        "/api/v1/auth/login",
        content=_json_body(settings.max_json_request_bytes + 1024),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "request_too_large"


@pytest.mark.asyncio
async def test_the_upload_route_keeps_the_large_ceiling(auth_client, monkeypatch):
    """...and the split must not clip the route it was sized for. A body between the two
    ceilings reaches the upload handler and is judged on its own 20 MB rule, not refused on
    the envelope."""
    monkeypatch.setattr(settings, "max_json_request_bytes", 1024)
    patient = await create_patient(auth_client)
    big_png = PNG + b"\x00" * (64 * 1024)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("scan.png", big_png, "image/png")},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_an_unmatched_address_gets_the_small_ceiling(client, monkeypatch):
    """A path that resolves to no route is not an upload route. The middleware runs outside
    the router and matches on the raw path, so the safe direction is the small ceiling: an
    unmatched address answers 404 and never needed a large body."""
    monkeypatch.setattr(settings, "max_json_request_bytes", 4096)
    resp = await client.post(
        "/api/v1/patients/not-a-uuid/documents/extra/segments",
        content=_json_body(16384),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413


# --- Nesting depth --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_deeply_nested_body_is_refused_with_a_matchable_code(client):
    """Previously this reached ``json.loads``, exhausted CPython's recursion limit, and came
    back as FastAPI's flat 400 — indistinguishable from a syntax error and carrying no
    ``code``. It is now refused while the body is still arriving."""
    depth = settings.max_json_depth + 10
    resp = await client.post(
        "/api/v1/auth/login",
        content="[" * depth + "]" * depth,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "request_too_nested"


@pytest.mark.asyncio
async def test_depth_is_refused_well_below_the_interpreter_s_own_limit(client):
    """The point of an explicit guard: the refusal happens at this application's stated bound,
    not wherever the interpreter happens to run out of stack."""
    import sys

    assert settings.max_json_depth < sys.getrecursionlimit() / 10
    depth = 100_000
    resp = await client.post(
        "/api/v1/auth/login",
        content="[" * depth,
        headers={"content-type": "application/json"},
    )
    assert resp.json()["code"] == "request_too_nested"


@pytest.mark.asyncio
async def test_brackets_inside_a_string_are_text_not_depth(auth_client):
    """The scanner skips string literals, so a clinical note that happens to contain brackets
    is prose. Getting this wrong would refuse real charts."""
    brackets = "{[" * (settings.max_json_depth * 4)
    resp = await auth_client.post(
        "/api/v1/patients",
        json={
            "full_name": "Bracket Patient",
            "date_of_birth": "1980-01-01",
            "sex": "male",
            "consent_given": True,
            "notes": f"Dose written as {brackets} on the scan",
        },
    )
    assert resp.status_code == 201, resp.text
    assert brackets in resp.json()["notes"]


@pytest.mark.asyncio
async def test_an_escaped_quote_does_not_end_the_string(client):
    r"""A string ending in ``\"`` stays open. Mishandling the escape would drop the scanner
    back into structure mode mid-string and count the brackets that follow."""
    payload = '{"email":"a\\"' + "[" * (settings.max_json_depth * 2) + '","password":"x"}'
    resp = await client.post(
        "/api/v1/auth/login", content=payload, headers={"content-type": "application/json"}
    )
    assert resp.status_code != 400, resp.text


@pytest.mark.asyncio
async def test_unbalanced_closers_cannot_buy_extra_depth(client):
    """Depth is clamped at zero on the way down. Without that, a run of ``]`` drives the
    counter negative and the nesting that follows starts from a discount."""
    limit = settings.max_json_depth
    resp = await client.post(
        "/api/v1/auth/login",
        content="]" * 1000 + "[" * (limit + 10),
        headers={"content-type": "application/json"},
    )
    assert resp.json()["code"] == "request_too_nested"


@pytest.mark.asyncio
async def test_a_non_json_body_is_not_depth_scanned(auth_client, monkeypatch):
    """A multipart upload is bytes, not a document. Scanning it would refuse any scan whose
    binary content happened to hold 32 unmatched ``[`` bytes — which for a PDF is routine."""
    monkeypatch.setattr(settings, "max_json_depth", 4)
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("scan.png", PNG + b"[" * 500, "image/png")},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_a_body_at_the_depth_limit_is_accepted(client, monkeypatch):
    """The bound is inclusive. Off by one here would refuse a legitimate body."""
    monkeypatch.setattr(settings, "max_json_depth", 8)
    resp = await client.post(
        "/api/v1/auth/login",
        content="[" * 8 + "]" * 8,
        headers={"content-type": "application/json"},
    )
    # 422 (not a login body) rather than 400 — it was parsed, which is the point.
    assert resp.status_code == 422


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
async def test_the_largest_legitimate_json_body_fits(auth_client):
    """The 1 MB ceiling is not arbitrary: the widest non-upload body in the API is an
    extraction approval carrying its maximum 500 corrections. It must fit with room to spare,
    or the ceiling is a bug rather than a bound."""
    body = {
        "corrections": [
            {"entity_index": 0, "field_name": "f" * 200, "value": "v" * 500} for _ in range(500)
        ],
        "rejected_entity_indexes": list(range(500)),
    }
    import json

    assert len(json.dumps(body).encode()) < settings.max_json_request_bytes
    patient = await create_patient(auth_client)
    # Unknown document -> 404 from the handler, which is proof the body reached it.
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/"
        "00000000-0000-0000-0000-000000000000/extraction/approve",
        json=body,
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_the_refusal_carries_the_usual_response_furniture(client, monkeypatch):
    """413 is refused before the app runs, so it would be easy to ship it outside the
    middleware that puts a request id and the security headers on every other response. It is
    wired innermost precisely so it does not."""
    monkeypatch.setattr(settings, "max_json_request_bytes", 4096)
    resp = await client.post(
        "/api/v1/auth/login",
        content=_json_body(16384),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.headers.get("X-Request-Id")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


@pytest.mark.asyncio
async def test_the_nesting_refusal_carries_it_too(client):
    """Same for the depth refusal, which travels the other of the two refusal paths — from
    inside the receive wrapper rather than before the app is invoked at all."""
    depth = settings.max_json_depth + 10
    resp = await client.post(
        "/api/v1/auth/login",
        content="[" * depth + "]" * depth,
        headers={"content-type": "application/json"},
    )
    assert resp.headers.get("X-Request-Id")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Permissions-Policy")


@pytest.mark.asyncio
async def test_a_get_with_no_body_is_unaffected(client, monkeypatch):
    """Nothing to count, and no Content-Length to read."""
    monkeypatch.setattr(settings, "max_json_request_bytes", 16)
    assert (await client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_a_malformed_content_length_is_not_this_middleware_s_error(client, monkeypatch):
    """An unparseable header is the server's to reject on its own terms. This must fall
    through to the counting path rather than raising out of the middleware."""
    monkeypatch.setattr(settings, "max_json_request_bytes", 4096)
    resp = await client.post(
        "/api/v1/auth/login",
        content=_json_body(64),
        headers={"content-type": "application/json", "content-length": "not-a-number"},
    )
    assert resp.status_code != 500
