"""One stored file per chart: ``uq_documents_patient_hash`` and the upload path around it.

``DocumentService.upload`` deduplicates by reading the chart for a live document with the
uploaded bytes and inserting one only if there is none. Two uploads of the same file that
overlap inside that read-then-insert window both find nothing and both write a row — a
double-clicked button, or a client retrying after a timeout while the first request is still
running.

The second row is not the damage. The lookup used ``scalar_one_or_none``, which raises
``MultipleResultsFound`` the moment two rows match, so *every* later upload of that file answered
500 — permanently, and including the re-upload that is the documented recovery for a scan that
would not read. The chart held one document twice and the file could not be put in again short
of altering its bytes.

These tests pin both halves: the index that stops the duplicate being written, and the lookup
that stays working over a database which already contains one.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.document import Document
from app.services.document_service import DocumentService, _aware, _is_upload_conflict
from app.services.storage import get_storage
from tests.conftest import create_patient
from tests.test_documents import PRESCRIPTION


async def _upload(client, patient_id, content=PRESCRIPTION, name="rx.pdf"):
    return await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, content, "application/pdf")},
    )


async def _sole_document(db) -> Document:
    return (
        (await db.execute(select(Document).where(Document.is_deleted.is_(False)))).scalars().one()
    )


def _twin(row: Document, **overrides) -> Document:
    """A second document row for the same chart and the same bytes — the race's losing INSERT."""
    fields = {
        "patient_id": row.patient_id,
        "account_id": row.account_id,
        "file_name": row.file_name,
        "file_type": row.file_type,
        "file_size_bytes": row.file_size_bytes,
        "storage_path": row.storage_path,
        "storage_hash_sha256": row.storage_hash_sha256,
        "extraction_status": "pending",
    }
    return Document(**{**fields, **overrides})


# ------------------------------------------------------------ the constraint


@pytest.mark.asyncio
async def test_the_same_file_cannot_be_charted_twice_for_one_patient(auth_client, db):
    """The durable half. Without it the losing INSERT of a raced upload simply succeeds."""
    patient = await create_patient(auth_client)
    assert (await _upload(auth_client, patient["id"])).status_code == 201

    db.add(_twin(await _sole_document(db)))
    with pytest.raises(IntegrityError) as caught:
        await db.flush()

    assert _is_upload_conflict(caught.value), (
        "the driver's wording for uq_documents_patient_hash is not one upload recognises, so a "
        "lost race would surface as a 500"
    )


@pytest.mark.asyncio
async def test_the_same_file_in_two_different_charts_is_two_documents(auth_client, db):
    """The constraint is per patient. A hospital form letter sent to two patients is two scans,
    and a chart-wide uniqueness rule would silently drop the second patient's copy."""
    one = await create_patient(auth_client)
    two = await create_patient(auth_client, full_name="Second Patient")

    assert (await _upload(auth_client, one["id"])).status_code == 201
    assert (await _upload(auth_client, two["id"])).status_code == 201

    rows = (
        (await db.execute(select(Document).where(Document.is_deleted.is_(False)))).scalars().all()
    )
    assert len({row.patient_id for row in rows}) == 2
    assert len({row.storage_hash_sha256 for row in rows}) == 1


@pytest.mark.asyncio
async def test_a_withdrawn_document_does_not_forbid_uploading_the_file_again(auth_client, db):
    """The index is partial on ``is_deleted`` for exactly this: a soft-deleted document must not
    make its file permanently unchartable. Same argument as ``uq_lab_results_observation``."""
    patient = await create_patient(auth_client)
    assert (await _upload(auth_client, patient["id"])).status_code == 201

    document = await _sole_document(db)
    document.is_deleted = True
    await db.commit()

    again = await _upload(auth_client, patient["id"])
    assert again.status_code == 201
    assert again.json()["id"] != str(document.id)


# ------------------------------------------------------------ reading a chart that already has one


@pytest.mark.asyncio
async def test_a_chart_that_already_holds_a_duplicate_can_still_be_uploaded_to(auth_client, db):
    """The regression this whole file exists for.

    ``scalar_one_or_none`` over two matching rows raises ``MultipleResultsFound``, which the
    error handler turns into a 500 — so a chart that lost the race once could never receive that
    file again, and the re-upload that is the documented recovery for an unread scan was the
    thing most certain to fail.

    Staged with the index dropped, because that is the only state in which the pair can exist:
    a database restored from a backup taken before 0026, or one an operator has just un-retired
    a row in. The constraint stops *new* duplicates; the lookup has to stay working over an old
    one, so it is read with an ORDER BY and a LIMIT rather than with ``scalar_one_or_none``.
    """
    patient = await create_patient(auth_client)
    assert (await _upload(auth_client, patient["id"])).status_code == 201

    original = await _sole_document(db)
    await db.execute(text("DROP INDEX uq_documents_patient_hash"))
    # Stamped a second later rather than left to the column default. ``created_at`` is
    # ``server_default=func.now()``, which on SQLite is CURRENT_TIMESTAMP at one-second
    # resolution, so a twin inserted moments after the original ties with it — and the assertion
    # below would then be decided by which random UUID happened to sort first.
    db.add(
        _twin(
            original,
            id=uuid.uuid4(),
            created_at=_aware(original.created_at) + timedelta(seconds=1),
        )
    )
    await db.commit()

    again = await _upload(auth_client, patient["id"])

    assert again.status_code == 201, again.text
    # And it answers with *the* document — the earliest of the group, the same row migration 0026
    # keeps — rather than with whichever the scan happened to return first.
    assert again.json()["id"] == str(original.id)


# ------------------------------------------------------------ losing the race


@pytest.mark.asyncio
async def test_an_upload_that_loses_the_race_answers_with_the_winning_document(
    auth_client, db, monkeypatch
):
    """End to end, with the race staged so the outcome is deterministic.

    The window cannot be opened for real on the shared in-memory test bind — one connection, so
    two sessions cannot genuinely overlap. It is staged instead by blinding the dedup read once:
    the winner's row is already committed, and the loser proceeds to its INSERT exactly as it
    would have done had it read the chart a moment before that commit landed.

    A 409 would be as wrong an answer here as the 500 was. There is nothing for the clinician to
    decide and nothing they did wrong, and the document they were uploading *is* in the chart —
    which is what the second of two double-clicks should be told.
    """
    patient = await create_patient(auth_client)
    assert (await _upload(auth_client, patient["id"], name="winner.pdf")).status_code == 201
    winner = await _sole_document(db)

    real_lookup = DocumentService._already_uploaded
    blinded: list[int] = []

    async def blind_once(self, patient_id, sha256):
        blinded.append(1)
        if len(blinded) == 1:
            return None
        return await real_lookup(self, patient_id, sha256)

    monkeypatch.setattr(DocumentService, "_already_uploaded", blind_once)

    resp = await _upload(auth_client, patient["id"], name="loser.pdf")

    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == str(winner.id), (
        "the loser must answer with the document the winner charted, not with a row of its own"
    )
    assert resp.json()["file_name"] == "winner.pdf"
    live = (
        (
            await db.execute(
                select(Document).where(
                    Document.patient_id == uuid.UUID(patient["id"]),
                    Document.is_deleted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(live) == 1, "the losing INSERT must not have been written"


@pytest.mark.asyncio
async def test_only_the_upload_constraint_is_treated_as_a_lost_race():
    """Anything else that violates a constraint while storing a document is a bug, and must stay
    a 500 with its traceback rather than be reported as a benign collision."""

    class _Fake:
        def __init__(self, message: str) -> None:
            self.orig = message

    assert not _is_upload_conflict(_Fake("FOREIGN KEY constraint failed"))  # type: ignore[arg-type]
    assert not _is_upload_conflict(
        _Fake("UNIQUE constraint failed: audit_logs.sequence")  # type: ignore[arg-type]
    )
    assert not _is_upload_conflict(
        _Fake('duplicate key value violates unique constraint "uq_lab_results_observation"')  # type: ignore[arg-type]
    )
    assert _is_upload_conflict(
        _Fake('duplicate key value violates unique constraint "uq_documents_patient_hash"')  # type: ignore[arg-type]
    ), "the PostgreSQL wording — the one that matters in production — is not matched"
    assert _is_upload_conflict(
        _Fake(  # type: ignore[arg-type]
            "UNIQUE constraint failed: documents.patient_id, documents.storage_hash_sha256"
        )
    ), "the SQLite wording — the one the test suite runs on — is not matched"


# ------------------------------------------------------------ the bytes left behind


@pytest.mark.asyncio
async def test_the_losing_uploads_bytes_are_not_left_orphaned_on_disk(auth_client, db):
    """``upload`` writes the file before it inserts the row, so a rolled-back INSERT leaves bytes
    nothing references.

    Usually the winner covers them — same patient, same digest, same path. Not always: the stored
    name carries the *uploaded* name's extension, so the same scan sent as ``rx.pdf`` and as
    ``rx.PDF`` hashes identically and lands at two paths. The loser's would never be read and
    never be cleaned up.
    """
    storage = get_storage()
    patient = await create_patient(auth_client)
    assert (await _upload(auth_client, patient["id"], name="rx.pdf")).status_code == 201
    winner_path = (await _sole_document(db)).storage_path

    service = DocumentService(db)
    orphan = await storage.write_async(patient["id"], "deadbeef" * 8, "scan.PDF", PRESCRIPTION)
    assert storage.exists(orphan)

    await service._discard_unreferenced(orphan)

    assert not storage.exists(orphan)
    assert storage.exists(winner_path), "the document still in the chart must keep its bytes"


@pytest.mark.asyncio
async def test_bytes_a_live_document_still_points_at_are_never_discarded(auth_client, db):
    """The guard is "does any live row name this exact path", not an assumption about how paths
    are built. An orphaned blob costs disk; a document row pointing at bytes that are gone costs
    a clinician the only copy of the original scan."""
    storage = get_storage()
    patient = await create_patient(auth_client)
    assert (await _upload(auth_client, patient["id"])).status_code == 201
    path = (await _sole_document(db)).storage_path

    await DocumentService(db)._discard_unreferenced(path)

    assert storage.exists(path)


@pytest.mark.asyncio
async def test_a_withdrawn_documents_original_scan_is_never_discarded(auth_client, db):
    """The guard reads past ``is_deleted``, unlike every other document query on this service.

    Withdrawing a document is a soft delete precisely so the original survives it — the audit
    trail's hash chain references the row, and the scan is the source of every value merged from
    it. A cleanup that only looked at live rows would delete the one copy of a withdrawn
    document's file.
    """
    storage = get_storage()
    patient = await create_patient(auth_client)
    assert (await _upload(auth_client, patient["id"])).status_code == 201

    document = await _sole_document(db)
    path = document.storage_path
    document.is_deleted = True
    await db.commit()

    await DocumentService(db)._discard_unreferenced(path)

    assert storage.exists(path)


@pytest.mark.asyncio
async def test_discarding_never_fails_the_upload_that_already_succeeded(db, caplog):
    """Housekeeping runs after the document has been stored and answered for. A failure in it is
    logged and swallowed — turning a successful upload into an error would be strictly worse
    than leaving a file on disk."""
    with caplog.at_level("WARNING"):
        # Refused by the storage root confinement, which raises.
        await DocumentService(db)._discard_unreferenced("/etc/passwd")

    assert "orphaned on disk" in caplog.text


# ------------------------------------------------------------ storage.delete itself


def test_storage_delete_refuses_a_path_outside_the_storage_root():
    """``storage_path`` is a plain column. If it ever stops being trustworthy — a half-restored
    backup, a hand-edited row — the blast radius of a delete must be nothing, not an arbitrary
    file removal."""
    from app.exceptions import DocumentNotFoundError

    with pytest.raises(DocumentNotFoundError):
        get_storage().delete("/etc/passwd")


def test_storage_delete_is_content_with_a_file_that_is_already_gone():
    """The caller's job is to leave nothing behind, and for a missing file that is already true."""
    storage = get_storage()
    path = storage.write("patient-x", "f" * 64, "scan.pdf", b"%PDF-1.4\n")

    assert storage.delete(path) is True
    assert storage.delete(path) is False


# ------------------------------------------------------------ re-uploading to re-read


@pytest.mark.asyncio
async def test_re_uploading_a_scan_that_did_not_read_is_on_the_audit_trail(auth_client, db):
    """A re-upload that re-runs extraction sends the scan to the model again. That is new
    extracted personal data derived from patient records, on a route whose ordinary answer
    writes no audit record at all — so without this, nothing named who asked for it."""
    from app.models.audit_log import AuditLog

    patient = await create_patient(auth_client)
    assert (await _upload(auth_client, patient["id"])).status_code == 201

    document = await _sole_document(db)
    document.extraction_status = "failed"
    document.extraction_metadata = {"approved": False, "failure_type": "ProviderUnavailable"}
    await db.commit()

    again = await _upload(auth_client, patient["id"])
    assert again.status_code == 201
    assert again.json()["id"] == str(document.id)

    entries = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action == "extraction_retried")
                .order_by(AuditLog.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(entries) == 1
    assert entries[0].payload["via"] == "re_upload"
    assert entries[0].payload["previous_status"] == "failed"


@pytest.mark.asyncio
async def test_re_uploading_a_document_already_read_does_not_read_it_again(auth_client, db):
    """A successful extraction is not re-run by an accidental second upload: it costs an LLM
    call, and the clinician may already be part-way through reviewing the one that is there."""
    from app.models.audit_log import AuditLog

    patient = await create_patient(auth_client)
    assert (await _upload(auth_client, patient["id"])).status_code == 201

    assert (await _upload(auth_client, patient["id"])).status_code == 201

    retried = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "extraction_retried")))
        .scalars()
        .all()
    )
    assert retried == []
