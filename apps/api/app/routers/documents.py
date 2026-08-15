"""Document upload, listing, extraction review, and approval routes."""

from __future__ import annotations

import re
import unicodedata
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db
from app.dependencies import get_current_account, rate_limit
from app.exceptions import FileTooLargeError
from app.models.user import Account
from app.openapi import PATIENT_ERRORS, errors
from app.schemas.document import (
    DocumentResponse,
    ExtractionApproval,
    ExtractionResult,
)
from app.services.audit_service import AuditService
from app.services.document_service import DocumentService, file_too_large_message

router = APIRouter(prefix="/patients/{patient_id}/documents", tags=["documents"])

_MIME_BY_TYPE = {
    "pdf": "application/pdf",
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/heic": "image/heic",
}


_UPLOAD_CHUNK_BYTES = 1024 * 1024

# Upload is the one route here that costs an LLM call (multimodal extraction over the scan), so
# it is the one that is rate limited and the one that can answer 429.
_UPLOAD_ERRORS = errors(401, 403, 404, 429)

# Anything outside this set is dropped from the ASCII fallback filename. Notably excludes the
# double quote and the semicolon (which would end/extend the Content-Disposition parameter) and
# CR/LF (header splitting).
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]")


def _content_disposition(file_name: str, disposition: str) -> str:
    """Build an RFC 6266 Content-Disposition for a clinician-supplied file name.

    Two things make naive f-string interpolation unsafe here. ``file_name`` comes straight
    off the upload, and HTTP header values are latin-1 encoded by Starlette -- so a
    Devanagari/Tamil/Bengali name (entirely normal for scanned prescriptions in this
    product's market) raises UnicodeEncodeError and the document becomes permanently
    undownloadable. A name containing a double quote or semicolon can also close the quoted
    string early and inject further disposition parameters.

    So: an aggressively sanitised ASCII ``filename`` for old clients, plus the lossless
    RFC 5987 ``filename*`` that every current browser prefers.
    """
    ascii_name = unicodedata.normalize("NFKD", file_name).encode("ascii", "ignore").decode("ascii")
    ascii_name = _UNSAFE_FILENAME_CHARS.sub("_", ascii_name).strip(" .")
    # Transliteration can erase a name entirely (e.g. a wholly Devanagari one).
    ascii_name = ascii_name or "document"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(file_name, safe='')}"


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """Read an upload, aborting as soon as it exceeds ``limit``.

    ``await file.read()`` buffers the entire body first and only then lets the service check
    the size, so a multi-gigabyte body is fully spooled (to memory, then to the temp disk
    Starlette rolls over to) before it can be rejected. Reading in chunks and stopping one
    chunk past the limit bounds what an unauthenticated-sized body can cost us.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
        total += len(chunk)
        if total > limit:
            raise FileTooLargeError(
                file_too_large_message(limit),
                detail=f"streamed upload exceeded limit {limit}B and was aborted",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a prescription, lab report or scan",
    responses=_UPLOAD_ERRORS,
    dependencies=[Depends(rate_limit("document_upload"))],
)
async def upload_document(
    patient_id: uuid.UUID,
    file: UploadFile = File(...),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    """Store the file and run extraction over it. Multipart; one file per request.

    The type is decided by magic bytes, not by the filename or the client's content type, and
    anything that is not a PDF or a JPEG/PNG/WebP/HEIC image is a 422 (`unsupported_file_type`).
    Oversized bodies are aborted mid-stream with 422 `file_too_large` rather than buffered whole.

    Extraction runs inline and its failure is not this endpoint's failure: the document is
    stored either way and the response reports `extraction_status`. `failed` means the scan is
    in the chart but was not read; `needs_confirmation` covers both a doubtful read and a read
    that produced nothing at all. Nothing extracted touches the patient graph until a clinician
    approves it at `POST /{doc_id}/approve`.

    Identical bytes for the same patient return the document already stored rather than a
    second one — and if that document was never successfully read, uploading it again re-runs
    extraction over it, which is the same recovery as `POST /{doc_id}/extraction/retry`. An
    already-approved document is returned untouched.
    """
    data = await _read_capped(file, settings.max_upload_bytes)
    document = await DocumentService(db).upload(
        account_id=account.id,
        patient_id=patient_id,
        file_name=file.filename or "upload",
        data=data,
    )
    return DocumentResponse.model_validate(document)


@router.get(
    "",
    response_model=list[DocumentResponse],
    summary="Every document in this patient's chart",
    responses=PATIENT_ERRORS,
)
async def list_documents(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[DocumentResponse]:
    """Metadata only, newest first — the file bytes come from `GET /{doc_id}/file`.

    Unpaginated, because a chart's document count is bounded by the patient's history. The
    listing is a PHI disclosure in itself — file names and dates describe the patient's care —
    so it is audited as `document_list_viewed`.

    Opening the chart is also what closes out documents whose extraction was interrupted and
    never finished: they are marked `failed`, so they stop reading as "still being read" and
    become retryable. That runs after this response is built, so a reclaim shows up on the next
    load rather than this one.
    """
    service = DocumentService(db)
    docs = await service.list(account.id, patient_id)
    # Appended after the read and immediately before the commit: on PostgreSQL the append
    # lock is transaction-scoped and only released at COMMIT, so auditing first and reading
    # afterwards would hold the global lock for the length of the read.
    await AuditService(db).record(
        action="document_list_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="patient",
        entity_id=patient_id,
        # A count, not the file names — those routinely carry the patient's own name, and
        # audit_logs.payload is unencrypted, immutable and never pruned.
        payload={"document_count": len(docs)},
    )
    await db.commit()
    body = [DocumentResponse.model_validate(d) for d in docs]
    # After the commit, and after the response has been materialised — never before. The
    # reclaim runs in its own transaction so failed housekeeping cannot take the chart listing
    # down with it, and its failure path rolls back, which expires *every* object in the
    # session including the `account` this handler is still holding. Reading `account.id` after
    # that reloads from an async engine outside a greenlet context and raises MissingGreenlet.
    # Same placement, and mostly the same argument, as `AuthService.sweep_sessions_if_due` on
    # the sign-in routes.
    await service.reclaim_stalled_extractions(patient_id)
    return body


@router.get(
    "/{doc_id}",
    response_model=DocumentResponse,
    summary="One document's metadata",
    responses=PATIENT_ERRORS,
)
async def get_document(
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    """404 (`document_not_found`) if the id is unknown, deleted, or belongs to another chart."""
    document = await DocumentService(db).get(account.id, patient_id, doc_id)
    return DocumentResponse.model_validate(document)


@router.get(
    "/{doc_id}/extraction",
    response_model=ExtractionResult,
    summary="What was extracted from a document, pending review",
    responses=PATIENT_ERRORS,
)
async def get_extraction(
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ExtractionResult:
    """The proposed entities and their per-field confidence — this is the review queue.

    A field below the confidence threshold carries `needs_confirmation`, and
    `confirmation_required_count` totals them. None of it is in the patient graph yet.

    Nothing here is in the chart, but all of it was read off the patient's own scan — drugs,
    doses, lab values — so the disclosure is audited as `extraction_viewed`.
    """
    service = DocumentService(db)
    document = await service.get(account.id, patient_id, doc_id)
    result = service.build_extraction_result(document)
    await AuditService(db).record(
        action="extraction_viewed",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="document",
        entity_id=doc_id,
        # Counts and status only. The extracted values themselves are the clinical content
        # and do not belong in an unencrypted table that is never pruned.
        payload={
            "extraction_status": document.extraction_status,
            "entity_count": len(result.entities),
        },
    )
    await db.commit()
    return result


@router.post(
    "/{doc_id}/extraction/retry",
    response_model=DocumentResponse,
    summary="Read a stored document again",
    responses=_UPLOAD_ERRORS | errors(409),
    dependencies=[Depends(rate_limit("document_upload"))],
)
async def retry_extraction(
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    """Re-run extraction over a document already in the chart, and report the new status.

    The recovery path when a scan did not read: every vision provider was down, the worker was
    restarted mid-page, or the extraction simply came back empty. The stored file is re-read
    from storage — nothing is re-uploaded — and the previous extraction is replaced.

    Refused with 409 if the extraction has already been approved (`extraction_already_approved`)
    — those entities are in the record, and a fresh unreviewed extraction would no longer
    describe what was merged — or if extraction is genuinely still running
    (`extraction_in_progress`). A document that has been stuck in `processing` past
    `EXTRACTION_STALL_MINUTES` is not "still running": nothing is finishing it, and it is
    retryable.

    Costs an LLM call, so it shares the upload rate limit and can answer 429.
    """
    document = await DocumentService(db).retry_extraction(
        account_id=account.id, patient_id=patient_id, doc_id=doc_id
    )
    return DocumentResponse.model_validate(document)


@router.post(
    "/{doc_id}/approve",
    summary="Merge a reviewed extraction into the patient graph",
    responses=PATIENT_ERRORS | errors(403, 409),
)
async def approve_extraction(
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    body: ExtractionApproval,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The clinician's sign-off — the only path from an extraction into the longitudinal record.

    `corrections` overwrite individual field values (and are audited per field);
    `rejected_entity_indexes` drop entities entirely. Everything else is merged, deduplicated
    against what the chart already holds. Returns the per-entity-type counts actually written.

    A correction naming an entity index or a field the extraction does not have is a 422
    (`correction_not_applicable`) and nothing is approved — the client is working from a stale
    view, and silently dropping the amendment would merge the value the clinician just
    overruled. Re-fetch `../extraction` and post the correction against it.

    A document nothing was extracted from — it failed, or it read as empty — is a 422
    (`nothing_to_approve`) rather than a 200 that merged nothing: approving it would tell the
    clinician the scan is in the record and would overwrite the status that says it never was.
    Read it again at `../extraction/retry`, or enter the details by hand. Rejecting every
    extracted entity is a different thing and stays a 200 — that is a decision about a
    document that *was* read.

    Safe to repeat. Approving twice merges nothing the second time rather than duplicating the
    document's entities, so a retry after a timeout — or a double-clicked button — is harmless.
    Two approvals that genuinely overlap are a 409 (`concurrent_approval`) for whichever one
    loses: the database refuses the duplicate observation and that approval is rolled back whole,
    leaving the chart as the winner wrote it. Retrying it is safe and is the way to confirm a
    correction it was carrying actually landed.

    Merging labs re-runs the deterministic critical-value check, so a panic value in an
    approved report raises its flag here rather than waiting for someone to open the chart.
    """
    counts = await DocumentService(db).approve(
        account_id=account.id, patient_id=patient_id, doc_id=doc_id, approval=body
    )
    return {"merged": counts}


@router.get(
    "/{doc_id}/file",
    summary="Download the original scan",
    response_class=Response,
    responses=PATIENT_ERRORS
    | {
        200: {
            "description": (
                "The stored file, byte for byte. `Content-Type` is the magic-byte-verified "
                "type. Images are served `inline`; PDFs are always `attachment` — a browser's "
                "PDF viewer executes embedded JavaScript, which inline rendering would run "
                "against this API's own origin."
            ),
            "content": {"application/pdf": {}, "image/jpeg": {}, "image/png": {}},
        }
    },
)
async def download_file(
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The archived original, which is the primary clinical source.

    The widest PHI disclosure in the API — it hands over the scan itself rather than an
    extracted summary — so the retrieval is audited as `document_downloaded`.
    """
    service = DocumentService(db)
    document = await service.get(account.id, patient_id, doc_id)
    data = await service.storage.read_async(document.storage_path)
    # Images may render inline (they are inert, and the type is magic-byte verified). A
    # user-uploaded PDF is not: the browser's PDF viewer executes embedded JavaScript, and
    # rendering it inline would run that script against this API's own origin, where the
    # session's tokens live. PDFs are therefore always handed over as a download.
    is_image = document.file_type.startswith("image/")
    disposition = "inline" if is_image else "attachment"
    # The widest PHI disclosure in the API -- this hands over the original scan, not an
    # extracted summary -- so the retrieval itself is audited.
    await AuditService(db).record(
        action="document_downloaded",
        account_id=account.id,
        patient_id=patient_id,
        entity_type="document",
        entity_id=doc_id,
        # file_type only, not file_name -- scan filenames usually carry the patient's name and
        # audit_logs.payload is stored unencrypted. entity_id identifies the document; its name
        # lives on the document row, so recording it here adds nothing but a copy of a direct
        # identifier in a table that is immutable and never pruned.
        payload={"file_type": document.file_type},
    )
    await db.commit()
    return Response(
        content=data,
        media_type=_MIME_BY_TYPE.get(document.file_type, "application/octet-stream"),
        headers={"Content-Disposition": _content_disposition(document.file_name, disposition)},
    )
