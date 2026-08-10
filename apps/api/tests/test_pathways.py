"""Clinical pathway API tests: guideline-cited stages, patient condition mapping."""

from __future__ import annotations

import pytest

from tests.conftest import create_patient
from tests.test_documents import PRESCRIPTION


@pytest.mark.asyncio
async def test_list_pathways(auth_client):
    resp = await auth_client.get("/api/v1/pathways")
    assert resp.status_code == 200
    names = resp.json()
    assert "Hypertension" in names
    assert "Type 2 Diabetes Mellitus" in names


@pytest.mark.asyncio
async def test_get_pathway_has_real_citations(auth_client):
    resp = await auth_client.get("/api/v1/pathways/hypertension")
    assert resp.status_code == 200
    body = resp.json()
    assert body["condition_name"] == "Hypertension"
    assert body["source"] == "icmr"
    assert body["autonomy_tier"] == "informational"
    stage_keys = {s["key"] for s in body["stages"]}
    expected_keys = {
        "diagnostic_workup",
        "first_line_therapy",
        "escalation",
        "monitoring",
        "cant_miss",
    }
    assert expected_keys <= stage_keys

    dx_stage = next(s for s in body["stages"] if s["key"] == "diagnostic_workup")
    assert dx_stage["citations"], "diagnosis stage should carry a real corpus citation"
    citation = dx_stage["citations"][0]
    assert citation["section_id"] == "ICMR-HTN-DX"
    assert citation["source"] == "icmr"
    assert citation["snippet"]


@pytest.mark.asyncio
async def test_no_certainty_language_in_pathway_items(auth_client):
    """CLAUDE.md rule #4: no imperative clinical language ('Give ', 'Start ', 'Administer ')."""
    resp = await auth_client.get("/api/v1/pathways")
    names = resp.json()
    banned = ("give ", "administer ", "the patient has ", "diagnose with ", "start the patient on ")
    for name in names:
        detail = await auth_client.get(f"/api/v1/pathways/{name}")
        for stage in detail.json()["stages"]:
            for item in stage["items"]:
                lowered = item.lower()
                for phrase in banned:
                    assert phrase not in lowered, f"{name}/{stage['key']}: {item!r}"


@pytest.mark.asyncio
async def test_unknown_condition_returns_404(auth_client):
    resp = await auth_client.get("/api/v1/pathways/some-nonexistent-condition")
    assert resp.status_code == 404
    assert resp.json()["code"] == "pathway_not_found"


@pytest.mark.asyncio
async def test_patient_pathways_maps_active_conditions(auth_client):
    """Patient has Type 2 Diabetes Mellitus (from the PRESCRIPTION fixture) -- the endpoint
    should surface its pathway and report any unmapped active conditions separately."""
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )
    doc = resp.json()
    await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/pathways")
    assert resp.status_code == 200
    body = resp.json()
    assert body["patient_id"] == patient["id"]
    conditions = {p["condition_name"] for p in body["pathways"]}
    assert "Type 2 Diabetes Mellitus" in conditions


@pytest.mark.asyncio
async def test_patient_pathways_requires_ownership(auth_client, client):
    """Cross-account access to another clinician's patient must 404, not leak existence."""
    patient = await create_patient(auth_client)
    other = await client.post(
        "/api/v1/auth/signup", json={"email": "other-pathways@b.com", "password": "password123"}
    )
    token = other.json()["access_token"]
    resp = await client.get(
        f"/api/v1/patients/{patient['id']}/pathways",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
