"""The full life of an uploaded file, over HTTP: upload -> read back -> approve -> download.

``test_documents`` covers upload and approval, and ``test_document_download`` covers the
Content-Disposition header in isolation. Neither walks a document from the bytes going in to
the same bytes coming back out, which is the property that matters clinically: what a clinician
downloads months later has to be the original scan, byte for byte, not a re-encoding or another
patient's file. These tests follow one document through every endpoint that touches it and
assert on the bytes, the metadata, the audit trail, and the tenancy boundary at each step.
"""

from __future__ import annotations

import hashlib

import pytest

from tests.conftest import create_patient

# The %PDF- header satisfies magic-byte sniffing; the body is read by the deterministic text
# parser, so no PDF library or LLM is involved.
SCAN = (
    b"%PDF-1.4\n"
    b"MEDICATIONS:\n"
    b"Glycomet 500mg BD\n"
    b"LABS:\n"
    b"Creatinine: 1.1 mg/dL (0.6-1.2)\n"
    b"CONDITIONS:\n"
    b"Type 2 Diabetes Mellitus\n"
)

# A one-pixel PNG: a real magic-byte-verified image, for the inline-vs-attachment branch.
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
    b"\x00\x00IEND\xaeB`\x82"
)


async def _upload(client, patient_id, content=SCAN, name="rx.pdf", mime="application/pdf"):
    return await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, content, mime)},
    )


async def _audit_actions(client, patient_id) -> list[str]:
    resp = await client.get(f"/api/v1/patients/{patient_id}/audit?limit=100")
    assert resp.status_code == 200, resp.text
    return [entry["action"] for entry in resp.json()["items"]]


@pytest.mark.asyncio
async def test_a_document_survives_the_whole_round_trip_byte_for_byte(auth_client):
    """Upload, list, fetch metadata, fetch extraction, approve, download — one document.

    The download is the assertion that matters: the archived scan is the primary clinical
    source, so it has to come back identical to what was stored, and its recorded sha256 has
    to be the digest of those exact bytes.
    """
    patient = await create_patient(auth_client)
    pid = patient["id"]

    upload = await _upload(auth_client, pid)
    assert upload.status_code == 201, upload.text
    document = upload.json()
    doc_id = document["id"]

    assert document["file_name"] == "rx.pdf"
    assert document["file_type"] == "pdf"
    assert document["file_size_bytes"] == len(SCAN)

    listed = (await auth_client.get(f"/api/v1/patients/{pid}/documents")).json()
    assert [d["id"] for d in listed] == [doc_id]

    fetched = await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == doc_id

    extraction = await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}/extraction")
    assert extraction.status_code == 200, extraction.text
    assert extraction.json()["document_id"] == doc_id
    assert extraction.json()["entities"], "the deterministic parser found nothing to review"

    approve = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 200, approve.text

    download = await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}/file")
    assert download.status_code == 200, download.text
    assert download.content == SCAN, "the downloaded scan is not the bytes that were uploaded"
    assert download.headers["content-type"].startswith("application/pdf")


@pytest.mark.asyncio
async def test_the_stored_hash_is_the_digest_of_the_downloadable_bytes(auth_client, db):
    """The recorded sha256 is what makes the archive tamper-evident, so it must match.

    Read from the database rather than the API, because ``DocumentResponse`` deliberately
    does not expose the hash or the storage path — those are storage internals, not clinical
    data. The invariant is still worth pinning: it is what a future integrity check would
    compare against.
    """
    import uuid as _uuid

    from app.models.document import Document

    pid = (await create_patient(auth_client))["id"]
    doc_id = (await _upload(auth_client, pid)).json()["id"]

    download = await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}/file")
    stored = await db.get(Document, _uuid.UUID(doc_id))

    assert stored is not None
    assert stored.storage_hash_sha256 == hashlib.sha256(download.content).hexdigest()


@pytest.mark.asyncio
async def test_a_document_remains_downloadable_after_its_extraction_is_approved(auth_client):
    """Approval merges data into the graph; it must not disturb the archived original."""
    pid = (await create_patient(auth_client))["id"]
    doc_id = (await _upload(auth_client, pid)).json()["id"]

    before = await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}/file")
    await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    after = await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}/file")

    assert before.content == after.content == SCAN


@pytest.mark.asyncio
async def test_every_step_of_the_lifecycle_is_audited(auth_client):
    """Upload, extraction, approval, graph merge and download each leave a trail.

    The download entry is the one that is easy to forget and the most important: it is the
    widest PHI disclosure in the API, handing over the original scan rather than a summary.
    """
    pid = (await create_patient(auth_client))["id"]
    doc_id = (await _upload(auth_client, pid)).json()["id"]
    await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}/file")

    actions = await _audit_actions(auth_client, pid)
    for expected in (
        "document_uploaded",
        "extraction_completed",
        "extraction_approved",
        "graph_merged",
        "document_downloaded",
    ):
        assert expected in actions, f"{expected} is missing from the audit trail: {actions}"


@pytest.mark.asyncio
async def test_the_audit_trail_never_records_the_uploaded_file_name(auth_client):
    """Scan filenames routinely carry the patient's name, and audit payloads are unencrypted.

    ``audit_logs`` is immutable and never pruned, so a direct identifier written into a payload
    is written there permanently. The document row already holds the name, and ``entity_id``
    points at it.
    """
    pid = (await create_patient(auth_client))["id"]
    identifying_name = "ramesh_kumar_cbc_2026.pdf"
    doc_id = (await _upload(auth_client, pid, name=identifying_name)).json()["id"]
    await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}/file")

    entries = (await auth_client.get(f"/api/v1/patients/{pid}/audit?limit=100")).json()["items"]
    for entry in entries:
        assert identifying_name not in str(entry.get("payload") or {}), (
            f"the {entry['action']} audit payload carries the uploaded file name, which is a "
            "direct identifier in an immutable table"
        )


@pytest.mark.asyncio
async def test_re_uploading_identical_bytes_returns_the_same_document(auth_client):
    """Clinicians re-upload the same scan routinely; it must not fork the archive."""
    pid = (await create_patient(auth_client))["id"]

    first = (await _upload(auth_client, pid)).json()
    second = (await _upload(auth_client, pid, name="rx-again.pdf")).json()

    assert first["id"] == second["id"]
    listed = (await auth_client.get(f"/api/v1/patients/{pid}/documents")).json()
    assert len(listed) == 1

    download = await auth_client.get(f"/api/v1/patients/{pid}/documents/{first['id']}/file")
    assert download.content == SCAN


@pytest.mark.asyncio
async def test_the_same_bytes_under_two_patients_stay_separate_documents(auth_client):
    """Deduplication is per patient. Two patients can legitimately have the same form.

    Collapsing them would attach one patient's document — and its extracted clinical data —
    to another patient's chart.
    """
    first = (await create_patient(auth_client, full_name="Patient One"))["id"]
    second = (await create_patient(auth_client, full_name="Patient Two"))["id"]

    first_doc = (await _upload(auth_client, first)).json()
    second_doc = (await _upload(auth_client, second)).json()

    assert first_doc["id"] != second_doc["id"]
    for pid, doc_id in ((first, first_doc["id"]), (second, second_doc["id"])):
        listed = (await auth_client.get(f"/api/v1/patients/{pid}/documents")).json()
        assert [d["id"] for d in listed] == [doc_id]


@pytest.mark.asyncio
async def test_another_account_cannot_reach_any_step_of_the_lifecycle(
    auth_client, second_auth_client
):
    """Every endpoint in the lifecycle must 404 for a document the caller does not own.

    Checked per endpoint rather than once: the download route is the one that hands over raw
    PHI, so an ownership check that is present on the metadata routes but missing there would
    be invisible to a single-endpoint test.
    """
    pid = (await create_patient(auth_client))["id"]
    doc_id = (await _upload(auth_client, pid)).json()["id"]

    base = f"/api/v1/patients/{pid}/documents/{doc_id}"
    for method, path, kwargs in (
        ("get", f"/api/v1/patients/{pid}/documents", {}),
        ("get", base, {}),
        ("get", f"{base}/extraction", {}),
        ("get", f"{base}/file", {}),
        (
            "post",
            f"{base}/approve",
            {"json": {"corrections": [], "rejected_entity_indexes": []}},
        ),
    ):
        resp = await getattr(second_auth_client, method)(path, **kwargs)
        assert resp.status_code == 404, f"{method.upper()} {path} leaked: {resp.status_code}"


@pytest.mark.asyncio
async def test_an_uploaded_image_round_trips_and_is_served_inline(auth_client):
    """Images take the other branch of the disposition logic; the bytes must still match."""
    pid = (await create_patient(auth_client))["id"]
    doc_id = (
        await _upload(auth_client, pid, content=PNG, name="scan.png", mime="image/png")
    ).json()["id"]

    download = await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}/file")

    assert download.status_code == 200
    assert download.content == PNG
    assert download.headers["content-type"] == "image/png"
    assert download.headers["content-disposition"].startswith("inline")


@pytest.mark.asyncio
async def test_corrections_made_at_approval_reach_the_record(auth_client):
    """A clinician's correction is the authoritative value, so it must be what is merged.

    The extraction is what the parser thought it read; the approval is what the clinician
    confirmed. If the correction were dropped, the chart would silently keep the parser's
    reading of a hand-written prescription.
    """
    pid = (await create_patient(auth_client))["id"]
    doc_id = (await _upload(auth_client, pid)).json()["id"]

    extraction = (
        await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}/extraction")
    ).json()
    index, field = next(
        (i, f["name"])
        for i, entity in enumerate(extraction["entities"])
        if entity["entity_type"] == "medication"
        for f in entity["fields"]
        if f["name"] == "dose"
    )

    approve = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={
            "corrections": [{"entity_index": index, "field_name": field, "value": "850mg"}],
            "rejected_entity_indexes": [],
        },
    )
    assert approve.status_code == 200, approve.text

    record = (await auth_client.get(f"/api/v1/patients/{pid}/record")).json()
    assert [m["dose"] for m in record["medications"]] == ["850mg"]
    assert "field_corrected" in await _audit_actions(auth_client, pid)
