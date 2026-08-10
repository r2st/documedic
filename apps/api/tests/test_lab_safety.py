"""Integration: critical/panic lab values surface via document approval + a dedicated endpoint."""

from __future__ import annotations

import pytest

from tests.conftest import create_patient

CRITICAL_LABS_DOC = (
    b"%PDF-1.4\nLABS:\nPotassium: 7.0 mmol/L (3.5-5.1)\nSodium: 138 mmol/L (135-145)\n"
)


async def _upload_and_approve(client, patient_id, content=CRITICAL_LABS_DOC):
    upload = await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": ("labs.pdf", content, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    doc = upload.json()
    approve = await client.post(
        f"/api/v1/patients/{patient_id}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 200, approve.text
    return doc, approve.json()


@pytest.mark.asyncio
async def test_critical_flags_endpoint_detects_panic_potassium(auth_client):
    patient = await create_patient(auth_client)
    await _upload_and_approve(auth_client, patient["id"])

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/labs/critical-flags")
    assert resp.status_code == 200, resp.text
    flags = resp.json()["flags"]
    assert len(flags) == 1
    assert flags[0]["marker_name"] == "Potassium"
    assert flags[0]["severity"] == "panic_high"


@pytest.mark.asyncio
async def test_normal_labs_produce_no_critical_flags(auth_client):
    patient = await create_patient(auth_client)
    await _upload_and_approve(
        auth_client,
        patient["id"],
        content=b"%PDF-1.4\nLABS:\nSodium: 140 mmol/L (135-145)\n",
    )
    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/labs/critical-flags")
    assert resp.status_code == 200
    assert resp.json()["flags"] == []


@pytest.mark.asyncio
async def test_document_approval_audits_critical_lab_value(auth_client):
    patient = await create_patient(auth_client)
    await _upload_and_approve(auth_client, patient["id"])

    audit = await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")
    actions = {entry["action"] for entry in audit.json()["items"]}
    assert "critical_lab_value_detected" in actions


@pytest.mark.asyncio
async def test_critical_flags_requires_ownership(client):
    """A patient owned by a different account is not accessible (404), not leaked."""
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "other@example.com", "password": "password123"},
    )
    token = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    patient = await create_patient(client)

    resp2 = await client.post(
        "/api/v1/auth/signup",
        json={"email": "second@example.com", "password": "password123"},
    )
    client.headers["Authorization"] = f"Bearer {resp2.json()['access_token']}"
    resp = await client.get(f"/api/v1/patients/{patient['id']}/labs/critical-flags")
    assert resp.status_code == 404
