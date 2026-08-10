"""Document upload -> extraction -> approval -> patient-graph integration tests."""

from __future__ import annotations

import pytest

from tests.conftest import create_patient

# A synthetic prescription. The %PDF- header satisfies magic-byte sniffing; the body is
# parsed by the deterministic text fallback (no real PDF library / LLM needed in tests).
PRESCRIPTION = (
    b"%PDF-1.4\n"
    b"MEDICATIONS:\n"
    b"Glycomet 500mg BD\n"
    b"LABS:\n"
    b"Creatinine: 3.0 mg/dL (0.6-1.2)\n"
    b"HbA1c: 9.2 % (4.0-5.6)\n"
    b"CONDITIONS:\n"
    b"Type 2 Diabetes Mellitus\n"
    b"ALLERGIES:\n"
    b"Ibuprofen - hives\n"
)


async def _upload(client, patient_id, content=PRESCRIPTION, name="rx.pdf"):
    return await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, content, "application/pdf")},
    )


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_type(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("note.txt", b"hello plain text", "text/plain")},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "unsupported_file_type"


@pytest.mark.asyncio
async def test_upload_and_extract(auth_client):
    patient = await create_patient(auth_client)
    resp = await _upload(auth_client, patient["id"])
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert doc["extraction_status"] in ("needs_confirmation", "completed")

    extraction = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/extraction"
    )
    entities = extraction.json()["entities"]
    types = {e["entity_type"] for e in entities}
    assert {"medication", "lab_result", "condition", "allergy"} <= types


@pytest.mark.asyncio
async def test_upload_deduplicates_identical_bytes(auth_client):
    patient = await create_patient(auth_client)
    first = await _upload(auth_client, patient["id"])
    second = await _upload(auth_client, patient["id"])
    assert first.json()["id"] == second.json()["id"]


@pytest.mark.asyncio
async def test_approve_merges_into_graph_with_egfr(auth_client):
    patient = await create_patient(auth_client)  # male, DOB 1968 -> ~58y
    doc = (await _upload(auth_client, patient["id"])).json()

    approve = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 200, approve.text
    merged = approve.json()["merged"]
    assert merged["medications"] >= 1
    assert merged["lab_results"] == 2
    assert merged["conditions"] == 1
    assert merged["allergies"] == 1

    record = (await auth_client.get(f"/api/v1/patients/{patient['id']}/record")).json()

    # Brand name normalized to generic via DrugVocabulary (Glycomet -> Metformin).
    generics = {m["generic_name"] for m in record["medications"]}
    assert "Metformin" in generics

    # Creatinine flagged abnormal-high against its reference range.
    creat = [lab for lab in record["lab_results"] if "Creatinine" in lab["marker_name"]]
    assert creat and creat[0]["is_abnormal"] is True
    assert creat[0]["abnormality_direction"] == "high"

    # eGFR derived marker computed (CKD-EPI 2021), abnormal at creatinine 3.0.
    egfr = [d for d in record["derived_markers"] if d["marker_name"] == "eGFR"]
    assert egfr and egfr[0]["formula_name"] == "CKD-EPI_2021"
    assert egfr[0]["is_abnormal"] is True


@pytest.mark.asyncio
async def test_approve_can_reject_entities(auth_client):
    patient = await create_patient(auth_client)
    doc = (await _upload(auth_client, patient["id"])).json()
    extraction = (
        await auth_client.get(f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/extraction")
    ).json()
    allergy_idx = next(
        i for i, e in enumerate(extraction["entities"]) if e["entity_type"] == "allergy"
    )
    approve = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": [allergy_idx]},
    )
    assert approve.json()["merged"]["allergies"] == 0


@pytest.mark.asyncio
async def test_listing_documents_for_another_accounts_patient_is_not_found(client):
    """Listing used to answer 200 with [] for a patient the caller does not own, while
    every other patient-scoped route 404s. Consistency matters: a 200 confirms the route
    reached the patient scope at all."""
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "docs-owner@example.com", "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
    patient = await create_patient(client)

    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "docs-intruder@example.com", "password": "password123"},
    )
    assert resp.status_code == 201, resp.text
    client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    resp = await client.get(f"/api/v1/patients/{patient['id']}/documents")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_listing_documents_for_an_unknown_patient_is_not_found(auth_client):
    import uuid

    resp = await auth_client.get(f"/api/v1/patients/{uuid.uuid4()}/documents")
    assert resp.status_code == 404
