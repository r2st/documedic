"""Health probes, security headers, and the application-wide error handlers.

The unhandled-exception handler matters for privacy as much as for UX: an exception raised
deep in the stack can be carrying patient data in its message, and Starlette's default 500
path would surface it. Nothing internal may reach the response body.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter

from app.config import settings
from app.exceptions import ValidationError
from app.middleware import HSTS_VALUE, SECURITY_HEADERS

# --- Health probes ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_env_and_llm_mode(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["env"] == settings.app_env
    assert body["llm_mode"] in {"live", "demo", "offline"}


@pytest.mark.asyncio
async def test_liveness_probe_needs_no_database(client):
    assert (await client.get("/health/live")).json() == {"status": "alive"}


@pytest.mark.asyncio
async def test_readiness_probe_checks_the_database(client):
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_dependencies_probe_lists_provider_state(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    body = (await auth_client.get("/health/dependencies")).json()
    assert body["database"] == "ok"
    assert body["llm_provider"] == "openrouter"
    assert body["llm_openrouter_configured"] is True
    assert body["llm_available_providers"][0] == "openrouter"
    assert body["llm_mode"] == "live"


@pytest.mark.asyncio
async def test_dependencies_probe_reports_offline_without_any_key(auth_client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_demo_fallback", False)

    body = (await auth_client.get("/health/dependencies")).json()
    assert body["llm_available_providers"] == []
    assert body["llm_mode"] == "offline"
    assert body["llm_simulated"] is False


@pytest.mark.asyncio
async def test_dependencies_probe_does_not_answer_anonymous_callers(client):
    """The operator view is authenticated; the probes the load balancer calls are not.

    `nginx.conf` proxies `location /health` as a prefix, so every path under it is reachable
    from the public internet. The three thin probes are meant to be. This one answers with the
    deployment's vendor inventory — which LLM providers hold keys, which is primary, the
    OpenRouter fallback topology, the storage backend — and must not.
    """
    assert (await client.get("/health/dependencies")).status_code == 401

    # The probes an unauthenticated load balancer depends on keep working.
    for path in ("/health", "/health/live", "/health/ready"):
        assert (await client.get(path)).status_code == 200, path


@pytest.mark.asyncio
async def test_health_marks_simulated_output_when_the_demo_net_is_serving(client, monkeypatch):
    """Clinical output built from sample data must never look like live reasoning."""
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_demo_fallback", True)

    body = (await client.get("/health")).json()
    assert body["llm_mode"] == "demo"
    assert body["llm_simulated"] is True


@pytest.mark.asyncio
async def test_health_probes_do_not_require_authentication(client):
    """Kubernetes/Caddy probe these without credentials."""
    for path in ("/health", "/health/live", "/health/ready"):
        assert (await client.get(path)).status_code == 200


# --- Middleware ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_security_headers_are_present_on_every_response(client):
    resp = await client.get("/health")
    for header, value in SECURITY_HEADERS.items():
        assert resp.headers[header] == value


@pytest.mark.asyncio
async def test_request_id_is_echoed_when_supplied(client):
    resp = await client.get("/health", headers={"X-Request-Id": "trace-abc-123"})
    assert resp.headers["X-Request-Id"] == "trace-abc-123"


@pytest.mark.asyncio
async def test_request_id_is_generated_when_absent(client):
    resp = await client.get("/health")
    assert len(resp.headers["X-Request-Id"]) == 32


@pytest.mark.asyncio
async def test_response_time_header_is_reported(client):
    resp = await client.get("/health")
    assert float(resp.headers["X-Response-Time-ms"]) >= 0


@pytest.mark.asyncio
async def test_hsts_only_in_production(client, monkeypatch):
    assert "Strict-Transport-Security" not in (await client.get("/health")).headers
    monkeypatch.setattr(settings, "app_env", "production")
    assert "Strict-Transport-Security" in (await client.get("/health")).headers


# --- Error handlers --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_domain_errors_render_as_code_and_message(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients", json={"full_name": "No Consent", "consent_given": False}
    )
    assert resp.status_code == 422
    assert resp.json() == {
        "code": "consent_required",
        "message": resp.json()["message"],
    }


@pytest.mark.asyncio
async def test_a_non_uuid_path_parameter_becomes_a_404(auth_client):
    """A client asking for /patients/3 wants 'no such patient', not a schema complaint."""
    resp = await auth_client.get("/api/v1/patients/3")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_body_validation_errors_stay_422(auth_client):
    resp = await auth_client.post("/api/v1/patients", json={"sex": "male"})
    assert resp.status_code == 422
    assert "detail" in resp.json()


@pytest.mark.asyncio
async def test_a_custom_validator_error_is_serialisable(auth_client):
    """A field_validator raising a bare ValueError puts a non-JSON-serialisable exception
    into error['ctx']['error']; the handler must coerce it rather than 500."""
    resp = await auth_client.post(
        "/api/v1/patients",
        json={"full_name": "Future Born", "date_of_birth": "2199-01-01", "consent_given": True},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]


@pytest.fixture
def boom_client(app, client):
    """``client``, with a route mounted at /api/v1/_test_boom that raises.

    The exception message stands in for one carrying patient data — a DB constraint error
    quoting the offending row is the realistic case — so every test below can assert that
    none of it escaped.
    """

    router = APIRouter()

    @router.get("/api/v1/_test_boom")
    async def boom():
        raise RuntimeError("patient Ramesh Kumar failed constraint uq_patients_phone")

    app.include_router(router)
    return client


@pytest.mark.asyncio
async def test_unhandled_exception_handler_returns_a_generic_500(boom_client):
    resp = await boom_client.get("/api/v1/_test_boom")

    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "internal_error"
    assert "Ramesh" not in resp.text
    assert "uq_patients_phone" not in resp.text
    # Actionable without leaking: says nothing was written (get_db rolled the request back), and
    # hands over the request id so the clinician's report can be tied to the logged traceback.
    assert "nothing was saved" in body["message"]
    assert body["request_id"] == resp.headers["X-Request-Id"]
    assert body["request_id"] in body["message"]


@pytest.mark.asyncio
async def test_a_500_still_carries_the_security_headers(boom_client):
    """A server error is not an excuse to ship a response without ``nosniff``.

    It regressed for a structural reason rather than an oversight: Starlette installs
    ``ServerErrorMiddleware`` outside every user middleware, so a 500 built there never
    passed back through ``RequestContextMiddleware`` and picked up none of its headers. The
    error body is still a body a browser will content-sniff and still a page an attacker
    would like to frame.
    """
    resp = await boom_client.get("/api/v1/_test_boom")

    assert resp.status_code == 500
    for header, value in SECURITY_HEADERS.items():
        assert resp.headers[header] == value, header


@pytest.mark.asyncio
async def test_a_500_carries_hsts_in_production(boom_client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    resp = await boom_client.get("/api/v1/_test_boom")

    assert resp.status_code == 500
    assert resp.headers["Strict-Transport-Security"] == HSTS_VALUE


@pytest.mark.asyncio
async def test_a_500_echoes_a_supplied_request_id(boom_client):
    """The reference in the body is the one the caller's own tracing already knows."""
    resp = await boom_client.get("/api/v1/_test_boom", headers={"X-Request-Id": "trace-boom-1"})

    assert resp.headers["X-Request-Id"] == "trace-boom-1"
    assert resp.json()["request_id"] == "trace-boom-1"
    assert "trace-boom-1" in resp.json()["message"]


@pytest.mark.asyncio
async def test_a_500_is_readable_from_the_dashboard_origin(boom_client):
    """Without ``Access-Control-Allow-Origin`` the 500 body may as well not exist.

    ``CORSMiddleware`` is also inside ``ServerErrorMiddleware``, so a 500 produced there was
    unreadable cross-origin: the dashboard's ``fetch`` rejected before it could parse
    ``{code, message, request_id}``, and the clinician was told the server could not be
    reached rather than being handed the reference id the body exists to give them.
    """
    origin = settings.cors_origin_list[0]
    resp = await boom_client.get("/api/v1/_test_boom", headers={"Origin": origin})

    assert resp.status_code == 500
    assert resp.headers["access-control-allow-origin"] == origin
    # The frontend reads the correlation id off the header too, which needs it exposed.
    assert "X-Request-Id" in resp.headers["access-control-expose-headers"]


@pytest.mark.asyncio
async def test_a_cancelled_request_is_not_reported_as_a_server_error():
    """``CancelledError`` must pass straight through the middleware's ``except Exception``.

    A clinician navigating away mid-request cancels the ASGI task, which surfaces as
    ``CancelledError`` out of ``call_next``. Swallowing that into a 500 would log a server
    error for every abandoned page load and bury the real ones among them.

    Driven through ``dispatch`` directly rather than a route, because a route that *raises*
    ``CancelledError`` is a different thing: ``BaseHTTPMiddleware`` turns that into
    ``RuntimeError("No response returned.")`` before this middleware ever sees it, and it
    genuinely is a 500.
    """
    import asyncio

    from starlette.requests import Request

    from app.middleware import RequestContextMiddleware

    middleware = RequestContextMiddleware(app=None)  # type: ignore[arg-type]
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    async def cancelled_call_next(_request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await middleware.dispatch(request, cancelled_call_next)


@pytest.mark.asyncio
async def test_the_outermost_backstop_handler_matches_the_middleware_contract(app):
    """The handler in ``main`` covers what the middleware cannot see (a CORSMiddleware
    failure, or one in dispatch itself). It is unreachable through a normal request, so it is
    called directly — what matters is that it answers in the same shape rather than dropping
    to Starlette's plain-text 500."""
    from starlette.requests import Request

    handler = app.exception_handlers[Exception]
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.request_id = "trace-backstop-1"

    try:
        raise RuntimeError("patient Ramesh Kumar failed constraint uq_patients_phone")
    except RuntimeError as exc:
        response = await handler(request, exc)

    assert response.status_code == 500
    assert b"Ramesh" not in response.body
    assert response.headers["X-Request-Id"] == "trace-backstop-1"
    for header, value in SECURITY_HEADERS.items():
        assert response.headers[header] == value, header


def test_domain_exceptions_carry_their_own_status_and_code():
    err = ValidationError("bad input")
    assert err.status_code == 422
    assert err.code == "validation_error"
    assert err.message == "bad input"


def test_domain_exceptions_fall_back_to_their_docstring():
    from app.exceptions import TooManyAttemptsError

    assert "Too many failed attempts" in TooManyAttemptsError().message
    assert TooManyAttemptsError().status_code == 429
