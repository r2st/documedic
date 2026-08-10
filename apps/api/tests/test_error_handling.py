"""API error-handling and validation hardening tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_patient


@pytest.mark.asyncio
async def test_unhandled_exception_returns_consistent_json_shape(app):
    """A bare, unexpected exception must not leak its message (which could easily contain
    patient data via a DB error) and must use the same {code, message} shape as every other
    error response, not Starlette's default plain-text 500.

    Uses a raw ASGITransport(raise_app_exceptions=False) client: Starlette's ServerError
    middleware sends our JSON response AND re-raises the original exception afterwards (by
    design, so a real ASGI server still logs it) -- httpx's default transport re-raises that
    into the caller, which is a test-harness artifact only. A real HTTP client never sees the
    Python-level re-raise, only the response bytes already sent, so this is what production
    traffic actually experiences.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as raw_client:
        signup = await raw_client.post(
            "/api/v1/auth/signup",
            json={"email": "err-handling@example.com", "password": "password123"},
        )
        raw_client.headers["Authorization"] = f"Bearer {signup.json()['access_token']}"

        with patch(
            "app.services.patient_service.PatientService.list",
            side_effect=RuntimeError("boom: patient Ramesh Kumar, DOB 1968-05-10"),
        ):
            resp = await raw_client.get("/api/v1/patients")

    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "internal_error"
    assert "boom" not in body["message"]
    assert "Ramesh" not in body["message"]


@pytest.mark.asyncio
async def test_uuid_path_param_404_not_422(auth_client):
    resp = await auth_client.get("/api/v1/patients/not-a-uuid")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_patient_name_too_long_rejected(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients",
        json={"full_name": "x" * 501, "consent_given": True},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patient_future_dob_rejected(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients",
        json={"full_name": "Test Patient", "consent_given": True, "date_of_birth": "2999-01-01"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patient_implausible_age_dob_rejected(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients",
        json={"full_name": "Test Patient", "consent_given": True, "date_of_birth": "1800-01-01"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patients_list_limit_capped(auth_client):
    resp = await auth_client.get("/api/v1/patients", params={"limit": 10000})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patients_list_negative_offset_rejected(auth_client):
    resp = await auth_client.get("/api/v1/patients", params={"offset": -1})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_signup_short_password_rejected(client):
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "short-pw@example.com", "password": "short"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_signup_invalid_email_rejected(client):
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "not-an-email", "password": "password123"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_response_carries_request_id_header(auth_client):
    resp = await auth_client.get("/api/v1/patients")
    assert resp.headers.get("X-Request-Id")


@pytest.mark.asyncio
async def test_drug_safety_check_empty_body_rejected(auth_client):
    """Neither drug_reference_id nor drug_name given -- must be a clean validation error, not
    an unhandled exception further down the resolution pipeline."""
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check", json={}
    )
    assert resp.status_code == 422
