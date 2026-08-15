"""Document upload, storage, extraction orchestration, and approval (P1-04/05/06)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.exceptions import (
    ConcurrentApprovalError,
    CorrectionNotApplicableError,
    DocumentNotFoundError,
    EntityNotMergeableError,
    ExtractionAlreadyApprovedError,
    ExtractionInProgressError,
    FileTooLargeError,
    NothingToApproveError,
    UnsupportedFileTypeError,
)
from app.models.document import Document
from app.models.patient import Patient
from app.schemas.document import (
    ExtractedEntity,
    ExtractionApproval,
    ExtractionField,
    ExtractionResult,
    FieldCorrection,
)
from app.services.audit_service import AuditDraft, AuditService
from app.services.extraction import ExtractionPipeline
from app.services.filetype import describe_unsupported, sniff_file_type
from app.services.graph_service import MERGEABLE_ENTITY_TYPES, GraphService
from app.services.lab_safety_service import LabSafetyService
from app.services.storage import compute_sha256_async, get_storage

logger = logging.getLogger(__name__)

# Comfortably longer than any real scan filename, short enough to be a bounded column value.
MAX_FILE_NAME_CHARS = 255

_BYTES_PER_MB = 1024 * 1024

# The one constraint an approval is expected to be able to lose a race to. Two spellings because
# the drivers do not agree on what to name in the message: asyncpg quotes the index
# ('duplicate key value violates unique constraint "uq_lab_results_observation"') while SQLite
# names the columns instead ('UNIQUE constraint failed: lab_results.patient_id,
# lab_results.dedup_key') even for a partial index. Matching either keeps the translation
# working on the test backend and the production one; test_a_duplicate_observation_is_a_409
# pins the SQLite form against the live driver so a wording change fails a test rather than
# silently turning a race into a 500.
#
# Deliberately narrow: any *other* constraint violated during a merge is a bug, not a race, and
# must keep propagating so it is logged with its traceback rather than reported to the clinician
# as a benign collision.
_OBSERVATION_CONFLICT_MARKERS = ("uq_lab_results_observation", "lab_results.dedup_key")


def _is_observation_conflict(exc: IntegrityError) -> bool:
    """Whether this integrity error is the duplicate-observation constraint firing."""
    message = str(exc.orig)
    return any(marker in message for marker in _OBSERVATION_CONFLICT_MARKERS)


def file_too_large_message(limit_bytes: int, actual_bytes: int | None = None) -> str:
    """Message for a rejected oversized upload, in megabytes and with a way out.

    ``actual_bytes`` is omitted on the streaming path, which aborts one chunk past the limit and
    so never learns the real size. Byte counts are what the limit is configured in, but nobody
    reading a toast mid-clinic converts 20971520 to anything; and "too large" without the fix
    (split the pages, or re-scan smaller) just sends the clinician back to the same scanner
    settings that produced the file.
    """
    limit_mb = limit_bytes / _BYTES_PER_MB
    if actual_bytes is None:
        size = f"This file is over the {limit_mb:.0f} MB limit."
    else:
        actual_mb = actual_bytes / _BYTES_PER_MB
        size = f"This file is {actual_mb:.1f} MB; the limit is {limit_mb:.0f} MB."
    return (
        f"{size} Upload the pages as separate files, or re-scan at 200-300 dpi in greyscale — "
        "that is enough resolution for the text to be read."
    )


def _band(score: float) -> str:
    if score >= settings.confirmation_confidence_threshold:
        return "high"
    if score >= settings.ocr_fallback_threshold:
        return "medium"
    return "low"


def _aware(value: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; treat a naive timestamp as UTC. See auth_service."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_stalled(document: Document) -> bool:
    """Whether a ``processing`` document has been abandoned rather than being worked on.

    Extraction runs inside the request that uploaded (or retried) the document and is bounded
    well under ``settings.extraction_stall_minutes`` — see the setting for the arithmetic. So
    once that long has passed there is no request left to finish it: the worker was restarted
    mid-extraction, the deploy rolled, or the client disconnected and the handler's task was
    cancelled between the commit that records the attempt and the commit that records its
    result. ``asyncio.CancelledError`` is not an ``Exception`` and so is deliberately not caught
    by ``_run_extraction``'s handler; this is what covers it instead.

    A ``processing`` row with no ``extraction_started_at`` is stalled by definition — the two
    are written in the same flush, so a row missing the timestamp predates that pairing and
    cannot be evidence of anything in flight.
    """
    if document.extraction_status != "processing":
        return False
    started = document.extraction_started_at
    if started is None:
        return True
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.extraction_stall_minutes)
    return _aware(started) < cutoff


def _is_retryable(document: Document) -> bool:
    """Whether re-reading this document would be a recovery rather than a regression.

    ``failed`` is the obvious one, and ``pending``/stalled-``processing`` are documents nothing
    is going to finish on its own. An extraction that has been *approved* is excluded: its
    entities are in the chart, and replacing the reviewed extraction with an unreviewed one
    would leave the review screen describing something other than what was merged.
    """
    if (document.extraction_metadata or {}).get("approved"):
        return False
    return document.extraction_status in {"failed", "pending"} or is_stalled(document)


class DocumentService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)
        self.storage = get_storage()
        self.pipeline = ExtractionPipeline()

    async def _get_patient(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
        from app.services.patient_service import PatientService

        return await PatientService(self.db).get(account_id, patient_id)

    async def _get_patient_for_processing(
        self, account_id: uuid.UUID, patient_id: uuid.UUID, *, for_update: bool = False
    ) -> Patient:
        """:meth:`_get_patient` plus the DPDP lawful-basis check.

        For ``upload`` and ``approve``, which put new personal data into the record. Listing
        and downloading what is already there stay on :meth:`_get_patient` — see
        :class:`~app.exceptions.ConsentWithdrawnError` on why withdrawal stops new processing
        rather than closing the chart.

        ``for_update`` locks the chart for the duration of the merge; only ``approve`` asks for
        it. See :meth:`PatientService.get`.
        """
        from app.services.patient_service import PatientService

        return await PatientService(self.db).get_for_processing(
            account_id, patient_id, for_update=for_update
        )

    async def upload(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        file_name: str,
        data: bytes,
    ) -> Document:
        # Ownership + existence, and the consent still being in force: this is the front door
        # for new personal data entering the record.
        await self._get_patient_for_processing(account_id, patient_id)

        # The client controls this string; it is persisted and echoed into the audit payload,
        # so cap it rather than storing an arbitrarily long name.
        file_name = (file_name or "upload")[:MAX_FILE_NAME_CHARS]

        if len(data) > settings.max_upload_bytes:
            raise FileTooLargeError(
                file_too_large_message(settings.max_upload_bytes, len(data)),
                detail=f"upload {len(data)}B over limit {settings.max_upload_bytes}B",
            )
        file_type = sniff_file_type(data)
        if file_type is None:
            digest = await compute_sha256_async(data)
            raise UnsupportedFileTypeError(
                f"{UnsupportedFileTypeError().message} {describe_unsupported(data)}",
                # ``detail`` is logged. It used to carry ``data[:12]!r`` — the leading bytes of
                # the rejected upload. Those are a signature only for formats we recognise;
                # for everything else (a note pasted into a .txt, a lab CSV, an .eml) they are
                # the file's first twelve characters of clinical text. Length and a hash prefix
                # identify the upload for support without reproducing any of it, and
                # describe_unsupported already names the format for the clinician.
                detail=f"unrecognised magic bytes, {len(data)}B, sha256={digest[:12]}",
            )

        sha256 = await compute_sha256_async(data)

        # Deduplicate: same bytes already uploaded for this patient.
        existing = await self.db.execute(
            select(Document).where(
                Document.patient_id == patient_id,
                Document.storage_hash_sha256 == sha256,
                Document.is_deleted.is_(False),
            )
        )
        dup = existing.scalar_one_or_none()
        if dup is not None:
            # Same bytes, same chart: one document, not two. But "already uploaded" and "already
            # read" are different facts, and returning the row unconditionally conflated them.
            # A document whose extraction failed — a provider outage, a worker restart — came
            # back from a re-upload exactly as it was, still `failed`, with no second attempt
            # made. Uploading the file again is the first thing anyone tries when a scan will
            # not read, and it was the one recovery guaranteed to do nothing.
            if _is_retryable(dup):
                await self._run_extraction(account_id, dup, data)
                await self.db.refresh(dup)
            return dup

        storage_path = await self.storage.write_async(str(patient_id), sha256, file_name, data)
        document = Document(
            patient_id=patient_id,
            account_id=account_id,
            file_name=file_name,
            file_type=file_type,
            file_size_bytes=len(data),
            storage_path=storage_path,
            storage_hash_sha256=sha256,
            extraction_status="pending",
        )
        self.db.add(document)
        await self.db.flush()
        await self.audit.record(
            action="document_uploaded",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="document",
            entity_id=document.id,
            # No file_name: uploaded scans are routinely named after the patient
            # ("ramesh_kumar_cbc_2026.pdf"), and audit_logs.payload is not encrypted. The name
            # is on the document row, which entity_id already points at, so recording it here
            # only duplicates a likely direct identifier into an immutable never-pruned table.
            payload={"file_type": file_type, "sha256": sha256},
        )
        # The upload is committed before extraction begins, and this is not a tidiness
        # preference — two things went wrong while one transaction spanned both.
        #
        # The audit log serialises appends on a *transaction-scoped* advisory lock, taken by the
        # `document_uploaded` record above and held until COMMIT (``AuditService._lock`` spells
        # this out: "a request that appends early and then does a second of clinical work holds
        # the global append lock for that second"). Extraction is not a second of work, it is up
        # to a minute and a half of it — three vision providers at `llm_request_timeout_seconds`
        # each, or a 60s Tesseract subprocess. Every audit append in the deployment queued behind
        # one clinician's unreadable scan, and since almost every write path audits, so did
        # almost every write: sign-ins, chart edits, reasoning runs. It also held a connection
        # from a pool of `database_pool_size` for the same stretch.
        #
        # And the document only existed once extraction was over. Anything that killed the
        # request before then — a worker restart, a rolling deploy, the client disconnecting —
        # rolled the row back and left the bytes on disk, which is the orphaned-blob failure
        # ``_run_extraction`` describes, reachable by every route except an exception out of the
        # pipeline. Committing here makes the upload durable the moment it is stored; a failure
        # during extraction now leaves a visible `processing` row that `reclaim_stalled_
        # extractions` finishes, instead of nothing at all.
        #
        # Phase 1: extraction still runs inline, in the same request. (Plan P1-05c upgrades this
        # to a Redis Stream worker with SSE progress; the pipeline interface is unchanged, and
        # the durable pre-extraction row is what that worker would pick up.)
        await self.db.commit()
        await self._run_extraction(account_id, document, data)
        await self.db.refresh(document)
        return document

    async def _run_extraction(self, account_id: uuid.UUID, document: Document, data: bytes) -> None:
        """Extract into ``document``, or mark it ``failed`` and leave the upload intact.

        The pipeline degrades internally — a vision error falls through to the deterministic
        parser, an unreadable scan yields zero entities — so reaching the handler below means
        something genuinely unexpected broke. That must not take the upload with it.

        It did. ``upload`` writes the file to storage *before* opening the transaction that
        holds the row, so an exception here rolled the ``documents`` row back while the bytes
        stayed on disk: an orphaned blob, no record of it, and a clinician looking at a 500
        with a scan that is nowhere in the chart. Re-uploading the same file could not recover
        it either — the dedup lookup is by ``(patient_id, sha256)`` against rows that no longer
        existed, so every retry wrote another orphan.

        ``failed`` was already in the ``ck_documents_extraction_status`` check constraint and
        nothing ever set it. This is the state it was for: the document is kept, the clinician
        can still download the original and enter the values by hand, and the failure is on the
        audit trail rather than only in a server log.

        Runs in its own transactions, before and after: the caller has already committed the
        row (see ``upload``), and the ``processing`` marker below is committed so that a request
        that dies mid-extraction leaves evidence of the attempt rather than a row that quietly
        reverts to ``pending`` and is never looked at again.
        """
        document.extraction_status = "processing"
        document.extraction_started_at = datetime.now(UTC)
        # Committed, not just flushed. A flush is invisible outside this transaction and is
        # undone by the rollback that follows a crash or a cancellation, so `processing` only
        # became a durable state once it was committed on its own — and a durable `processing`
        # row is the entire input to ``reclaim_stalled_extractions``. Without this, an
        # interrupted extraction leaves a `pending` document that nothing distinguishes from
        # one whose extraction has not started, and no code path ever starts it.
        await self.db.commit()

        try:
            # Off the event loop. ``ExtractionPipeline.run`` is synchronous and every branch of
            # it blocks for a long time: a vision call through a *sync* provider SDK
            # (``settings.llm_request_timeout_seconds``, 30s by default), a Tesseract
            # ``subprocess.run`` capped at 60s, and pypdf parsing that is CPU-bound on a large
            # scan. Called directly from this ``async def`` it held the only thread the worker
            # runs its event loop on, so one upload of one unreadable scan stalled *every*
            # concurrent request in that process — other clinicians' chart reads, the SSE
            # reasoning streams, and the ``/health/ready`` probe that tells the load balancer
            # this instance is alive — for up to a minute and a half.
            #
            # The pipeline is documented stateless and takes no session, so a worker thread is
            # safe: bytes in, result out, nothing shared. The transaction is still open across
            # this (extraction is synchronous within the upload by design until P1-05c moves it
            # to a worker), but the event loop is now free to serve everyone else while it runs.
            #
            # ``asyncio.to_thread`` rather than Starlette's ``run_in_threadpool`` to match
            # ``app.agents.util.call_llm``, which already takes the reasoning engine's identical
            # sync-SDK calls off the loop this way. One offload idiom, one thread pool.
            result = await asyncio.to_thread(self.pipeline.run, data, document.file_type)
        except Exception as exc:  # noqa: BLE001 — the upload survives any extraction failure
            await self._mark_extraction_failed(account_id, document, exc)
            return
        entities_meta = []
        confirmation_required = 0
        for ent in result.entities:
            fields_meta = []
            for f in ent.fields:
                band = _band(f.confidence)
                needs = band != "high"
                if needs:
                    confirmation_required += 1
                fields_meta.append(
                    {
                        "name": f.name,
                        "value": f.value,
                        "confidence": round(f.confidence, 3),
                        "confidence_band": band,
                        "needs_confirmation": needs,
                    }
                )
            entities_meta.append(
                {"entity_type": ent.entity_type, "fields": fields_meta, "region": None}
            )

        document.extraction_metadata = {
            "document_type": result.document_type,
            "model": result.model,
            "ocr_fallback_used": result.ocr_fallback_used,
            "entities": entities_meta,
            "confirmation_required_count": confirmation_required,
            "unreadable_line_count": result.unreadable_lines,
            "approved": False,
        }
        document.document_type = result.document_type
        document.extraction_model = result.model
        document.ocr_fallback_used = result.ocr_fallback_used
        document.extraction_completed_at = datetime.now(UTC)
        # A dropped line holds the document too. The review queue can only show what was read, so
        # on its own it is silent about a line that produced nothing: a prescription whose warfarin
        # line the scanner mangled shows three cleanly-read drugs, every field banded "high", and
        # "completed" — which reads as "this page has been fully understood". It has not been, and
        # the one thing standing between that and a chart missing a drug is whether the clinician
        # happens to compare the queue against the original.
        # Nothing read is not the same as nothing to do, and "completed" says the second. An
        # extraction that produced no entities at all — an unreadable scan, or every vision
        # provider down while the file had no text layer for the deterministic parser to work
        # on — used to land here as `completed` with an empty review queue, which on the
        # document list is indistinguishable from a page that was read and simply held nothing
        # chartable. The scan is then filed as processed and nobody keys its drugs in.
        #
        # `needs_confirmation` is what that document is: a human has to look at the original
        # before anything from it can be in the chart. It also keeps the document out of
        # `completed`, which is the state the approval path writes and the one that means the
        # clinician has signed the extraction off.
        document.extraction_status = (
            "needs_confirmation"
            if not result.entities or confirmation_required or result.unreadable_lines
            else "completed"
        )
        await self.db.flush()
        await self.audit.record(
            action="extraction_completed",
            account_id=account_id,
            patient_id=document.patient_id,
            entity_type="document",
            entity_id=document.id,
            payload={
                "entity_count": len(entities_meta),
                "confirmation_required_count": confirmation_required,
                "ocr_fallback_used": result.ocr_fallback_used,
                "status": document.extraction_status,
            },
        )
        await self.db.commit()

    async def _mark_extraction_failed(
        self,
        account_id: uuid.UUID,
        document: Document,
        exc: Exception | None = None,
        *,
        failure_type: str | None = None,
    ) -> None:
        """Record the failure on the document and on the audit trail. See ``_run_extraction``.

        Either an ``exc`` (the pipeline raised) or an explicit ``failure_type`` (the extraction
        was never finished by anyone — see ``reclaim_stalled_extractions``).
        """
        if failure_type is not None:
            kind = failure_type
        elif exc is not None:
            kind = type(exc).__name__
        else:  # pragma: no cover — both callers supply one or the other
            kind = "UnknownError"
        logger.exception(
            "Extraction failed for document %s (file_type=%s, failure_type=%s); the document is "
            "kept and marked failed for manual entry.",
            document.id,
            document.file_type,
            kind,
            # No live exception on the reclaim path, and logger.exception would then log
            # "NoneType: None" as the traceback.
            exc_info=exc is not None,
        )
        document.extraction_status = "failed"
        document.extraction_completed_at = datetime.now(UTC)
        # An empty entity list, not a missing key: build_extraction_result and the approval path
        # both read `entities`, and a failed document is still fetchable and still listed.
        document.extraction_metadata = {
            "document_type": None,
            "model": None,
            "ocr_fallback_used": False,
            "entities": [],
            "confirmation_required_count": 0,
            "unreadable_line_count": 0,
            "approved": False,
            # Type only. The exception's message can quote the document's contents (a parser
            # error carries the text it choked on), and extraction_metadata is not encrypted.
            "failure_type": kind,
        }
        await self.db.flush()
        await self.audit.record(
            action="extraction_failed",
            account_id=account_id,
            patient_id=document.patient_id,
            entity_type="document",
            entity_id=document.id,
            payload={"failure_type": kind},
        )
        await self.db.commit()

    async def reclaim_stalled_extractions(self, patient_id: uuid.UUID) -> int:
        """Close out this chart's abandoned ``processing`` documents. Returns how many.

        The class of document this exists for: uploaded, stored, recorded, extraction begun —
        and then nothing, because the request that was doing the extracting is gone. A worker
        restart, a rolling deploy, an OOM kill, or a cancellation (``asyncio.CancelledError`` is
        a ``BaseException``, so ``_run_extraction``'s ``except Exception`` does not see it, and
        deliberately: there is nothing useful to do inside a task that is being torn down).
        Left alone the row reads "being read" in the chart forever, is not in the review queue
        because it has no extraction, and is not in any list of failures because it never
        failed. It is the quietest way for a scan to go missing.

        Reclaiming means marking it ``failed`` with ``failure_type: "ExtractionInterrupted"``,
        which puts it on the audit trail, tells the clinician the truth on the document list,
        and — because ``_is_retryable`` reads that status — makes re-uploading the file or
        pressing Retry actually re-read it.

        Scoped to one patient and hung off the document listing rather than run by a scheduler,
        for the reason ``AuthService.sweep_sessions_if_due`` gives: this deployment has no
        scheduler, and the chart someone is looking at is where a stuck document matters. It
        commits its own work and swallows its own failures — a chart that cannot be listed
        because housekeeping failed would be a strictly worse outcome than an unreclaimed row.

        **Call it after the caller has finished with its own session**, which for the listing
        route means after the response models are built. Two reasons, and the first is not
        obvious: ``Session.rollback`` expires *every* object in the session regardless of
        ``expire_on_commit``, so the failure path below silently expires whatever the request is
        holding — including the ``Account`` the authentication dependency loaded. The next plain
        attribute read on it (``account.id``, on the line after this one used to be called) then
        tries to reload from an async engine outside a greenlet context and raises
        ``MissingGreenlet``: a 500, from the error handling that exists to prevent one. The
        second is the ordinary one — committing inside someone else's transaction commits their
        half-finished work too.

        The cost is that a chart with a stuck document shows it as ``processing`` once more
        before the reclaim lands. That is the right way round: a listing that is one refresh
        behind on abandoned housekeeping beats a listing that 500s.
        """
        try:
            rows = (
                (
                    await self.db.execute(
                        select(Document).where(
                            Document.patient_id == patient_id,
                            Document.is_deleted.is_(False),
                            Document.extraction_status == "processing",
                        )
                    )
                )
                .scalars()
                .all()
            )
            # Filtered in Python, not in SQL: `extraction_started_at` comes back tz-naive from
            # SQLite and comparing it against an aware cutoff raises. The candidate set is every
            # in-flight extraction for one patient, which is zero or one in ordinary operation.
            stalled = [row for row in rows if is_stalled(row)]
            for document in stalled:
                logger.warning(
                    "Reclaiming document %s: extraction has been in `processing` since %s and "
                    "no request is finishing it.",
                    document.id,
                    document.extraction_started_at,
                )
                await self._mark_extraction_failed(
                    document.account_id, document, failure_type="ExtractionInterrupted"
                )
            return len(stalled)
        except Exception:
            await self.db.rollback()
            logger.warning(
                "Reclaiming stalled extractions for patient %s failed; the documents were left "
                "as they were.",
                patient_id,
                exc_info=True,
            )
            return 0

    async def retry_extraction(
        self, *, account_id: uuid.UUID, patient_id: uuid.UUID, doc_id: uuid.UUID
    ) -> Document:
        """Read a stored document again. The recovery path for an extraction that did not work.

        Extraction can fail for reasons that have nothing to do with the scan — every vision
        provider down, a worker restarted mid-page — and until this existed there was no way
        back from any of them. The document sat in the chart as ``failed`` (or, worse,
        ``processing``) permanently, and re-uploading the identical file matched the dedup
        lookup and returned the same untouched row.

        Refused for a document whose extraction is already approved, and for one that is
        genuinely still being read. Everything else is re-read, including a successful
        extraction the clinician does not trust — nothing from it is in the record until they
        approve it, so replacing it costs nothing.
        """
        document = await self.get(account_id, patient_id, doc_id)
        if (document.extraction_metadata or {}).get("approved"):
            raise ExtractionAlreadyApprovedError()
        if document.extraction_status == "processing" and not is_stalled(document):
            raise ExtractionInProgressError(
                detail=f"extraction started at {document.extraction_started_at}"
            )
        # Re-reading a scan puts new extracted personal data on the record, so it is subject to
        # the same DPDP lawful-basis check as the upload that first stored it.
        await self._get_patient_for_processing(account_id, patient_id)

        data = await self.storage.read_async(document.storage_path)
        await self.audit.record(
            action="extraction_retried",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="document",
            entity_id=document.id,
            payload={"previous_status": document.extraction_status},
        )
        # Committed before the pipeline runs, for the reason `upload` sets out at length: the
        # audit append lock is transaction-scoped, and holding it across a vision call would
        # queue every other audit append in the deployment behind this one retry.
        await self.db.commit()

        await self._run_extraction(account_id, document, data)
        await self.db.refresh(document)
        return document

    async def get(
        self,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        doc_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Document:
        """The document, or ``DocumentNotFoundError``.

        ``for_update`` takes a row lock, and only :meth:`approve` asks for one. Approving merges
        the stored extraction into the chart, and the merge decides what to insert by reading
        what is already there — a read-then-insert window that two approvals of the same
        document can both sit inside. Sequentially the second one sees the first one's rows and
        merges nothing, which is what makes a double-clicked Approve button harmless; overlapping,
        both read an empty chart and both insert, and the patient ends up with one blood draw
        recorded twice. The lock makes the second approval wait for the first to commit, so its
        read happens after the write it needs to see (READ COMMITTED takes a fresh snapshot per
        statement, and the lock is released at commit).

        PostgreSQL only, in the same sense as ``AuditService._lock``: SQLAlchemy's SQLite dialect
        silently drops ``FOR UPDATE``, so on a SQLite deployment two genuinely simultaneous
        approvals of one document can still both merge. Sequential re-approval — the case that
        actually happens, and the one the deduplication in ``GraphService`` covers — is safe on
        every backend.
        """
        statement = select(Document).where(
            Document.id == doc_id,
            Document.patient_id == patient_id,
            Document.account_id == account_id,
            Document.is_deleted.is_(False),
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.db.execute(statement)
        document = result.scalar_one_or_none()
        if document is None:
            raise DocumentNotFoundError()
        return document

    async def list(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> list[Document]:
        # Ownership check first: every other patient-scoped route 404s for a patient the
        # caller does not own, and listing must not be the one endpoint that answers 200
        # (with an empty list) for someone else's patient id.
        await self._get_patient(account_id, patient_id)
        result = await self.db.execute(
            select(Document)
            .where(
                Document.patient_id == patient_id,
                Document.account_id == account_id,
                Document.is_deleted.is_(False),
            )
            .order_by(Document.created_at.desc())
        )
        return list(result.scalars().all())

    def build_extraction_result(self, document: Document) -> ExtractionResult:
        meta = document.extraction_metadata or {}
        entities = [
            ExtractedEntity(
                entity_type=ent["entity_type"],
                fields=[ExtractionField(**f) for f in ent["fields"]],
                region=ent.get("region"),
            )
            for ent in meta.get("entities", [])
        ]
        return ExtractionResult(
            document_id=document.id,
            document_type=meta.get("document_type"),
            model=meta.get("model"),
            ocr_fallback_used=meta.get("ocr_fallback_used", False),
            entities=entities,
            confirmation_required_count=meta.get("confirmation_required_count", 0),
            # Absent on documents extracted before the parser began counting; those were not
            # known to be complete either, so the default understates rather than reassures.
            unreadable_line_count=meta.get("unreadable_line_count", 0),
        )

    async def approve(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        doc_id: uuid.UUID,
        approval: ExtractionApproval,
    ) -> dict[str, int]:
        # Locked: the merge below reads the chart to decide what is already in it. See `get`.
        document = await self.get(account_id, patient_id, doc_id, for_update=True)
        # Approval is what merges the extracted entities into the chart, so it is new
        # processing even though the file arrived earlier.
        #
        # Locked too, and on the *chart* rather than the document. The document lock above only
        # serialises two approvals of the same document; two different prescriptions merging into
        # one record at the same moment never contended, and they are not independent — see
        # `PatientService.get` for the discontinuation this lost. The order (document, then
        # patient) is fixed here and this is the only caller of either lock, so there is no cycle
        # for two approvals to deadlock on.
        patient = await self._get_patient_for_processing(account_id, patient_id, for_update=True)
        meta = dict(document.extraction_metadata or {})
        raw_entities = meta.get("entities", [])

        # There is a difference between "I reviewed this and rejected all of it" and "there was
        # never anything here", and only the first is an approval. An extraction that produced
        # no entities — it failed, or it ran and read nothing off an unreadable scan — used to
        # answer 200 with `{"merged": {}}` and set `extraction_status` to `completed`, which
        # both told the clinician the document was in the record and destroyed the one marker
        # showing it had never been read. See NothingToApproveError; the way forward is
        # ../extraction/retry, or entering the values by hand.
        if not raw_entities:
            raise NothingToApproveError(
                detail=f"extraction_status={document.extraction_status}, no extracted entities"
            )

        # Resolve every correction to the field it names *before* applying any of them. A
        # correction that matched nothing used to be dropped and the approval still answered
        # 200, so a clinician who retyped a dose the extractor had misread saw "approved" while
        # the original value merged into the chart — and a dose is one of the inputs the
        # deterministic safety checks read. Resolving first also makes the refusal all-or-
        # nothing: no half-corrected extraction is left behind for the retry to build on.
        targets: list[tuple[FieldCorrection, dict]] = []
        unapplicable: list[str] = []
        for corr in approval.corrections:
            entity = (
                raw_entities[corr.entity_index]
                if 0 <= corr.entity_index < len(raw_entities)
                else None
            )
            field = (
                next(
                    (f for f in entity.get("fields", []) if f["name"] == corr.field_name),
                    None,
                )
                if entity is not None
                else None
            )
            if field is None:
                unapplicable.append(f"{corr.entity_index}.{corr.field_name}")
            else:
                targets.append((corr, field))

        if unapplicable:
            raise CorrectionNotApplicableError(
                # Field *names* and positions, never the values: this message is rendered to
                # the clinician and also logged, and the value is the clinical datum.
                detail=f"no extracted field for {', '.join(unapplicable)}"
            )

        # Apply clinician corrections to the stored extraction (audited per field).
        corrected_fields: list[dict] = []
        for corr, field in targets:
            field["value"] = corr.value
            field["confidence"] = 1.0
            field["confidence_band"] = "high"
            field["needs_confirmation"] = False
            corrected_fields.append({"entity_index": corr.entity_index, "field": corr.field_name})

        # Build the merge payload, skipping rejected entities.
        rejected = set(approval.rejected_entity_indexes)

        # An entity of a type the graph cannot chart is refused, not merged-and-forgotten. Same
        # argument as CorrectionNotApplicableError above, and the same failure it was written
        # for: the approval used to answer 200 with the entity nowhere in the counts, and the
        # clinician who ticked "include in the record" read that as the record including it.
        #
        # Refusing names the entity so it can be rejected and the rest of the document approved.
        # The alternative — dropping it quietly — is what left every encounter on every
        # discharge summary out of the chart without one line of evidence that it had happened.
        #
        # Reachable only for documents extracted before the parser began refusing these types;
        # every type it can emit today is one ``GraphService.merge_entities`` dispatches on.
        unmergeable = [
            f"{idx} ({ent.get('entity_type')!r})"
            for idx, ent in enumerate(raw_entities)
            if idx not in rejected and ent.get("entity_type") not in MERGEABLE_ENTITY_TYPES
        ]
        if unmergeable:
            raise EntityNotMergeableError(
                detail=(
                    "nothing in the record can hold these extracted items, so they cannot be "
                    f"approved: {', '.join(unmergeable)}. Reject them to approve the rest."
                )
            )

        merge_payload: list[dict] = []
        for idx, ent in enumerate(raw_entities):
            if idx in rejected:
                continue
            fields = {f["name"]: f["value"] for f in ent["fields"]}
            confidence = {f["name"]: f["confidence"] for f in ent["fields"]}
            merge_payload.append(
                {
                    "entity_type": ent["entity_type"],
                    "fields": fields,
                    "confidence": confidence,
                    "region": ent.get("region"),
                }
            )

        if corrected_fields:
            await self.audit.record(
                action="field_corrected",
                account_id=account_id,
                patient_id=patient_id,
                entity_type="document",
                entity_id=document.id,
                payload={"corrections": corrected_fields},
            )

        try:
            counts = await GraphService(self.db).merge_entities(
                patient=patient, document=document, entities=merge_payload
            )
        except IntegrityError as exc:
            # uq_lab_results_observation: another approval of this document inserted the same
            # observation between this one's dedup read and its flush. The lock in `get` closes
            # that window on PostgreSQL and is a no-op on SQLite, so the constraint is what
            # actually holds here. See the index on LabResult for the whole argument.
            if _is_observation_conflict(exc):
                await self.db.rollback()
                raise ConcurrentApprovalError(detail=str(exc.orig)) from exc
            raise

        if counts.get("lab_results"):
            # Deterministic, offline critical/panic-value backstop — runs regardless of what
            # reference range (if any) the source document carried. See lab_safety_service.
            await LabSafetyService(self.db).check_patient_labs(
                account_id=account_id, patient_id=patient_id
            )

        meta["approved"] = True
        meta["entities"] = raw_entities
        document.extraction_metadata = meta
        document.extraction_status = "completed"
        await self.db.flush()

        await self.audit.record_many(
            [
                AuditDraft(
                    action="extraction_approved",
                    account_id=account_id,
                    patient_id=patient_id,
                    entity_type="document",
                    entity_id=document.id,
                    payload={"merged": counts, "rejected_count": len(rejected)},
                ),
                AuditDraft(
                    action="graph_merged",
                    account_id=account_id,
                    patient_id=patient_id,
                    entity_type="patient",
                    entity_id=patient_id,
                    payload=counts,
                ),
            ]
        )
        await self.db.commit()
        return counts
