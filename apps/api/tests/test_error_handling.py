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
    resp = await auth_client.post(f"/api/v1/patients/{patient['id']}/drug-safety/check", json={})
    assert resp.status_code == 422


# --------------------------------------------------------- 422 bodies must not echo input

# A patient payload where every field is independently recognisable in a response body.
_SENSITIVE_PATIENT = {
    "full_name": "Ramesh Kumar",
    "phone": "+91-9876543210",
    "address_text": "12 MG Road, Bengaluru 560001",
    "notes": "HIV positive, on ART since 2019",
    "date_of_birth": "1968-05-10",
    "consent_given": True,
}
_SENSITIVE_VALUES = (
    "Ramesh Kumar",
    "9876543210",
    "MG Road",
    "Bengaluru",
    "HIV positive",
    "1968-05-10",
)


@pytest.mark.asyncio
async def test_missing_field_422_does_not_echo_the_submitted_body(auth_client):
    """The failure mode this guards is specific to ``missing`` errors.

    Pydantic reports the value it rejected in ``error["input"]``. For a field-level failure
    that is one field; for a missing required field it is the *whole* submitted body. FastAPI's
    default handler returns that verbatim, so omitting ``full_name`` used to hand back the
    patient's phone, address and free-text clinical notes in the 422 -- the same columns that
    are encrypted at rest because they are DPDP-sensitive.
    """
    body = {k: v for k, v in _SENSITIVE_PATIENT.items() if k != "full_name"}
    resp = await auth_client.post("/api/v1/patients", json=body)

    assert resp.status_code == 422
    serialised = resp.text
    for secret in _SENSITIVE_VALUES:
        if secret == "Ramesh Kumar":  # omitted from this request
            continue
        assert secret not in serialised, f"{secret!r} echoed back in: {serialised}"


@pytest.mark.asyncio
async def test_field_level_422_does_not_echo_the_rejected_value(auth_client):
    """A bad value for one field must not come back either -- the value may itself be the PII."""
    body = dict(_SENSITIVE_PATIENT, sex="NOT_A_VALID_SEX", phone="not a phone number")
    resp = await auth_client.post("/api/v1/patients", json=body)

    assert resp.status_code == 422
    assert "not a phone number" not in resp.text, resp.text
    for secret in _SENSITIVE_VALUES:
        assert secret not in resp.text, f"{secret!r} echoed back in: {resp.text}"


@pytest.mark.asyncio
async def test_422_still_names_the_offending_field_and_the_constraint(auth_client):
    """Stripping the input must not make the error useless: loc and msg have to survive.

    These are what a client acts on -- which field failed and why -- and neither carries the
    submitted value.
    """
    body = {k: v for k, v in _SENSITIVE_PATIENT.items() if k != "full_name"}
    resp = await auth_client.post("/api/v1/patients", json=body)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail
    error = next(e for e in detail if e["loc"][-1] == "full_name")
    assert error["type"] == "missing"
    assert error["loc"] == ["body", "full_name"]
    assert error["msg"]
    # The dropped keys are gone entirely, not merely emptied.
    assert "input" not in error
    assert "ctx" not in error


@pytest.mark.asyncio
async def test_custom_validator_422_reports_its_message_without_the_value(auth_client):
    """A bare ValueError from a field_validator is the path that used to need ctx["error"].

    ctx is dropped now, so this pins that the validator's own message still reaches the client
    through ``msg`` -- and that the rejected date does not.
    """
    resp = await auth_client.post(
        "/api/v1/patients",
        json={"full_name": "Test Patient", "consent_given": True, "date_of_birth": "2999-01-01"},
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    error = next(e for e in detail if e["loc"][-1] == "date_of_birth")
    assert "cannot be in the future" in error["msg"]
    assert "2999-01-01" not in resp.text, resp.text


@pytest.mark.asyncio
async def test_search_body_422_does_not_echo_the_search_term(auth_client):
    """Patient search terms are patient names; a rejected one must not come back in the body."""
    resp = await auth_client.post("/api/v1/patients/search", json={"search": "Ramesh Kumar" * 40})

    assert resp.status_code == 422
    assert "Ramesh Kumar" not in resp.text, resp.text


@pytest.mark.asyncio
async def test_rejecting_a_legacy_search_query_param_does_not_echo_it(auth_client):
    """The 400 that retires ``?search=`` must not repeat the term it is rejecting.

    The term is already in the access log by the time we see it — that is the whole reason
    for the migration — but the response body reaches error trackers and browser consoles
    that the log line does not, so it must stay clean.
    """
    resp = await auth_client.get("/api/v1/patients", params={"search": "Ramesh Kumar"})

    assert resp.status_code == 400
    assert "Ramesh" not in resp.text, resp.text
