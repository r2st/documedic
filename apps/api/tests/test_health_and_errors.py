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
from app.middleware import SECURITY_HEADERS

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
async def test_dependencies_probe_lists_provider_state(client, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    body = (await client.get("/health/dependencies")).json()
    assert body["database"] == "ok"
    assert body["llm_provider"] == "openrouter"
    assert body["llm_openrouter_configured"] is True
    assert body["llm_available_providers"][0] == "openrouter"
    assert body["llm_mode"] == "live"


@pytest.mark.asyncio
async def test_dependencies_probe_reports_offline_without_any_key(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_demo_fallback", False)

    body = (await client.get("/health/dependencies")).json()
    assert body["llm_available_providers"] == []
    assert body["llm_mode"] == "offline"
    assert body["llm_simulated"] is False


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


@pytest.mark.asyncio
async def test_unhandled_exception_handler_returns_a_generic_500(app):
    """The exception message here stands in for one carrying patient data — a DB constraint
    error, say. None of it may reach the response body."""
    from starlette.testclient import TestClient

    router = APIRouter()

    @router.get("/api/v1/_test_boom")
    async def boom():
        raise RuntimeError("patient Ramesh Kumar failed constraint uq_patients_phone")

    app.include_router(router)

    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.get("/api/v1/_test_boom")

    assert resp.status_code == 500
    body = resp.json()
    assert body == {
        "code": "internal_error",
        "message": "An unexpected error occurred. Please try again.",
    }
    assert "Ramesh" not in resp.text
    assert "uq_patients_phone" not in resp.text


def test_domain_exceptions_carry_their_own_status_and_code():
    err = ValidationError("bad input")
    assert err.status_code == 422
    assert err.code == "validation_error"
    assert err.message == "bad input"


def test_domain_exceptions_fall_back_to_their_docstring():
    from app.exceptions import TooManyAttemptsError

    assert "Too many failed attempts" in TooManyAttemptsError().message
    assert TooManyAttemptsError().status_code == 429
