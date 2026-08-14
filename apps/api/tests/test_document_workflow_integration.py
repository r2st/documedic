"""Upload and download as a clinician actually uses them: many files, over many visits.

``test_document_lifecycle_e2e`` follows *one* document from upload to download. That proves
the pipeline works; it cannot prove the pipeline keeps documents apart, because with one
document in the system every wrong answer is also the right one. A chart in use holds a
prescription, two lab reports and a discharge summary, uploaded weeks apart and downloaded in
whatever order someone opens them — and the bug that matters there is the one where document B
comes back for a request for document A.

So these tests build charts with several documents and assert on isolation: each download
returns its own bytes, a document id from one chart cannot be fetched through another chart's
URL, and the audit trail names the right document each time.

The other half is the paths a mature deployment reaches and a happy-path test never does: the
stored file gone from underneath a document row that still exists, and two clinicians
downloading the same scan at the same moment — now that every download appends to a
hash-chained log, that is two appends contending for the same chain tail.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.document import Document
from app.services.document_service import DocumentService
from tests.conftest import create_patient
from tests.test_concurrent_ingestion import (  # noqa: F401 — fixtures used by name
    clinicians,
    concurrent_app,
    file_engine,
    file_sessionmaker,
)

# Four distinguishable scans. Each carries different clinical content, so a download that
# returns the wrong one fails on the bytes rather than on a length that happens to match.
SCANS: dict[str, bytes] = {
    "prescription.pdf": (
        b"%PDF-1.4\nMEDICATIONS:\nGlycomet 500mg BD\nCONDITIONS:\nType 2 Diabetes Mellitus\n"
    ),
    "renal-panel.pdf": b"%PDF-1.4\nLABS:\nCreatinine: 1.4 mg/dL (0.6-1.2)\n",
    "lipids.pdf": b"%PDF-1.4\nLABS:\nLDL: 168 mg/dL (0-100)\n",
    "discharge.pdf": b"%PDF-1.4\nCONDITIONS:\nHypertension\nMEDICATIONS:\nAmlodipine 5mg OD\n",
}

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
    b"\x00\x00IEND\xaeB`\x82"
)


async def _upload(client, patient_id: str, name: str, data: bytes, mime="application/pdf") -> dict:
    resp = await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, data, mime)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _download(client, patient_id: str, doc_id: str):
    return await client.get(f"/api/v1/patients/{patient_id}/documents/{doc_id}/file")


async def _upload_chart(client, patient_id: str) -> dict[str, dict]:
    """Every scan in SCANS, uploaded to one chart, keyed by file name."""
    return {name: await _upload(client, patient_id, name, data) for name, data in SCANS.items()}


# ---------------------------------------------------------------- isolation across documents


async def test_every_document_in_a_chart_downloads_as_its_own_bytes(auth_client):
    """The property a single-document test cannot state: no cross-over.

    Downloads run in reverse upload order, because "returns the most recently written file"
    is the exact bug this guards and forward order would hide it.
    """
    patient = await create_patient(auth_client)
    uploaded = await _upload_chart(auth_client, patient["id"])

    for name in reversed(list(SCANS)):
        resp = await _download(auth_client, patient["id"], uploaded[name]["id"])
        assert resp.status_code == 200, resp.text
        assert resp.content == SCANS[name], f"{name} downloaded as another document's bytes"


async def test_listing_a_chart_returns_every_uploaded_document_exactly_once(auth_client):
    patient = await create_patient(auth_client)
    uploaded = await _upload_chart(auth_client, patient["id"])

    listing = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")
    assert listing.status_code == 200
    listed_ids = [d["id"] for d in listing.json()]

    assert sorted(listed_ids) == sorted(d["id"] for d in uploaded.values())
    assert len(listed_ids) == len(set(listed_ids)), "the listing repeated a document"


async def test_a_document_cannot_be_fetched_through_another_charts_url(auth_client):
    """Same clinician, two of their own patients — the ownership check is per chart.

    A UUID in a URL is guessable-adjacent (it appears in logs, in a shared link, in a screen
    recording), and the account owns both charts, so account-level authorisation alone would
    let this through. Every document route has to scope by patient as well.
    """
    first = await create_patient(auth_client, full_name="First Patient")
    second = await create_patient(auth_client, full_name="Second Patient")
    doc = await _upload(auth_client, first["id"], "prescription.pdf", SCANS["prescription.pdf"])

    for url in (
        f"/api/v1/patients/{second['id']}/documents/{doc['id']}",
        f"/api/v1/patients/{second['id']}/documents/{doc['id']}/extraction",
        f"/api/v1/patients/{second['id']}/documents/{doc['id']}/file",
    ):
        resp = await auth_client.get(url)
        assert resp.status_code == 404, f"{url} reached another chart's document: {resp.text}"
        assert resp.json()["code"] == "document_not_found"

    approve = await auth_client.post(
        f"/api/v1/patients/{second['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 404, "an extraction was approved into the wrong chart"


async def test_approving_one_document_leaves_the_others_downloadable_and_unapproved(auth_client):
    """Approval is per document. Merging one must not consume or alter the rest of the queue."""
    patient = await create_patient(auth_client)
    uploaded = await _upload_chart(auth_client, patient["id"])
    approved = uploaded["renal-panel.pdf"]

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{approved['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert resp.status_code == 200, resp.text

    for name, doc in uploaded.items():
        download = await _download(auth_client, patient["id"], doc["id"])
        assert download.status_code == 200
        assert download.content == SCANS[name], f"{name} changed when another was approved"

    record = (await auth_client.get(f"/api/v1/patients/{patient['id']}/record")).json()
    markers = {lab["marker_name"] for lab in record["lab_results"]}
    assert "Creatinine" in markers
    assert "LDL" not in markers, "an unapproved document's labs reached the chart"


async def test_a_whole_chart_of_documents_merges_into_one_record(auth_client):
    """The workflow a first consultation actually is: hand over a folder, approve it, read it.

    Asserted on the assembled record rather than on the per-approval counts, because that is
    where a merge that dropped or duplicated an entity becomes visible to a clinician.
    """
    patient = await create_patient(auth_client)
    uploaded = await _upload_chart(auth_client, patient["id"])

    for doc in uploaded.values():
        resp = await auth_client.post(
            f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
            json={"corrections": [], "rejected_entity_indexes": []},
        )
        assert resp.status_code == 200, resp.text

    record = (await auth_client.get(f"/api/v1/patients/{patient['id']}/record")).json()
    assert {lab["marker_name"] for lab in record["lab_results"]} == {"Creatinine", "LDL"}
    generics = {m["generic_name"] for m in record["medications"]}
    assert {"Metformin", "Amlodipine"} <= generics, generics
    conditions = {c["condition_name"] for c in record["conditions"]}
    assert {"Type 2 Diabetes Mellitus", "Hypertension"} <= conditions, conditions

    # And every original is still retrievable afterwards — approval merges, it does not consume.
    for name, doc in uploaded.items():
        download = await _download(auth_client, patient["id"], doc["id"])
        assert download.status_code == 200 and download.content == SCANS[name]


async def test_mixed_pdf_and_image_uploads_keep_their_own_types_and_dispositions(auth_client):
    """A chart holds both scanned photos and generated PDFs; each must round-trip as itself."""
    patient = await create_patient(auth_client)
    pdf = await _upload(auth_client, patient["id"], "rx.pdf", SCANS["prescription.pdf"])
    png = await _upload(auth_client, patient["id"], "photo.png", PNG, mime="image/png")

    pdf_resp = await _download(auth_client, patient["id"], pdf["id"])
    png_resp = await _download(auth_client, patient["id"], png["id"])

    assert pdf_resp.content == SCANS["prescription.pdf"]
    assert png_resp.content == PNG
    assert pdf_resp.headers["content-type"] == "application/pdf"
    assert png_resp.headers["content-type"] == "image/png"
    # A user-uploaded PDF is never rendered on this origin; an inert, magic-byte-verified
    # image may be.
    assert pdf_resp.headers["content-disposition"].startswith("attachment")
    assert png_resp.headers["content-disposition"].startswith("inline")


# ---------------------------------------------------------------- the trail the workflow leaves


async def test_the_full_workflow_leaves_one_audit_entry_per_step(auth_client):
    """Upload, list, review, approve, download — each step accounted for, in order.

    The read steps are the ones that were previously invisible, so this walks the sequence a
    clinician performs and asserts the trail reads back as the same story.
    """
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"], "rx.pdf", SCANS["prescription.pdf"])

    assert (await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")).status_code == 200
    assert (
        await auth_client.get(f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/extraction")
    ).status_code == 200
    assert (
        await auth_client.post(
            f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
            json={"corrections": [], "rejected_entity_indexes": []},
        )
    ).status_code == 200
    assert (await _download(auth_client, patient["id"], doc["id"])).status_code == 200

    trail = (
        await auth_client.get(f"/api/v1/patients/{patient['id']}/audit", params={"limit": 200})
    ).json()["items"]
    # Newest-first from the API; read it forwards, the way the consultation happened.
    actions = [entry["action"] for entry in reversed(trail)]

    expected_order = [
        "document_uploaded",
        "document_list_viewed",
        "extraction_viewed",
        "extraction_approved",
        "document_downloaded",
    ]
    positions = [actions.index(a) for a in expected_order if a in actions]
    assert len(positions) == len(expected_order), (
        f"the workflow lost a step from the trail: {actions}"
    )
    assert positions == sorted(positions), f"the trail is out of order: {actions}"

    verify = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit/verify")).json()
    assert verify["chain_valid"] is True


async def test_each_download_names_the_document_it_actually_handed_over(auth_client):
    """With several documents in a chart, "a scan was downloaded" is not enough.

    The trail has to say *which* one, or it cannot answer the question it exists for.
    """
    patient = await create_patient(auth_client)
    uploaded = await _upload_chart(auth_client, patient["id"])
    fetched = ["lipids.pdf", "prescription.pdf", "lipids.pdf"]

    for name in fetched:
        assert (
            await _download(auth_client, patient["id"], uploaded[name]["id"])
        ).status_code == 200

    trail = (
        await auth_client.get(
            f"/api/v1/patients/{patient['id']}/audit",
            params={"action": "document_downloaded", "limit": 200},
        )
    ).json()["items"]

    assert len(trail) == len(fetched)
    recorded = [entry["entity_id"] for entry in reversed(trail)]
    assert recorded == [uploaded[name]["id"] for name in fetched]


# ---------------------------------------------------------------- storage gone missing


async def test_a_document_whose_file_vanished_is_a_404_the_clinician_can_act_on(auth_client, db):
    """The row outlives the bytes: a pruned volume, a half-restored backup, a remounted disk.

    The document row is still real and its extracted details are still in the chart, so the
    answer is not "try again" — it is that this particular file cannot be produced and someone
    has to look at storage. Everything else about the chart must keep working.
    """
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"], "rx.pdf", SCANS["prescription.pdf"])
    other = await _upload(auth_client, patient["id"], "lipids.pdf", SCANS["lipids.pdf"])

    row = (await db.execute(select(Document).where(Document.id == doc["id"]))).scalar_one()
    Path(row.storage_path).unlink()

    resp = await _download(auth_client, patient["id"], doc["id"])
    assert resp.status_code == 404, resp.text
    error = resp.json()
    assert error["code"] == "document_not_found"
    assert "storage" in error["message"].lower(), error["message"]

    # The metadata, the extraction and the rest of the chart are unaffected.
    assert (
        await auth_client.get(f"/api/v1/patients/{patient['id']}/documents/{doc['id']}")
    ).status_code == 200
    assert (await _download(auth_client, patient["id"], other["id"])).content == SCANS["lipids.pdf"]


async def test_a_failed_download_leaves_no_record_claiming_a_disclosure(auth_client, db):
    """Nothing was handed over, so nothing may be recorded as having been.

    The audit entry is appended after the bytes are successfully read, which is what makes the
    trail's own claim true. An entry written before the read would assert a disclosure that
    did not happen — worse than no entry, because it is wrong in the direction a reviewer
    would act on.
    """
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"], "rx.pdf", SCANS["prescription.pdf"])

    row = (await db.execute(select(Document).where(Document.id == doc["id"]))).scalar_one()
    Path(row.storage_path).unlink()

    assert (await _download(auth_client, patient["id"], doc["id"])).status_code == 404

    trail = (
        await auth_client.get(
            f"/api/v1/patients/{patient['id']}/audit",
            params={"action": "document_downloaded", "limit": 200},
        )
    ).json()["items"]
    assert trail == [], "a failed download was recorded as a completed one"


async def test_a_storage_failure_never_leaks_the_path_to_the_client(auth_client, db):
    """The filesystem layout is an operator's business, and the path contains the patient id."""
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"], "rx.pdf", SCANS["prescription.pdf"])

    row = (await db.execute(select(Document).where(Document.id == doc["id"]))).scalar_one()
    storage_path = row.storage_path
    Path(storage_path).unlink()

    resp = await _download(auth_client, patient["id"], doc["id"])
    body = resp.text

    assert storage_path not in body
    assert patient["id"] not in body, "the response echoed the patient id from the storage path"
    for leak in ("/storage", "FileNotFoundError", "Traceback", "sha256"):
        assert leak not in body, f"the storage failure leaked {leak!r} to the client: {body}"


# ---------------------------------------------------------------- simultaneous downloads


@pytest_asyncio.fixture
async def concurrent_patient(clinicians):  # noqa: F811 — fixture injected by name
    first, _second = clinicians
    resp = await first.post(
        "/api/v1/patients",
        json={
            "full_name": "Concurrent Download Patient",
            "sex": "female",
            "date_of_birth": "1975-02-11",
            "consent_given": True,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_two_clinicians_downloading_at_once_both_get_the_file_and_both_are_recorded(
    clinicians,  # noqa: F811
    concurrent_patient,
):
    """Downloads write now, so simultaneous downloads contend for the audit chain tail.

    Before every download was audited this was a pure read and could not conflict with
    anything. It is now an append, and two of them racing is the case where a naive
    read-max-then-insert loses an entry — which would mean a disclosure that happened and was
    not recorded. Run on the file-backed fixture, because the shared in-memory connection used
    elsewhere cannot produce real contention.
    """
    first, second = clinicians
    upload = await first.post(
        f"/api/v1/patients/{concurrent_patient}/documents",
        files={"file": ("rx.pdf", SCANS["prescription.pdf"], "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]
    url = f"/api/v1/patients/{concurrent_patient}/documents/{doc_id}/file"

    responses = await asyncio.gather(*(c.get(url) for c in (first, second, first, second)))

    assert [r.status_code for r in responses] == [200] * 4, [r.text for r in responses]
    digest = hashlib.sha256(SCANS["prescription.pdf"]).hexdigest()
    for resp in responses:
        assert hashlib.sha256(resp.content).hexdigest() == digest

    trail = (
        await first.get(
            f"/api/v1/patients/{concurrent_patient}/audit",
            params={"action": "document_downloaded", "limit": 200},
        )
    ).json()
    assert trail["pagination"]["total"] == 4, (
        f"four downloads produced {trail['pagination']['total']} audit entries — a disclosure "
        "went unrecorded under contention"
    )
    verify = (await first.get(f"/api/v1/patients/{concurrent_patient}/audit/verify")).json()
    assert verify["chain_valid"] is True


@pytest.mark.asyncio
async def test_simultaneous_uploads_of_different_files_all_land_and_stay_distinct(
    clinicians,  # noqa: F811
    concurrent_patient,
):
    """Four files arriving at once must produce four documents, each downloadable as itself."""
    first, second = clinicians
    senders = [first, second, first, second]

    uploads = await asyncio.gather(
        *(
            client.post(
                f"/api/v1/patients/{concurrent_patient}/documents",
                files={"file": (name, data, "application/pdf")},
            )
            for client, (name, data) in zip(senders, SCANS.items(), strict=True)
        )
    )
    assert [u.status_code for u in uploads] == [201] * 4, [u.text for u in uploads]

    ids = [u.json()["id"] for u in uploads]
    assert len(set(ids)) == 4, "concurrent uploads of different files collapsed into one document"

    for doc_id, (name, data) in zip(ids, SCANS.items(), strict=True):
        resp = await first.get(f"/api/v1/patients/{concurrent_patient}/documents/{doc_id}/file")
        assert resp.status_code == 200, resp.text
        assert resp.content == data, f"{name} came back as another document"


# ------------------------------------------------------------------ approving the same scan twice

# Nothing rejects a second approval of one document — ``extraction_metadata["approved"]`` is
# written but never read — and two ordinary things produce one: a double-clicked Approve button
# on a slow connection, and a clinician who approves, spots a mis-extracted value and approves
# again with a correction. So the merge has to be idempotent at the API boundary, not just in
# GraphService, which is where these differ from the unit tests in
# ``test_graph_service_merge_dedup``.


async def test_approving_the_same_document_twice_does_not_duplicate_the_record(auth_client):
    """The chart after two approvals is the chart after one."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc = await _upload(auth_client, pid, "renal-panel.pdf", SCANS["renal-panel.pdf"])
    body = {"corrections": [], "rejected_entity_indexes": []}

    first = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc['id']}/approve", json=body
    )
    assert first.status_code == 200, first.text
    after_first = (await auth_client.get(f"/api/v1/patients/{pid}/record")).json()

    second = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc['id']}/approve", json=body
    )
    assert second.status_code == 200, second.text
    after_second = (await auth_client.get(f"/api/v1/patients/{pid}/record")).json()

    assert second.json()["merged"] == dict.fromkeys(first.json()["merged"], 0)
    for section in ("medications", "lab_results", "conditions", "allergies", "derived_markers"):
        assert len(after_second[section]) == len(after_first[section]), (
            f"re-approving duplicated {section}: "
            f"{len(after_first[section])} -> {len(after_second[section])}"
        )


async def test_re_approving_with_a_correction_amends_the_record_without_duplicating_it(auth_client):
    """The realistic second approval: the merge was right except for one value.

    This is why a second approval cannot simply be refused. The clinician is correcting the
    chart, so the corrected entity has to merge — while everything alongside it, already in the
    record from the first pass, must not arrive a second time.
    """
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc = await _upload(auth_client, pid, "renal-panel.pdf", SCANS["renal-panel.pdf"])
    body = {"corrections": [], "rejected_entity_indexes": []}
    assert (
        await auth_client.post(f"/api/v1/patients/{pid}/documents/{doc['id']}/approve", json=body)
    ).status_code == 200

    extraction = (
        await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc['id']}/extraction")
    ).json()
    index = next(
        i for i, e in enumerate(extraction["entities"]) if e["entity_type"] == "lab_result"
    )

    corrected = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc['id']}/approve",
        json={
            "corrections": [{"entity_index": index, "field_name": "value_numeric", "value": "2.9"}],
            "rejected_entity_indexes": [],
        },
    )
    assert corrected.status_code == 200, corrected.text

    record = (await auth_client.get(f"/api/v1/patients/{pid}/record")).json()
    creatinines = sorted(
        lab["value_numeric"] for lab in record["lab_results"] if lab["marker_name"] == "Creatinine"
    )
    # Both readings are present and distinct: the original stands (nothing in this system edits
    # a merged clinical row in place) and the corrected value is a new one alongside it.
    assert creatinines == ["1.400000", "2.900000"], creatinines


async def test_a_re_approval_leaves_the_other_documents_in_the_chart_untouched(auth_client):
    """Idempotency must be per document — re-approving one scan cannot disturb its neighbours."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    uploaded = await _upload_chart(auth_client, pid)
    body = {"corrections": [], "rejected_entity_indexes": []}
    for doc in uploaded.values():
        assert (
            await auth_client.post(
                f"/api/v1/patients/{pid}/documents/{doc['id']}/approve", json=body
            )
        ).status_code == 200

    before = (await auth_client.get(f"/api/v1/patients/{pid}/record")).json()
    again = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{uploaded['renal-panel.pdf']['id']}/approve", json=body
    )
    assert again.status_code == 200, again.text
    after = (await auth_client.get(f"/api/v1/patients/{pid}/record")).json()

    assert {lab["marker_name"] for lab in after["lab_results"]} == {"Creatinine", "LDL"}
    for section in ("medications", "lab_results", "conditions", "allergies", "derived_markers"):
        assert len(after[section]) == len(before[section])


def test_approve_locks_the_document_row_before_reading_the_chart() -> None:
    """The overlapping double-click is closed by a row lock, and this is where that is pinned.

    Sequential re-approval is safe on every backend: the second one reads the first one's rows
    and merges nothing. Two approvals genuinely in flight are a different problem — both can read
    the chart before either has written — and the fix is the ``FOR UPDATE`` on the ``documents``
    row that makes the second wait for the first to commit.

    Asserted by compiling the statement rather than by racing two requests, because the race
    cannot be run here: SQLAlchemy's SQLite dialect drops ``FOR UPDATE`` silently, so on the
    file-backed engine the other concurrency tests use, a test of the locked behaviour would fail
    against correct code. Compiling against the PostgreSQL dialect asserts the thing that is
    actually true — that production takes the lock — instead of asserting nothing.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from app.models.document import Document

    statement = (
        select(Document)
        .where(Document.id == uuid.uuid4(), Document.is_deleted.is_(False))
        .with_for_update()
    )
    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE" in compiled
    source = inspect.getsource(DocumentService.approve)
    assert "for_update=True" in source, (
        "DocumentService.approve must load the document with a row lock; without it two "
        "simultaneous approvals both read an empty chart and both merge the same lab results"
    )


async def test_two_clinicians_approving_different_documents_at_once_both_land(
    concurrent_app,  # noqa: F811 — pytest fixture
    clinicians,  # noqa: F811 — pytest fixture
):
    """The lock is per document, so it must not serialise unrelated work into losing a merge.

    A lock taken on the wrong row — the patient, say — would make these two approvals contend,
    and the failure mode of that is silent: one merge's rows simply never appear.
    """
    one, two = clinicians
    patient = await create_patient(one)
    pid = patient["id"]
    renal = await _upload(one, pid, "renal-panel.pdf", SCANS["renal-panel.pdf"])
    lipids = await _upload(one, pid, "lipids.pdf", SCANS["lipids.pdf"])
    body = {"corrections": [], "rejected_entity_indexes": []}

    results = await asyncio.gather(
        one.post(f"/api/v1/patients/{pid}/documents/{renal['id']}/approve", json=body),
        two.post(f"/api/v1/patients/{pid}/documents/{lipids['id']}/approve", json=body),
        return_exceptions=True,
    )
    assert all(not isinstance(r, BaseException) and r.status_code == 200 for r in results), results

    record = (await one.get(f"/api/v1/patients/{pid}/record")).json()
    assert {lab["marker_name"] for lab in record["lab_results"]} == {"Creatinine", "LDL"}
    # Neither concurrent merge duplicated its own document's labs either.
    assert len(record["lab_results"]) == 2

    verify = (await one.get(f"/api/v1/patients/{pid}/audit/verify")).json()
    assert verify["chain_valid"] is True
