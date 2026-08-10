"""Request-input bounds.

Every user-controlled string that reaches a store, a retriever, or an LLM prompt needs an
upper bound, so an oversized request is rejected at the edge (422) instead of turning into
unbounded work downstream.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import create_patient


@pytest.mark.asyncio
async def test_guideline_query_is_length_bounded(auth_client):
    resp = await auth_client.get("/api/v1/guidelines/search", params={"q": "x" * 501})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_guideline_query_at_the_limit_is_accepted(auth_client):
    resp = await auth_client.get("/api/v1/guidelines/search", params={"q": "x" * 500})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_guideline_query_below_the_minimum_is_rejected(auth_client):
    resp = await auth_client.get("/api/v1/guidelines/search", params={"q": "x"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_guideline_result_count_is_capped(auth_client):
    assert (
        await auth_client.get("/api/v1/guidelines/search", params={"q": "dengue", "k": 26})
    ).status_code == 422
    assert (
        await auth_client.get("/api/v1/guidelines/search", params={"q": "dengue", "k": 0})
    ).status_code == 422


@pytest.mark.asyncio
async def test_audit_action_filter_is_length_bounded(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/audit", params={"action": "a" * 101}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patient_search_term_is_length_bounded(auth_client):
    resp = await auth_client.post("/api/v1/patients/search", json={"search": "a" * 201})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_pagination_bounds_are_enforced(auth_client):
    assert (await auth_client.get("/api/v1/patients", params={"limit": 101})).status_code == 422
    assert (await auth_client.get("/api/v1/patients", params={"limit": 0})).status_code == 422
    assert (await auth_client.get("/api/v1/patients", params={"offset": -1})).status_code == 422


@pytest.mark.asyncio
async def test_search_pagination_bounds_are_enforced(auth_client):
    """The body-based search must keep the same bounds the query params had."""
    for body in ({"limit": 101}, {"limit": 0}, {"offset": -1}):
        resp = await auth_client.post("/api/v1/patients/search", json=body)
        assert resp.status_code == 422, body


@pytest.mark.asyncio
async def test_presenting_complaint_is_length_bounded(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "x" * 4001},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_intake_answer_batch_is_bounded(auth_client):
    resp = await auth_client.post(
        f"/api/v1/reasoning/{uuid.uuid4()}/intake/answers",
        json={
            "answers": [{"question_id": str(uuid.uuid4()), "answer_text": "yes"} for _ in range(51)]
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patient_free_text_fields_are_length_bounded(auth_client):
    base = {"full_name": "Bounds Test", "sex": "male", "consent_given": True}
    for field, over in (("notes", 10001), ("address_text", 2001), ("phone", 21)):
        resp = await auth_client.post("/api/v1/patients", json={**base, field: "x" * over})
        assert resp.status_code == 422, f"{field} accepted {over} characters"


@pytest.mark.asyncio
async def test_an_empty_patient_name_is_rejected(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients",
        json={"full_name": "", "sex": "male", "consent_given": True},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_the_dossier_format_parameter_is_allowlisted(auth_client):
    resp = await auth_client.get(
        "/api/v1/regulatory/samd-dossier", params={"format": "../../etc/passwd"}
    )
    assert resp.status_code == 422
