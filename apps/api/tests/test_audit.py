"""Immutable audit-log integration tests."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.conftest import create_patient
from tests.test_documents import PRESCRIPTION


@pytest.mark.asyncio
async def test_actions_are_logged_and_chain_is_valid(auth_client):
    patient = await create_patient(auth_client)
    doc = (
        await auth_client.post(
            f"/api/v1/patients/{patient['id']}/documents",
            files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
        )
    ).json()
    await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )

    audit = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")).json()
    actions = {entry["action"] for entry in audit["items"]}
    assert "patient_created" in actions
    assert "document_uploaded" in actions
    assert "extraction_completed" in actions
    assert "extraction_approved" in actions
    assert "graph_merged" in actions

    # Every entry carries a hash and links to the previous one.
    for entry in audit["items"]:
        assert len(entry["record_hash"]) == 64
        assert len(entry["prev_hash"]) == 64

    verify = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit/verify")).json()
    assert verify["chain_valid"] is True
    assert verify["entries_checked"] >= 4


@pytest.mark.asyncio
async def test_audit_filter_by_action(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/audit", params={"action": "patient_created"}
    )
    items = resp.json()["items"]
    assert items and all(i["action"] == "patient_created" for i in items)


@pytest.mark.asyncio
async def test_audit_log_model_is_append_only_shape(db):
    """Structural immutability: the audit table has no updated_at / is_deleted columns."""
    from app.models.audit_log import AuditLog

    columns = set(AuditLog.__table__.columns.keys())
    assert "updated_at" not in columns
    assert "is_deleted" not in columns
    assert {"sequence", "prev_hash", "record_hash"} <= columns


@pytest.mark.asyncio
async def test_tampering_detected_by_verifier(db, auth_client):
    """Mutating a stored payload makes the recomputed chain invalid."""
    patient = await create_patient(auth_client)
    # Tamper directly at the SQL layer (simulating a breach), then re-verify.
    await db.execute(
        text("UPDATE audit_logs SET payload = :p WHERE action = 'patient_created'"),
        {"p": '{"full_name": "TAMPERED"}'},
    )
    await db.commit()

    from app.services.audit_service import AuditService

    _count, valid = await AuditService(db).verify_patient_chain(patient["id"])
    assert valid is False


@pytest.mark.asyncio
async def test_audit_pagination_walks_every_entry_newest_first_without_gaps(auth_client):
    """Guards the two-phase pagination in AuditService.list_for_patient.

    That query fetches a page of ``sequence`` values in an inner statement and the rows for
    them in an outer one, so the composite index can serve it as a covering index. The risk
    of that shape is the outer statement silently losing the inner ORDER BY. Paging through
    in small steps must yield every entry exactly once, strictly newest-first.
    """
    patient = await create_patient(auth_client)
    for n in range(6):
        await auth_client.patch(
            f"/api/v1/patients/{patient['id']}", json={"notes": f"revision {n}"}
        )

    first = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")).json()
    total = first["pagination"]["total"]
    assert total >= 7  # patient_created + the six updates above

    seen: list[dict] = []
    for offset in range(0, total, 3):
        page = (
            await auth_client.get(
                f"/api/v1/patients/{patient['id']}/audit",
                params={"limit": 3, "offset": offset},
            )
        ).json()
        assert page["pagination"]["total"] == total  # the count is unpaginated
        assert [e["sequence"] for e in page["items"]] == sorted(
            (e["sequence"] for e in page["items"]), reverse=True
        )
        seen.extend(page["items"])

    sequences = [e["sequence"] for e in seen]
    assert len(sequences) == total
    assert len(set(sequences)) == total, "a page repeated an entry"
    assert sequences == sorted(sequences, reverse=True), "paging is not globally newest-first"


@pytest.mark.asyncio
async def test_audit_action_filter_applies_to_both_pagination_phases(auth_client):
    """The action filter has to be repeated in the inner sequence query.

    If it were applied only to the outer statement, the inner one would pick the newest N
    sequences of *any* action and the outer filter would then drop most of them -- returning
    far fewer rows than the limit while `total` still claimed there were more.
    """
    patient = await create_patient(auth_client)
    for n in range(5):
        await auth_client.patch(
            f"/api/v1/patients/{patient['id']}", json={"notes": f"revision {n}"}
        )

    page = (
        await auth_client.get(
            f"/api/v1/patients/{patient['id']}/audit",
            params={"action": "patient_created", "limit": 2},
        )
    ).json()
    assert page["pagination"]["total"] == 1
    assert [e["action"] for e in page["items"]] == ["patient_created"]


@pytest.mark.asyncio
async def test_reading_a_patient_record_is_audited(auth_client):
    """PHI reads must be attributable, not just writes.

    Without this, an account browsing records it has no clinical reason to open leaves no
    trace -- the most common real-world breach shape, and the one the DPDP Act's
    accountability duty is aimed at.
    """
    patient = await create_patient(auth_client)

    await auth_client.get(f"/api/v1/patients/{patient['id']}")
    await auth_client.get(f"/api/v1/patients/{patient['id']}/record")

    audit = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")).json()
    actions = [e["action"] for e in audit["items"]]
    assert "patient_viewed" in actions
    assert "patient_record_viewed" in actions

    # The access entries join the same hash chain as writes, so they are equally tamper-evident.
    verify = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit/verify")).json()
    assert verify["chain_valid"] is True


@pytest.mark.asyncio
async def test_downloading_a_document_is_audited_with_its_filename(auth_client):
    """Downloading hands over the original scan, so the retrieval itself is recorded."""
    patient = await create_patient(auth_client)
    doc = (
        await auth_client.post(
            f"/api/v1/patients/{patient['id']}/documents",
            files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
        )
    ).json()

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/file")
    assert resp.status_code == 200

    audit = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")).json()
    downloads = [e for e in audit["items"] if e["action"] == "document_downloaded"]
    assert len(downloads) == 1
    assert downloads[0]["entity_id"] == doc["id"]
    assert downloads[0]["payload"]["file_name"] == "rx.pdf"


@pytest.mark.asyncio
async def test_ownership_checks_do_not_file_spurious_view_entries(auth_client):
    """Only endpoints that actually disclose patient data audit a view.

    PatientService.get doubles as the ownership guard for most routers, so auditing inside it
    would file a `patient_viewed` for every document list, upload and audit read, burying the
    real access signal. Uploading a document must not look like someone opened the chart.
    """
    patient = await create_patient(auth_client)
    await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )
    await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")

    audit = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")).json()
    assert [e["action"] for e in audit["items"]].count("patient_viewed") == 0
