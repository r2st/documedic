"""What happens to an uploaded scan when extraction itself breaks.

The extraction pipeline is defensive at every layer it knows about — a vision-provider error
falls through to the deterministic parser, an unreadable image yields no entities — so the only
way to reach ``_run_extraction``'s handler is a genuine, unanticipated fault. These tests pin
what that must cost the clinician: the document, never.

The failure mode being guarded is specific and quiet. ``upload`` writes the file to storage
before the transaction holding the ``documents`` row commits, so an exception out of the
pipeline used to roll the row back while the bytes stayed on disk — an orphaned blob, nothing
in the chart, and a 500 in front of a clinician holding the only copy of a prescription. Worse,
re-uploading could not recover it: dedup matches ``(patient_id, sha256)`` against rows that no
longer existed, so each retry wrote another orphan.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.document import Document
from tests.conftest import create_patient

SCAN = b"%PDF-1.4\nMEDICATIONS:\nGlycomet 500mg BD\n"


async def _upload(client, patient_id, content=SCAN, name="rx.pdf"):
    return await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, content, "application/pdf")},
    )


def _exploding_pipeline():
    """Patch the pipeline to raise the kind of fault nothing else in it catches."""
    return patch(
        "app.services.document_service.ExtractionPipeline.run",
        side_effect=RuntimeError("extractor segfaulted on page 2 of Ramesh Kumar's scan"),
    )


@pytest.mark.asyncio
async def test_upload_succeeds_and_keeps_the_document_when_extraction_raises(auth_client):
    """The upload is a 201 with the document, not a 500 — the scan is in the chart."""
    patient = await create_patient(auth_client)

    with _exploding_pipeline():
        resp = await _upload(auth_client, patient["id"])

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["extraction_status"] == "failed"
    assert body["file_name"] == "rx.pdf"


@pytest.mark.asyncio
async def test_a_failed_document_is_still_listed_readable_and_downloadable(auth_client):
    """Everything the clinician needs in order to key the values in by hand still works."""
    patient = await create_patient(auth_client)
    with _exploding_pipeline():
        doc = (await _upload(auth_client, patient["id"])).json()

    listing = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")
    assert [d["id"] for d in listing.json()] == [doc["id"]]

    fetched = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents/{doc['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["extraction_status"] == "failed"

    # The extraction read-back is an empty result, not a 500 on a missing metadata key.
    extraction = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/extraction"
    )
    assert extraction.status_code == 200, extraction.text
    assert extraction.json()["entities"] == []
    assert extraction.json()["confirmation_required_count"] == 0

    # The original bytes — the whole point of keeping the row.
    download = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/file")
    assert download.status_code == 200
    assert download.content == SCAN


@pytest.mark.asyncio
async def test_the_failure_is_audited_without_quoting_the_document(auth_client):
    """`extraction_failed` reaches the audit trail carrying the fault's type and nothing else.

    The exception message here embeds a patient name, which is exactly why only
    ``type(exc).__name__`` is recorded: ``audit_logs.payload`` is stored unencrypted.
    """
    patient = await create_patient(auth_client)
    with _exploding_pipeline():
        doc = (await _upload(auth_client, patient["id"])).json()

    audit = await auth_client.get(f"/api/v1/patients/{patient['id']}/audit?limit=100")
    entries = audit.json()["items"]
    actions = [e["action"] for e in entries]

    assert "document_uploaded" in actions
    assert "extraction_failed" in actions
    assert "extraction_completed" not in actions

    failure = next(e for e in entries if e["action"] == "extraction_failed")
    assert failure["payload"] == {"failure_type": "RuntimeError"}
    assert failure["entity_id"] == doc["id"]

    serialized = str(entries)
    assert "Ramesh Kumar" not in serialized
    assert "segfaulted" not in serialized


@pytest.mark.asyncio
async def test_the_error_response_never_carries_the_extractor_fault(auth_client):
    """A 201 body has no room for it, but assert the message text explicitly anyway."""
    patient = await create_patient(auth_client)
    with _exploding_pipeline():
        resp = await _upload(auth_client, patient["id"])

    assert "segfaulted" not in resp.text
    assert "Ramesh Kumar" not in resp.text


@pytest.mark.asyncio
async def test_retrying_the_same_file_reuses_the_row_instead_of_orphaning_another(auth_client, db):
    """Dedup still works after a failure, which is what stops retries piling up blobs.

    The pre-fix behaviour rolled the row back, so ``(patient_id, sha256)`` never matched and a
    clinician retrying an upload wrote a fresh orphan every time.
    """
    patient = await create_patient(auth_client)
    with _exploding_pipeline():
        first = (await _upload(auth_client, patient["id"])).json()
        second = (await _upload(auth_client, patient["id"])).json()

    assert first["id"] == second["id"]

    rows = (
        (await db.execute(select(Document).where(Document.patient_id == patient["id"])))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].extraction_status == "failed"
    assert rows[0].extraction_metadata["failure_type"] == "RuntimeError"
    assert rows[0].extraction_completed_at is not None


@pytest.mark.asyncio
async def test_a_later_upload_of_a_different_file_still_extracts_normally(auth_client):
    """One failure does not poison the pipeline for the next document."""
    patient = await create_patient(auth_client)
    with _exploding_pipeline():
        await _upload(auth_client, patient["id"])

    good = await _upload(auth_client, patient["id"], content=SCAN + b"LABS:\nHbA1c: 8.1 %\n")
    assert good.status_code == 201
    assert good.json()["extraction_status"] in {"completed", "needs_confirmation"}
