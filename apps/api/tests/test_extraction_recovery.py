"""Documents that were uploaded but never read, and how they get out of that state.

``test_extraction_failure`` covers the one interruption the pipeline can catch — an exception
out of ``ExtractionPipeline.run``. This file covers the ones it cannot, which are the ones that
happen in production:

* the worker is restarted, the deploy rolls, or the handler's task is cancelled *during*
  extraction (``asyncio.CancelledError`` is a ``BaseException``, so no ``except Exception``
  anywhere in the stack sees it);
* extraction ran, completed, and read nothing at all;
* extraction failed for a reason that has since gone away — a vision provider outage — and the
  document is still sitting in the chart unread.

The common thread is that none of these used to leave anything a clinician or an operator could
act on. A cancelled extraction rolled back the whole upload, leaving the scan's bytes on disk
with no row pointing at them. An empty extraction reported ``completed``. A failed one could be
re-uploaded forever without being re-read, because dedup matched and returned it untouched.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.document import Document
from app.models.user import Account
from app.services.document_service import DocumentService
from app.services.extraction.pipeline import ExtractionPipeline, ExtractionResultInternal
from app.services.extraction.text_parser import ParsedEntity, ParsedField
from tests.conftest import create_patient

SCAN = b"%PDF-1.4\nMEDICATIONS:\nGlycomet 500mg BD\n"
# PDF magic bytes, and nothing under a clinical section header for the deterministic parser to
# find. A scan whose text layer is empty behaves the same way.
UNREADABLE_SCAN = b"%PDF-1.4\nzzz qqq wgh\n"


async def _upload(client, patient_id, content=SCAN, name="rx.pdf"):
    return await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, content, "application/pdf")},
    )


async def _retry(client, patient_id, doc_id):
    return await client.post(f"/api/v1/patients/{patient_id}/documents/{doc_id}/extraction/retry")


async def _account_id(db) -> uuid.UUID:
    account_id = (await db.execute(select(Account.id))).scalars().first()
    assert account_id is not None
    return account_id


async def _status(db, doc_id) -> str:
    """The document's committed status, read as a bare column so no identity map can answer."""
    status = await db.scalar(select(Document.extraction_status).where(Document.id == doc_id))
    assert status is not None, "the document row is not there at all"
    return str(status)


def _exploding_pipeline():
    return patch(
        "app.services.document_service.ExtractionPipeline.run",
        side_effect=RuntimeError("extractor fell over"),
    )


async def _stuck_document(db, patient_id, *, age_minutes: float, name="stuck.pdf") -> Document:
    """A document frozen in ``processing``, as an interrupted extraction leaves one behind."""
    started = datetime.now(UTC) - timedelta(minutes=age_minutes)
    document = Document(
        patient_id=uuid.UUID(str(patient_id)),
        account_id=await _account_id(db),
        file_name=name,
        file_type="pdf",
        file_size_bytes=len(SCAN),
        storage_path="/nonexistent/never-read-by-the-reclaim",
        storage_hash_sha256=f"{abs(hash(name)):064x}"[:64],
        extraction_status="processing",
        extraction_started_at=started,
        extraction_metadata={},
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)
    return document


# ------------------------------------------------- the upload is durable before extraction runs


async def test_the_document_row_is_committed_before_extraction_starts(auth_client, db):
    """While the pipeline is still working, the document is already in the chart.

    This is what stops an interrupted extraction taking the upload with it. ``upload`` writes
    the file to storage first, so for as long as the row's transaction stayed open across
    extraction, anything that killed the request — a restart, a rolling deploy, a cancelled
    task — rolled the row back and left the bytes orphaned on disk with nothing pointing at
    them. The clinician saw a 500 holding the only copy of a prescription, and re-uploading
    could not recover it: dedup matches ``(patient_id, sha256)`` against rows that no longer
    existed, so every retry wrote another orphan.

    Asserted from a *different* session, which only sees committed state.
    """
    patient = await create_patient(auth_client)
    entered, release = threading.Event(), threading.Event()

    def _blocking_run(_self, _data, _file_type, *, raw_text=None):
        entered.set()
        release.wait(timeout=10)
        return ExtractionResultInternal([], None, False, None)

    with patch("app.services.document_service.ExtractionPipeline.run", new=_blocking_run):
        upload = asyncio.create_task(_upload(auth_client, patient["id"]))
        try:
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=6)
            db.expire_all()
            row = (
                await db.execute(
                    select(Document).where(Document.patient_id == uuid.UUID(patient["id"]))
                )
            ).scalar_one()
            assert row.extraction_status == "processing"
            assert row.extraction_started_at is not None
            assert row.storage_path
        finally:
            release.set()
        await upload


async def test_the_upload_is_audited_before_extraction_rather_than_after(auth_client, db):
    """`document_uploaded` reaches the audit log before the pipeline is entered.

    Not a cosmetic ordering. ``AuditService._lock`` serialises appends on
    ``pg_advisory_xact_lock``, which is held until the appending transaction *commits* — so an
    entry written before a long piece of work holds the global append lock for the length of
    that work. Extraction is the longest piece of work in the application: three vision
    providers at ``llm_request_timeout_seconds`` each, or a 60s Tesseract subprocess. With the
    append inside that transaction, every audit write in the deployment queued behind one
    clinician's unreadable scan — and since almost every write path audits, so did almost every
    write, including signing in.

    The lock is released by the same COMMIT that makes the row visible here, so a committed
    ``document_uploaded`` while extraction is still running is exactly the property being
    asserted. (The advisory lock itself is PostgreSQL-only; the commit ordering is not.)
    """
    from app.models.audit_log import AuditLog

    patient = await create_patient(auth_client)
    entered, release = threading.Event(), threading.Event()

    def _blocking_run(_self, _data, _file_type, *, raw_text=None):
        entered.set()
        release.wait(timeout=10)
        return ExtractionResultInternal([], None, False, None)

    with patch("app.services.document_service.ExtractionPipeline.run", new=_blocking_run):
        upload = asyncio.create_task(_upload(auth_client, patient["id"]))
        try:
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=6)
            db.expire_all()
            actions = set(
                (
                    await db.execute(
                        select(AuditLog.action).where(
                            AuditLog.patient_id == uuid.UUID(patient["id"])
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert "document_uploaded" in actions, (
                "the upload's audit entry is still uncommitted, so the global audit append "
                "lock is being held for the whole of extraction"
            )
            assert "extraction_completed" not in actions
        finally:
            release.set()
        await upload


async def test_a_cancelled_extraction_leaves_the_document_visible_rather_than_nothing(
    sessionmaker, auth_client, db
):
    """A ``BaseException`` out of the pipeline is not caught, and must not need to be.

    ``asyncio.CancelledError`` is what a worker shutdown or a client disconnect delivers into
    the handler, and it is not an ``Exception`` — ``_run_extraction``'s handler cannot see it,
    and there is nothing useful a task being torn down could do anyway. What matters is what it
    leaves behind: a committed ``processing`` row that names the file, rather than a rolled-back
    upload and an orphaned blob.
    """
    patient = await create_patient(auth_client)
    account_id = await _account_id(db)

    async with sessionmaker() as session:
        service = DocumentService(session)
        with patch(
            "app.services.document_service.ExtractionPipeline.run",
            side_effect=asyncio.CancelledError(),
        ):
            with pytest.raises(asyncio.CancelledError):
                await service.upload(
                    account_id=account_id,
                    patient_id=uuid.UUID(patient["id"]),
                    file_name="rx.pdf",
                    data=SCAN,
                )

    db.expire_all()
    row = (
        await db.execute(select(Document).where(Document.patient_id == uuid.UUID(patient["id"])))
    ).scalar_one()
    assert row.extraction_status == "processing"
    # And the bytes it names are actually there — row and blob did not diverge.
    assert await DocumentService(db).storage.exists_async(row.storage_path)


# ----------------------------------------------------------------- reclaiming a stuck document


async def test_a_document_stuck_in_processing_is_reclaimed_as_failed(auth_client, db):
    patient = await create_patient(auth_client)
    stuck = await _stuck_document(db, patient["id"], age_minutes=60)

    reclaimed = await DocumentService(db).reclaim_stalled_extractions(uuid.UUID(patient["id"]))

    assert reclaimed == 1
    await db.refresh(stuck)
    assert stuck.extraction_status == "failed"
    assert stuck.extraction_metadata["failure_type"] == "ExtractionInterrupted"
    assert stuck.extraction_metadata["entities"] == []
    assert stuck.extraction_completed_at is not None


async def test_an_extraction_that_has_only_just_started_is_left_alone(auth_client, db):
    """The reclaim must never cut off an extraction that is genuinely still running."""
    patient = await create_patient(auth_client)
    live = await _stuck_document(db, patient["id"], age_minutes=0)

    reclaimed = await DocumentService(db).reclaim_stalled_extractions(uuid.UUID(patient["id"]))

    assert reclaimed == 0
    assert await _status(db, live.id) == "processing"


async def test_a_processing_document_with_no_start_time_is_stalled_by_definition(auth_client, db):
    """Status and timestamp are written in one flush, so one without the other is a leftover."""
    patient = await create_patient(auth_client)
    stuck = await _stuck_document(db, patient["id"], age_minutes=0, name="no-clock.pdf")
    stuck.extraction_started_at = None
    await db.commit()

    assert await DocumentService(db).reclaim_stalled_extractions(uuid.UUID(patient["id"])) == 1
    assert await _status(db, stuck.id) == "failed"


async def test_the_reclaim_is_on_the_audit_trail(auth_client, db):
    """An operator asking "what happened to that scan" gets an answer, not a silent status flip."""
    patient = await create_patient(auth_client)
    stuck = await _stuck_document(db, patient["id"], age_minutes=60)
    await DocumentService(db).reclaim_stalled_extractions(uuid.UUID(patient["id"]))

    audit = await auth_client.get(f"/api/v1/patients/{patient['id']}/audit?limit=100")
    failures = [e for e in audit.json()["items"] if e["action"] == "extraction_failed"]
    assert [e["entity_id"] for e in failures] == [str(stuck.id)]
    assert failures[0]["payload"] == {"failure_type": "ExtractionInterrupted"}


async def test_listing_a_chart_reclaims_its_stuck_documents(auth_client, db):
    """The listing is where a stuck document is stared at, so it is where it gets closed out.

    Left to itself the row reads "being read" in the chart forever: it is not in the review
    queue, because it has no extraction; and not in any list of failures, because it never
    failed. That is the quietest way for a scan to go missing.

    The reclaim runs after the response is built — see the handler for why it cannot run before
    — so the chart is accurate from the next load, which is the one the clinician is looking at
    when they come back to the document that would not read.
    """
    patient = await create_patient(auth_client)
    stuck_id = (await _stuck_document(db, patient["id"], age_minutes=60)).id

    first = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")
    assert first.status_code == 200, first.text
    second = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")

    assert second.status_code == 200, second.text
    entry = next(d for d in second.json() if d["id"] == str(stuck_id))
    assert entry["extraction_status"] == "failed", (
        "the listing kept showing the clinician a document as still being read"
    )


async def test_one_chart_s_reclaim_does_not_touch_another_s(auth_client, db):
    first = await create_patient(auth_client)
    second = await create_patient(auth_client, full_name="Sunita Devi")
    mine = await _stuck_document(db, first["id"], age_minutes=60, name="mine.pdf")
    theirs = await _stuck_document(db, second["id"], age_minutes=60, name="theirs.pdf")

    await DocumentService(db).reclaim_stalled_extractions(uuid.UUID(first["id"]))

    assert await _status(db, mine.id) == "failed"
    assert await _status(db, theirs.id) == "processing"


async def test_a_failing_reclaim_does_not_take_the_document_listing_down(auth_client, db):
    """Housekeeping that cannot run is a worse chart; a chart that cannot be opened is worse.

    Two ways this bites, and the second is why the reclaim runs where it does. The obvious one
    is the exception propagating. The other is its recovery: ``Session.rollback`` expires
    *every* object in the session whatever ``expire_on_commit`` says, so a reclaim that fails
    part-way through a request leaves the ``Account`` the auth dependency loaded expired, and
    the next bare ``account.id`` reloads it from an async engine outside a greenlet context —
    ``MissingGreenlet``, and a 500 out of the error handling that exists to prevent one.
    """
    patient = await create_patient(auth_client)
    stuck_id = (await _stuck_document(db, patient["id"], age_minutes=60)).id

    with patch(
        "app.services.document_service.DocumentService._mark_extraction_failed",
        side_effect=RuntimeError("housekeeping exploded"),
    ):
        first = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")
        second = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert [d["id"] for d in second.json()] == [str(stuck_id)]
    assert second.json()[0]["extraction_status"] == "processing"


# --------------------------------------------------------------------------- retrying by hand


async def test_a_failed_extraction_can_be_retried_and_succeeds(auth_client):
    """The recovery path for a provider outage: the same file, read again, from storage."""
    patient = await create_patient(auth_client)
    with _exploding_pipeline():
        doc = (await _upload(auth_client, patient["id"])).json()
    assert doc["extraction_status"] == "failed"

    retried = await _retry(auth_client, patient["id"], doc["id"])

    assert retried.status_code == 200, retried.text
    assert retried.json()["id"] == doc["id"]
    assert retried.json()["extraction_status"] in {"completed", "needs_confirmation"}

    extraction = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/extraction"
    )
    assert extraction.json()["entities"], "the retry produced no entities from a readable scan"


async def test_the_retry_is_audited_with_the_status_it_replaced(auth_client):
    patient = await create_patient(auth_client)
    with _exploding_pipeline():
        doc = (await _upload(auth_client, patient["id"])).json()
    await _retry(auth_client, patient["id"], doc["id"])

    audit = await auth_client.get(f"/api/v1/patients/{patient['id']}/audit?limit=100")
    entries = [e for e in audit.json()["items"] if e["action"] == "extraction_retried"]
    assert len(entries) == 1
    assert entries[0]["entity_id"] == doc["id"]
    assert entries[0]["payload"] == {"previous_status": "failed"}


async def test_a_stalled_document_can_be_retried(auth_client, db):
    """A ``processing`` document past the stall window is not busy — it is abandoned."""
    patient = await create_patient(auth_client)
    with _exploding_pipeline():
        doc = (await _upload(auth_client, patient["id"])).json()
    # Put it back into the state an interrupted extraction leaves, with the same stored bytes.
    row = await db.get(Document, uuid.UUID(doc["id"]))
    assert row is not None
    row.extraction_status = "processing"
    row.extraction_started_at = datetime.now(UTC) - timedelta(minutes=60)
    await db.commit()

    retried = await _retry(auth_client, patient["id"], doc["id"])

    assert retried.status_code == 200, retried.text
    assert retried.json()["extraction_status"] in {"completed", "needs_confirmation"}


async def test_an_extraction_that_is_genuinely_running_is_not_retried(auth_client, db):
    """Two extractions of one document are not two opinions — both overwrite the same metadata."""
    patient = await create_patient(auth_client)
    live = await _stuck_document(db, patient["id"], age_minutes=0)

    resp = await _retry(auth_client, patient["id"], live.id)

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "extraction_in_progress"
    assert await _status(db, live.id) == "processing"


async def test_an_approved_extraction_is_never_re_read(auth_client, db):
    """Its entities are in the chart; a fresh unreviewed read would no longer describe them."""
    patient = await create_patient(auth_client)
    doc = (await _upload(auth_client, patient["id"])).json()
    approved = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approved.status_code == 200, approved.text

    resp = await _retry(auth_client, patient["id"], doc["id"])

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "extraction_already_approved"
    assert await _status(db, uuid.UUID(doc["id"])) == "completed"


async def test_retrying_an_unknown_document_is_a_404(auth_client):
    patient = await create_patient(auth_client)
    resp = await _retry(auth_client, patient["id"], uuid.uuid4())
    assert resp.status_code == 404
    assert resp.json()["code"] == "document_not_found"


async def test_another_account_cannot_retry_this_chart_s_document(auth_client, second_auth_client):
    """Same 404 as every other cross-tenant read: the API does not confirm the chart exists."""
    patient = await create_patient(auth_client)
    doc = (await _upload(auth_client, patient["id"])).json()

    resp = await _retry(second_auth_client, patient["id"], doc["id"])

    assert resp.status_code == 404, resp.text


async def test_retry_is_refused_after_consent_is_withdrawn(auth_client):
    """Re-reading a scan is new processing of personal data, so it needs a lawful basis (DPDP)."""
    patient = await create_patient(auth_client)
    with _exploding_pipeline():
        doc = (await _upload(auth_client, patient["id"])).json()
    withdrawn = await auth_client.patch(
        f"/api/v1/patients/{patient['id']}", json={"consent_given": False}
    )
    assert withdrawn.status_code == 200, withdrawn.text

    resp = await _retry(auth_client, patient["id"], doc["id"])

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "consent_withdrawn"


# ------------------------------------------------------- re-uploading the file is also a retry


async def test_re_uploading_a_failed_document_reads_it_again(auth_client, db):
    """The first thing anyone tries when a scan will not read, and it used to do nothing.

    Dedup returns the existing row for identical bytes, which is right — one document, not two.
    But it returned it *untouched*, so the retry a clinician actually performs was the one
    recovery guaranteed to have no effect.
    """
    patient = await create_patient(auth_client)
    with _exploding_pipeline():
        first = (await _upload(auth_client, patient["id"])).json()
    assert first["extraction_status"] == "failed"

    second = (await _upload(auth_client, patient["id"])).json()

    assert second["id"] == first["id"], "the re-upload created a second document"
    assert second["extraction_status"] in {"completed", "needs_confirmation"}
    rows = (
        (await db.execute(select(Document).where(Document.patient_id == uuid.UUID(patient["id"]))))
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_re_uploading_a_document_that_read_fine_does_not_pay_for_it_twice(auth_client):
    """Dedup still short-circuits the ordinary case — no second extraction, no second LLM call."""
    patient = await create_patient(auth_client)
    first = (await _upload(auth_client, patient["id"])).json()
    assert first["extraction_status"] in {"completed", "needs_confirmation"}

    with patch(
        "app.services.document_service.ExtractionPipeline.run",
        side_effect=AssertionError("extraction ran again for an already-extracted document"),
    ):
        second = (await _upload(auth_client, patient["id"])).json()

    assert second["id"] == first["id"]


async def test_re_uploading_an_approved_document_does_not_replace_its_extraction(auth_client):
    patient = await create_patient(auth_client)
    doc = (await _upload(auth_client, patient["id"])).json()
    approved = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approved.status_code == 200, approved.text

    with patch(
        "app.services.document_service.ExtractionPipeline.run",
        side_effect=AssertionError("an approved document was re-extracted"),
    ):
        again = (await _upload(auth_client, patient["id"])).json()

    assert again["id"] == doc["id"]
    assert again["extraction_status"] == "completed"


# -------------------------------------------------------- a document nothing could be read from


async def test_a_scan_nothing_was_read_from_is_not_reported_as_completed(auth_client):
    """`completed` says "read, and there was nothing chartable on it". That is a different fact.

    On the document list the two are indistinguishable, and the wrong one gets the scan filed
    as processed with nobody keying its drugs in. `needs_confirmation` says what is true: a
    human has to look at the original.
    """
    patient = await create_patient(auth_client)

    resp = await _upload(auth_client, patient["id"], content=UNREADABLE_SCAN, name="blank.pdf")

    assert resp.status_code == 201, resp.text
    assert resp.json()["extraction_status"] == "needs_confirmation"
    extraction = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{resp.json()['id']}/extraction"
    )
    assert extraction.json()["entities"] == []


async def test_approving_a_document_nothing_was_read_from_is_refused(auth_client, db):
    """Merging nothing and answering 200 tells the clinician the scan is in the record."""
    patient = await create_patient(auth_client)
    doc = (await _upload(auth_client, patient["id"], content=UNREADABLE_SCAN)).json()

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "nothing_to_approve"
    assert await _status(db, uuid.UUID(doc["id"])) == "needs_confirmation"


async def test_approving_a_failed_document_is_refused_and_keeps_the_failure_visible(
    auth_client, db
):
    """It used to answer 200 and set `completed`, erasing the one marker of an unread scan."""
    patient = await create_patient(auth_client)
    with _exploding_pipeline():
        doc = (await _upload(auth_client, patient["id"])).json()

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "nothing_to_approve"
    assert await _status(db, uuid.UUID(doc["id"])) == "failed", (
        "approving an unread document marked it completed, hiding the failure"
    )
    # And the way out is still open.
    assert (await _retry(auth_client, patient["id"], doc["id"])).status_code == 200


async def test_rejecting_every_entity_is_still_a_real_approval(auth_client, db):
    """ "I read this and none of it belongs in the chart" is a decision, and stays a 200."""
    patient = await create_patient(auth_client)
    doc = (await _upload(auth_client, patient["id"])).json()
    extraction = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/extraction"
    )
    count = len(extraction.json()["entities"])
    assert count

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": list(range(count))},
    )

    assert resp.status_code == 200, resp.text
    assert await _status(db, uuid.UUID(doc["id"])) == "completed"


# ------------------------------------------------- a vision read the confidence gate held back


def _unsure_vision(*, confidence: float = 0.3):
    """Patch the vision client to return entities the confidence gate will reject."""
    entities = [
        ParsedEntity(
            entity_type="medication",
            fields=[
                ParsedField(name="brand_name_raw", value="Glycomet", confidence=confidence),
                ParsedField(name="dose", value="500", confidence=confidence),
            ],
        )
    ]
    return (
        patch("app.services.extraction.pipeline.claude_client.is_available", return_value=True),
        patch(
            "app.services.extraction.pipeline.claude_client.extract",
            return_value=(entities, "prescription"),
        ),
        patch(
            "app.services.extraction.pipeline.claude_client.model_label",
            return_value="test-vision-model",
        ),
    )


def test_a_low_confidence_vision_read_survives_a_fallback_that_finds_nothing():
    """It was thrown away *before* the fallback was known to have failed.

    A scanned prescription the model reads at 0.3 mean confidence, in a PDF with no text layer
    for pypdf to recover: four drugs read doubtfully, and the document came back empty. Holding
    the doubtful read back in favour of the deterministic parser is right; discarding it when
    that parser produces nothing at all is not — there is then nothing to prefer it to.
    """
    available, extract, label = _unsure_vision()
    with available, extract, label:
        result = ExtractionPipeline().run(UNREADABLE_SCAN, "pdf")

    assert [e.entity_type for e in result.entities] == ["medication"]
    assert result.model == "test-vision-model"
    assert result.document_type == "prescription"


def test_the_deterministic_parser_still_wins_when_it_reads_something():
    """The confidence gate is not being removed — a doubtful read is only ever the last resort."""
    available, extract, label = _unsure_vision()
    with available, extract, label:
        result = ExtractionPipeline().run(SCAN, "pdf", raw_text=SCAN.decode())

    assert result.model is None, "the low-confidence vision read displaced a clean parse"
    assert result.entities


def test_a_confident_vision_read_is_used_as_it_stands():
    available, extract, label = _unsure_vision(confidence=0.95)
    with available, extract, label:
        result = ExtractionPipeline().run(SCAN, "pdf", raw_text=SCAN.decode())

    assert result.model == "test-vision-model"


async def test_a_doubtfully_read_document_goes_to_the_review_queue(auth_client):
    """Every field bands low, so nothing reaches the record until a clinician confirms it."""
    patient = await create_patient(auth_client)
    available, extract, label = _unsure_vision()

    with available, extract, label:
        resp = await _upload(auth_client, patient["id"], content=UNREADABLE_SCAN)

    assert resp.status_code == 201, resp.text
    assert resp.json()["extraction_status"] == "needs_confirmation"
    extraction = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{resp.json()['id']}/extraction"
    )
    body = extraction.json()
    assert body["confirmation_required_count"] == 2
    assert all(f["needs_confirmation"] for e in body["entities"] for f in e["fields"])
