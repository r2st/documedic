"""Patient API integration tests."""

from __future__ import annotations

import pytest

from tests.conftest import create_patient


@pytest.mark.asyncio
async def test_create_requires_consent(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients",
        json={"full_name": "No Consent", "consent_given": False},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "consent_required"


@pytest.mark.asyncio
async def test_create_rejects_future_date_of_birth(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients",
        json={"full_name": "Future Person", "date_of_birth": "2099-01-01", "consent_given": True},
    )
    assert resp.status_code == 422
    # The 422 body itself must be well-formed JSON with a readable message, not a 500 from a
    # non-serializable ValueError leaking into the response (see main.py's
    # validation_error_handler -- a bare `raise ValueError(...)` in a field_validator is the
    # standard Pydantic v2 idiom and must not crash the handler).
    body = resp.json()
    assert "cannot be in the future" in str(body)


@pytest.mark.asyncio
async def test_create_rejects_implausible_date_of_birth(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients",
        json={"full_name": "Ancient Person", "date_of_birth": "1850-01-01", "consent_given": True},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_malformed_phone(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients",
        json={"full_name": "Bad Phone", "phone": "call-me-maybe!!", "consent_given": True},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_update_rejects_future_date_of_birth(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.patch(
        f"/api/v1/patients/{patient['id']}", json={"date_of_birth": "2099-01-01"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_and_get(auth_client):
    patient = await create_patient(auth_client, full_name="Sita Devi", sex="female")
    assert patient["consent_given"] is True
    assert patient["consent_given_at"] is not None

    got = await auth_client.get(f"/api/v1/patients/{patient['id']}")
    assert got.status_code == 200
    assert got.json()["full_name"] == "Sita Devi"


@pytest.mark.asyncio
async def test_list_and_search(auth_client):
    await create_patient(auth_client, full_name="Arjun Mehta", phone="9990001111")
    await create_patient(auth_client, full_name="Priya Nair", phone="8881112222")

    resp = await auth_client.get("/api/v1/patients")
    assert resp.json()["pagination"]["total"] == 2

    search = await auth_client.get("/api/v1/patients", params={"search": "Arjun"})
    items = search.json()["items"]
    assert len(items) == 1 and items[0]["full_name"] == "Arjun Mehta"

    by_phone = await auth_client.get("/api/v1/patients", params={"search": "8881112222"})
    assert by_phone.json()["pagination"]["total"] == 1


@pytest.mark.asyncio
async def test_update(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.patch(
        f"/api/v1/patients/{patient['id']}", json={"notes": "Diabetic, on metformin"}
    )
    assert resp.status_code == 200
    assert resp.json()["notes"] == "Diabetic, on metformin"


@pytest.mark.asyncio
async def test_soft_delete_hides_patient(auth_client):
    patient = await create_patient(auth_client)
    await auth_client.delete(f"/api/v1/patients/{patient['id']}")
    got = await auth_client.get(f"/api/v1/patients/{patient['id']}")
    assert got.status_code == 404


@pytest.mark.asyncio
async def test_cannot_access_other_accounts_patient(auth_client, client):
    patient = await create_patient(auth_client)
    # A different account.
    other = await client.post(
        "/api/v1/auth/signup", json={"email": "other@b.com", "password": "password123"}
    )
    token = other.json()["access_token"]
    resp = await client.get(
        f"/api/v1/patients/{patient['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 404
