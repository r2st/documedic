"""Document upload, listing, extraction review, and approval routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db
from app.dependencies import get_current_account
from app.exceptions import FileTooLargeError
from app.models.user import Account
from app.schemas.document import (
    DocumentResponse,
    ExtractionApproval,
    ExtractionResult,
)
from app.services.document_service import DocumentService

router = APIRouter(prefix="/patients/{patient_id}/documents", tags=["documents"])

_MIME_BY_TYPE = {
    "pdf": "application/pdf",
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/heic": "image/heic",
}


_UPLOAD_CHUNK_BYTES = 1024 * 1024


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
            raise FileTooLargeError(f"File exceeds maximum of {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    patient_id: uuid.UUID,
    file: UploadFile = File(...),
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    data = await _read_capped(file, settings.max_upload_bytes)
    document = await DocumentService(db).upload(
        account_id=account.id,
        patient_id=patient_id,
        file_name=file.filename or "upload",
        data=data,
    )
    return DocumentResponse.model_validate(document)


@router.get("", response_model=list[DocumentResponse])
async def list_documents(
    patient_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> list[DocumentResponse]:
    docs = await DocumentService(db).list(account.id, patient_id)
    return [DocumentResponse.model_validate(d) for d in docs]


@router.get("/{doc_id}", response_model=DocumentResponse)
async def get_document(
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> DocumentResponse:
    document = await DocumentService(db).get(account.id, patient_id, doc_id)
    return DocumentResponse.model_validate(document)


@router.get("/{doc_id}/extraction", response_model=ExtractionResult)
async def get_extraction(
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> ExtractionResult:
    service = DocumentService(db)
    document = await service.get(account.id, patient_id, doc_id)
    return service.build_extraction_result(document)


@router.post("/{doc_id}/approve")
async def approve_extraction(
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    body: ExtractionApproval,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> dict:
    counts = await DocumentService(db).approve(
        account_id=account.id, patient_id=patient_id, doc_id=doc_id, approval=body
    )
    return {"merged": counts}


@router.get("/{doc_id}/file")
async def download_file(
    patient_id: uuid.UUID,
    doc_id: uuid.UUID,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db),
) -> Response:
    service = DocumentService(db)
    document = await service.get(account.id, patient_id, doc_id)
    data = service.storage.read(document.storage_path)
    return Response(
        content=data,
        media_type=_MIME_BY_TYPE.get(document.file_type, "application/octet-stream"),
        headers={"Content-Disposition": f'inline; filename="{document.file_name}"'},
    )
