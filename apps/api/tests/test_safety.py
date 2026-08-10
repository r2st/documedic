"""Drug-safety API integration tests (hard blocks, warnings, offline-deterministic)."""

from __future__ import annotations

import pytest

from tests.conftest import create_patient
from tests.test_documents import PRESCRIPTION


async def _setup_patient_with_record(auth_client) -> str:
    """Create a patient and approve a prescription so the graph has meds/labs/conditions/allergy."""
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
    return patient["id"]


async def _check(auth_client, patient_id, **body):
    return await auth_client.post(f"/api/v1/patients/{patient_id}/drug-safety/check", json=body)


@pytest.mark.asyncio
async def test_renal_hard_block_for_metformin(auth_client):
    pid = await _setup_patient_with_record(auth_client)
    resp = await _check(auth_client, pid, drug_reference_id="MET-500")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_blocked"] is True
    assert body["is_hard_block"] is True
    assert any(f["check_type"] == "renal_dose" and f["is_hard_block"] for f in body["flags"])


@pytest.mark.asyncio
async def test_direct_allergy_hard_block(auth_client):
    pid = await _setup_patient_with_record(auth_client)
    resp = await _check(auth_client, pid, drug_reference_id="IBU-400")
    body = resp.json()
    assert body["is_blocked"] is True
    assert any(f["check_type"] == "allergy_conflict" for f in body["flags"])


@pytest.mark.asyncio
async def test_cross_class_allergy_hard_block(auth_client):
    """Ibuprofen allergy (NSAID) hard-blocks Diclofenac (NSAID) by class."""
    pid = await _setup_patient_with_record(auth_client)
    resp = await _check(auth_client, pid, drug_reference_id="DIC-50")
    body = resp.json()
    assert body["is_blocked"] is True
    assert any(f["check_type"] == "allergy_conflict" for f in body["flags"])


@pytest.mark.asyncio
async def test_safe_drug_not_blocked(auth_client):
    pid = await _setup_patient_with_record(auth_client)
    resp = await _check(auth_client, pid, drug_reference_id="PCM-650")
    body = resp.json()
    assert body["is_blocked"] is False
    assert body["proposed_drug_name"] == "Paracetamol"


@pytest.mark.asyncio
async def test_resolve_by_brand_name(auth_client):
    pid = await _setup_patient_with_record(auth_client)
    resp = await _check(auth_client, pid, drug_name="Brufen")  # brand for Ibuprofen
    body = resp.json()
    assert body["proposed_drug_reference_id"] == "IBU-400"
    assert body["is_blocked"] is True


@pytest.mark.asyncio
async def test_unresolvable_drug_returns_422(auth_client):
    patient = await create_patient(auth_client)
    resp = await _check(auth_client, patient["id"], drug_name="Zzzxxx Nonexistent")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_checked_against_counts_present(auth_client):
    pid = await _setup_patient_with_record(auth_client)
    resp = await _check(auth_client, pid, drug_reference_id="PCM-650")
    checked = resp.json()["checked_against"]
    assert checked["egfr_available"] is True
    assert checked["allergies"] >= 1


@pytest.mark.asyncio
async def test_reordering_active_medication_flags_duplicate_therapy(auth_client):
    """Patient is already on Glycomet (MET-500); re-checking it must surface a duplicate-therapy
    warning alongside the (unrelated) renal hard block -- neither check should suppress the
    other, and the new check_type must round-trip through persistence + the response schema."""
    pid = await _setup_patient_with_record(auth_client)
    resp = await _check(auth_client, pid, drug_reference_id="MET-500")
    assert resp.status_code == 200
    body = resp.json()
    dup = [f for f in body["flags"] if f["check_type"] == "duplicate_therapy"]
    assert len(dup) == 1
    assert dup[0]["severity"] == "warning"
    assert dup[0]["is_hard_block"] is False
    assert any(f["check_type"] == "renal_dose" for f in body["flags"])
