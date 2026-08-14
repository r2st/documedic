"""One error contract, enforced across every route in the router table.

The OpenAPI description tells clients to match on ``code`` and treat ``message`` as prose that
gets rewritten. That promise is only worth anything if it holds everywhere, and the places it
had quietly stopped holding were the ones no endpoint test covers: the router's own 404 for an
unmatched address and 405 for a wrong method, both of which answered ``{"detail": "Not Found"}``
before any application code ran.

So these tests enumerate routes from the live app rather than listing them, the same way
``test_route_authz`` does. A new endpoint is covered the moment it is registered, and an error
path that invents its own body shape fails here rather than in a frontend six months later.

The correlation id is the other half. ``X-Request-Id`` is what ties a clinician saying "it just
failed" to a logged traceback, and an error response without one is a report nobody can act on.
"""

from __future__ import annotations

import uuid

import pytest

from tests.test_route_authz import PUBLIC_PATHS, _fill, _routes

# The one documented departure from {code, message}: FastAPI's request-validation failure,
# which returns {detail: [...]} because a client needs the offending field, not one sentence.
# It is called out in the OpenAPI description; everything else must be {code, message}.
VALIDATION_STATUS = 422


def assert_structured_error(resp, where: str) -> None:
    """A 4xx/5xx body is either ``{code, message}`` or the documented 422 ``{detail: [...]}``."""
    body = resp.json()
    if resp.status_code == VALIDATION_STATUS and "detail" in body:
        assert isinstance(body["detail"], list), where
        return
    assert set(body) >= {"code", "message"}, f"{where} -> {body}"
    assert isinstance(body["code"], str) and body["code"], where
    assert isinstance(body["message"], str) and body["message"], where
    # `detail` is the internal cause and is logged, never serialized. Its presence here would
    # mean an error path bypassed aether_error_handler.
    assert "detail" not in body, f"{where} leaked the internal detail channel"


@pytest.mark.asyncio
async def test_every_route_rejects_an_anonymous_caller_in_the_documented_shape(app, client):
    """The sweep that catches a new endpoint answering errors its own way."""
    ctx = {"patient_id": str(uuid.uuid4())}

    for method, template, _ in _routes(app):
        if template in PUBLIC_PATHS:
            continue
        resp = await client.request(method, _fill(template, ctx), json={})
        assert resp.status_code >= 400, f"{method} {template} answered an anonymous caller"
        assert_structured_error(resp, f"{method} {template}")


@pytest.mark.asyncio
async def test_every_error_response_carries_a_correlation_id(app, client):
    """Without ``X-Request-Id`` there is nothing linking the clinician's report to the log."""
    ctx = {"patient_id": str(uuid.uuid4())}

    for method, template, _ in _routes(app):
        if template in PUBLIC_PATHS:
            continue
        resp = await client.request(method, _fill(template, ctx), json={})
        assert resp.headers.get("X-Request-Id"), f"{method} {template} shipped without one"


@pytest.mark.asyncio
async def test_an_error_response_echoes_the_callers_own_correlation_id(auth_client):
    """A caller that brings its own trace id gets it back, so both sides log the same string."""
    resp = await auth_client.get(
        f"/api/v1/patients/{uuid.uuid4()}", headers={"X-Request-Id": "trace-contract-1"}
    )
    assert resp.status_code == 404
    assert resp.headers["X-Request-Id"] == "trace-contract-1"


# --- the router's own errors, which run before any application code -----------------------


@pytest.mark.asyncio
async def test_an_unmatched_address_answers_code_and_message(client):
    """This was ``{"detail": "Not Found"}`` — the one 404 in the API a client could not match."""
    resp = await client.get("/api/v1/no-such-endpoint")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    assert_structured_error(resp, "GET /api/v1/no-such-endpoint")
    assert resp.headers["X-Request-Id"]


@pytest.mark.asyncio
async def test_an_unmatched_address_outside_the_api_prefix_answers_the_same_way(client):
    """`/health` is mounted outside /api/v1, so unmatched paths exist above the prefix too."""
    resp = await client.get("/no-such-endpoint")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_a_wrong_method_answers_code_and_message_and_keeps_allow(client):
    """A 405 without ``Allow`` is not a 405 — the header has to survive the reshaping."""
    resp = await client.put("/api/v1/auth/login", json={})

    assert resp.status_code == 405
    assert resp.json()["code"] == "method_not_allowed"
    assert "POST" in resp.headers["Allow"]


@pytest.mark.asyncio
async def test_the_router_errors_say_nothing_was_changed(client):
    """Both are unreachable-request failures, so the clinician's real question — whether a
    half-written record is now in the chart — is answered rather than left open."""
    assert (
        "nothing was changed" in (await client.put("/api/v1/auth/login", json={})).json()["message"]
    )
    # The 404 says it differently: an address that matched nothing never reached any write, and
    # the useful next step is where to find the record instead.
    assert "patient list" in (await client.get("/api/v1/nope")).json()["message"]


@pytest.mark.asyncio
async def test_the_router_errors_do_not_echo_starlettes_developer_prose(client):
    """``exc.detail`` is "Not Found"/"Method Not Allowed" — diagnostics, not clinician copy."""
    assert "Not Found" not in (await client.get("/api/v1/nope")).text
    assert "Method Not Allowed" not in (await client.put("/api/v1/auth/login", json={})).text


@pytest.mark.asyncio
async def test_the_router_errors_still_carry_the_security_headers(client):
    from app.middleware import SECURITY_HEADERS

    resp = await client.get("/api/v1/nope")
    for header, value in SECURITY_HEADERS.items():
        assert resp.headers[header] == value, header


@pytest.mark.asyncio
async def test_an_unexpected_http_status_falls_back_rather_than_leaking_the_detail(app, client):
    """A dependency introducing an HTTPException status this app has no copy for must still
    answer in shape, and must not pass its own ``detail`` through to the clinician."""
    from fastapi import APIRouter
    from starlette.exceptions import HTTPException as StarletteHTTPException

    extra = APIRouter()

    @extra.get("/api/v1/_test_teapot")
    async def teapot():
        raise StarletteHTTPException(status_code=418, detail="upstream shard 7 is a teapot")

    app.include_router(extra)

    resp = await client.get("/api/v1/_test_teapot")
    assert resp.status_code == 418
    assert resp.json()["code"] == "error"
    assert "shard 7" not in resp.text
    assert_structured_error(resp, "GET /api/v1/_test_teapot")
