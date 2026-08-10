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

    search = await auth_client.post("/api/v1/patients/search", json={"search": "Arjun"})
    items = search.json()["items"]
    assert len(items) == 1 and items[0]["full_name"] == "Arjun Mehta"

    by_phone = await auth_client.post("/api/v1/patients/search", json={"search": "8881112222"})
    assert by_phone.json()["pagination"]["total"] == 1


@pytest.mark.asyncio
async def test_search_term_never_travels_in_the_url(auth_client):
    """The whole point of the POST: the identifier must not be in the request line.

    A name in a query string is written verbatim into nginx access logs, load-balancer
    logs and browser history -- undoing the at-rest encryption on patients.full_name.
    """
    await create_patient(auth_client, full_name="Arjun Mehta", phone="9990001111")

    resp = await auth_client.post("/api/v1/patients/search", json={"search": "Arjun Mehta"})

    assert resp.status_code == 200
    assert resp.json()["pagination"]["total"] == 1
    assert "Arjun" not in str(resp.request.url)
    assert resp.request.url.query in (b"", None)


@pytest.mark.asyncio
async def test_list_rejects_the_legacy_search_query_parameter(auth_client):
    """Fail loudly, not open.

    Undeclared query params are dropped silently by FastAPI, so a stale client would have
    received an unfiltered page and looked fine -- after logging the name.
    """
    await create_patient(auth_client, full_name="Arjun Mehta")
    await create_patient(auth_client, full_name="Priya Nair")

    resp = await auth_client.get("/api/v1/patients", params={"search": "Arjun"})

    assert resp.status_code == 400
    assert resp.json()["code"] == "unsupported_query_parameter"
    assert "POST /patients/search" in resp.json()["message"]


@pytest.mark.asyncio
async def test_list_rejects_an_empty_search_query_parameter(auth_client):
    """``?search=`` is still a name-shaped parameter in the log line; reject it too."""
    resp = await auth_client.get("/api/v1/patients?search=")
    assert resp.status_code == 400
    assert resp.json()["code"] == "unsupported_query_parameter"


@pytest.mark.asyncio
async def test_search_paginates_and_scopes_to_the_caller(auth_client, second_auth_client):
    for i in range(5):
        await create_patient(auth_client, full_name=f"Ramesh Kumar {i}", phone=f"999000111{i}")
    await create_patient(second_auth_client, full_name="Ramesh Kumar X")

    first = await auth_client.post(
        "/api/v1/patients/search", json={"search": "Ramesh", "limit": 2, "offset": 0}
    )
    assert first.json()["pagination"] == {
        "total": 5,
        "limit": 2,
        "offset": 0,
        "has_more": True,
    }

    last = await auth_client.post(
        "/api/v1/patients/search", json={"search": "Ramesh", "limit": 2, "offset": 4}
    )
    assert len(last.json()["items"]) == 1
    assert last.json()["pagination"]["has_more"] is False

    # The other account's identically-named patient is invisible here, as everywhere else.
    other = await second_auth_client.post("/api/v1/patients/search", json={"search": "Ramesh"})
    assert other.json()["pagination"]["total"] == 1


@pytest.mark.asyncio
async def test_search_without_a_term_lists_everything(auth_client):
    await create_patient(auth_client, full_name="Arjun Mehta")
    await create_patient(auth_client, full_name="Priya Nair")

    resp = await auth_client.post("/api/v1/patients/search", json={})

    assert resp.json()["pagination"]["total"] == 2


@pytest.mark.asyncio
async def test_search_requires_authentication(client):
    resp = await client.post("/api/v1/patients/search", json={"search": "Arjun"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_search_rejects_an_oversized_term(auth_client):
    resp = await auth_client.post("/api/v1/patients/search", json={"search": "x" * 201})
    assert resp.status_code == 422
    # The rejected value is a patient name; it must not come back (see _safe_validation_errors).
    assert "x" * 201 not in resp.text


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
