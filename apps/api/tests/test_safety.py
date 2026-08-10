"""Drug-safety API integration tests (hard blocks, warnings, offline-deterministic)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.drug_vocabulary import DrugVocabulary
from app.models.medication_event import MedicationEvent
from tests.conftest import create_patient
from tests.test_documents import PRESCRIPTION


async def _add_current_medication(db, patient_id: str, reference_id: str) -> None:
    """Directly insert a current MedicationEvent, bypassing document extraction — used to set
    up interaction scenarios for drugs the PRESCRIPTION fixture doesn't cover."""
    vocab = (
        await db.execute(
            select(DrugVocabulary).where(DrugVocabulary.reference_id == reference_id)
        )
    ).scalar_one()
    db.add(
        MedicationEvent(
            patient_id=patient_id,
            drug_vocabulary_id=vocab.id,
            generic_name=vocab.generic_name,
            event_type="start",
            is_current=True,
            clinician_confirmed=True,
        )
    )
    await db.commit()


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


@pytest.mark.asyncio
async def test_previously_dangling_interaction_refs_now_resolve_and_fire(auth_client, db):
    """SPIRO-25, THEO-300 and CONTRAST-IODINE used to be referenced by seeded interaction rules
    but had no DrugVocabulary entry, so those drugs could never be resolved/prescribed and the
    rules could never fire. Now that vocabulary entries exist, prescribing the interacting drug
    against a patient on the paired drug must surface the interaction."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    await _add_current_medication(db, pid, "ENA-5")  # Enalapril (ACE inhibitor)
    resp = await _check(auth_client, pid, drug_reference_id="SPIRO-25")
    body = resp.json()
    assert body["proposed_drug_name"] == "Spironolactone"
    assert any(
        f["check_type"] == "drug_interaction" and f["severity"] == "critical"
        for f in body["flags"]
    )


@pytest.mark.asyncio
async def test_sildenafil_nitrate_is_hard_block(auth_client, db):
    """PDE5 inhibitor + nitrate is a life-threatening combination and must hard-block."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    await _add_current_medication(db, pid, "ISMN-20")  # Isosorbide mononitrate
    resp = await _check(auth_client, pid, drug_reference_id="SIL-50")
    body = resp.json()
    assert body["is_blocked"] is True
    assert body["is_hard_block"] is True
    assert any(
        f["check_type"] == "drug_interaction" and f["is_hard_block"] for f in body["flags"]
    )


@pytest.mark.asyncio
async def test_warfarin_aspirin_bleeding_risk_flagged(auth_client, db):
    patient = await create_patient(auth_client)
    pid = patient["id"]
    await _add_current_medication(db, pid, "WARF-5")  # Warfarin
    resp = await _check(auth_client, pid, drug_reference_id="ASP-75")
    body = resp.json()
    assert any(
        f["check_type"] == "drug_interaction" and f["severity"] == "critical"
        for f in body["flags"]
    )
    assert body["is_hard_block"] is False  # major, not contraindicated -- flagged, not blocked


@pytest.mark.asyncio
async def test_lithium_nsaid_toxicity_flagged(auth_client, db):
    patient = await create_patient(auth_client)
    pid = patient["id"]
    await _add_current_medication(db, pid, "LIT-400")  # Lithium carbonate
    resp = await _check(auth_client, pid, drug_reference_id="IBU-400")
    body = resp.json()
    assert any(f["check_type"] == "drug_interaction" for f in body["flags"])


@pytest.mark.asyncio
async def test_digoxin_renal_dose_adjustment_not_full_block(auth_client):
    """Digoxin's renal contraindication is dose-adjustment, not an absolute block, so a normal
    eGFR patient shouldn't be blocked and the renal_dose flag only appears with low eGFR."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    resp = await _check(auth_client, pid, drug_reference_id="DIG-0.25")
    body = resp.json()
    assert body["is_blocked"] is False


@pytest.mark.asyncio
async def test_check_response_flags_carry_persisted_ids(auth_client):
    """Every flag in a /check response must carry the drug_safety_checks.id it was persisted
    as -- that id is required to submit a hard-block override."""
    pid = await _setup_patient_with_record(auth_client)
    resp = await _check(auth_client, pid, drug_reference_id="MET-500")
    body = resp.json()
    assert body["flags"]
    for flag in body["flags"]:
        assert flag["id"] is not None


@pytest.mark.asyncio
async def test_hard_block_override_requires_reasoning(auth_client):
    pid = await _setup_patient_with_record(auth_client)
    resp = await _check(auth_client, pid, drug_reference_id="MET-500")
    hard_block = next(f for f in resp.json()["flags"] if f["is_hard_block"])

    too_short = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/override",
        json={"drug_safety_check_id": hard_block["id"], "reasoning": "ok"},
    )
    assert too_short.status_code == 422


@pytest.mark.asyncio
async def test_hard_block_override_recorded_and_audited(auth_client):
    pid = await _setup_patient_with_record(auth_client)
    resp = await _check(auth_client, pid, drug_reference_id="MET-500")
    hard_block = next(f for f in resp.json()["flags"] if f["is_hard_block"])

    override_resp = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/override",
        json={
            "drug_safety_check_id": hard_block["id"],
            "reasoning": "Nephrology reviewed and approved continued low-dose metformin with "
            "closer creatinine monitoring given limited alternatives.",
        },
    )
    assert override_resp.status_code == 201
    override = override_resp.json()
    assert override["patient_id"] == pid
    assert override["drug_safety_check_id"] == hard_block["id"]

    listed = await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/overrides")
    assert listed.status_code == 200
    assert any(o["id"] == override["id"] for o in listed.json())

    audit_resp = await auth_client.get(f"/api/v1/patients/{pid}/audit")
    actions = [e["action"] for e in audit_resp.json()["items"]]
    assert "drug_safety_hard_block_overridden" in actions


@pytest.mark.asyncio
async def test_cannot_override_a_non_hard_block_flag(auth_client):
    pid = await _setup_patient_with_record(auth_client)
    resp = await _check(auth_client, pid, drug_reference_id="MET-500")
    non_hard_block = next(f for f in resp.json()["flags"] if not f["is_hard_block"])

    override_resp = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/override",
        json={
            "drug_safety_check_id": non_hard_block["id"],
            "reasoning": "Attempting to override a warning-level flag that isn't blocked.",
        },
    )
    assert override_resp.status_code == 422
