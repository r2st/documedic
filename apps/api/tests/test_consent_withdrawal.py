"""Withdrawing consent has to stop processing, not just be noted.

``consent_given`` gated chart *creation* and was then never read again. ``PatientUpdate``
accepts ``consent_given: false``, so a withdrawal could be recorded -- and nothing changed:
documents still uploaded, extractions still merged into the graph, and the reasoning engine
still ran over the record. Under the DPDP Act 2023 a data principal may withdraw at any time
and the fiduciary must cease processing, and ``regulatory_service`` publishes "Explicit consent
captured before clinical data is stored" as a live control in the compliance summary a DPDP or
CDSCO reviewer reads.

The line drawn here is *new processing*, not access:

* refused -- document upload, extraction approval, opening a reasoning session
* still allowed -- reading the chart, its documents, its audit trail, and the deterministic
  drug-safety checks over data already lawfully held

Withdrawal is not erasure (``soft_delete`` is), medical records carry their own retention
obligations, and a privacy flag that silently switched off the allergy hard block would be a
safety defect wearing a privacy control's clothes.
"""

from __future__ import annotations

import pytest

from tests.conftest import create_patient

PRESCRIPTION = b"%PDF-1.4\nMEDICATIONS:\nGlycomet 500mg BD\nALLERGIES:\nIbuprofen - hives\n"


async def _withdraw(client, patient_id):
    resp = await client.patch(f"/api/v1/patients/{patient_id}", json={"consent_given": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["consent_given"] is False
    return resp.json()


async def _upload(client, patient_id, name="rx.pdf"):
    return await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, PRESCRIPTION, "application/pdf")},
    )


# --------------------------------------------------------------- new processing is refused


@pytest.mark.asyncio
async def test_upload_is_refused_after_consent_is_withdrawn(auth_client):
    patient = await create_patient(auth_client)
    await _withdraw(auth_client, patient["id"])

    resp = await _upload(auth_client, patient["id"])

    assert resp.status_code == 403
    assert resp.json()["code"] == "consent_withdrawn"


@pytest.mark.asyncio
async def test_opening_a_reasoning_session_is_refused_after_withdrawal(auth_client):
    patient = await create_patient(auth_client)
    await _withdraw(auth_client, patient["id"])

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "fever for three days"},
    )

    assert resp.status_code == 403
    assert resp.json()["code"] == "consent_withdrawn"


@pytest.mark.asyncio
async def test_approving_an_extraction_is_refused_after_withdrawal(auth_client):
    """The file arrived lawfully; merging it into the chart is still fresh processing."""
    patient = await create_patient(auth_client)
    upload = await _upload(auth_client, patient["id"])
    assert upload.status_code == 201, upload.text
    doc = upload.json()

    await _withdraw(auth_client, patient["id"])

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )

    assert resp.status_code == 403
    assert resp.json()["code"] == "consent_withdrawn"


# --------------------------------------------------------------- access is not withdrawn


@pytest.mark.asyncio
async def test_the_existing_chart_stays_readable_after_withdrawal(auth_client):
    """Withdrawal stops processing; it is not erasure, and it must not hide the record."""
    patient = await create_patient(auth_client)
    await _upload(auth_client, patient["id"])
    await _withdraw(auth_client, patient["id"])

    detail = await auth_client.get(f"/api/v1/patients/{patient['id']}")
    documents = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")
    record = await auth_client.get(f"/api/v1/patients/{patient['id']}/record")
    audit = await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")

    assert detail.status_code == 200
    assert documents.status_code == 200
    assert record.status_code == 200
    assert audit.status_code == 200


@pytest.mark.asyncio
async def test_the_deterministic_safety_check_still_runs_after_withdrawal(auth_client):
    """A privacy flag must never be able to switch off an allergy hard block.

    The safety engine reads data the account already holds lawfully and returns a verdict about
    a drug the clinician is considering right now. Gating it behind consent would turn a
    withdrawal into a silently-degraded safety check, which is the opposite of what the control
    is for (Critical Safety Rules #3 and #8).
    """
    patient = await create_patient(auth_client)
    await _withdraw(auth_client, patient["id"])

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Paracetamol"},
    )

    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------- the trail records which way


@pytest.mark.asyncio
async def test_the_audit_trail_distinguishes_a_withdrawal_from_a_grant(auth_client):
    """``changed_fields: ["consent_given"]`` alone made the two indistinguishable.

    That entry is the record of when processing became unlawful, so the direction has to be
    readable from it. The value is not PII -- it is the lawful basis for holding everything
    that is.
    """
    patient = await create_patient(auth_client)
    await _withdraw(auth_client, patient["id"])
    regrant = await auth_client.patch(
        f"/api/v1/patients/{patient['id']}", json={"consent_given": True}
    )
    assert regrant.status_code == 200

    audit = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/audit", params={"action": "patient_updated"}
    )
    assert audit.status_code == 200
    # Newest first, so the re-grant precedes the withdrawal.
    states = [e["payload"]["consent_given"] for e in audit.json()["items"]]
    assert states == [True, False]


@pytest.mark.asyncio
async def test_an_unrelated_edit_records_no_consent_state(auth_client):
    """The value is None unless consent actually changed, so a true/false there is meaningful."""
    patient = await create_patient(auth_client)
    resp = await auth_client.patch(
        f"/api/v1/patients/{patient['id']}", json={"phone": "+919812345678"}
    )
    assert resp.status_code == 200

    audit = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/audit", params={"action": "patient_updated"}
    )
    entry = audit.json()["items"][0]
    assert entry["payload"]["changed_fields"] == ["phone"]
    assert entry["payload"]["consent_given"] is None


# --------------------------------------------------------------- re-granting restores it


@pytest.mark.asyncio
async def test_re_recording_consent_restores_processing(auth_client):
    patient = await create_patient(auth_client)
    await _withdraw(auth_client, patient["id"])
    assert (await _upload(auth_client, patient["id"])).status_code == 403

    regrant = await auth_client.patch(
        f"/api/v1/patients/{patient['id']}", json={"consent_given": True}
    )
    assert regrant.status_code == 200
    assert regrant.json()["consent_given"] is True

    assert (await _upload(auth_client, patient["id"])).status_code == 201
