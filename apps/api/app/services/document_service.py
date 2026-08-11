"""Document upload, storage, extraction orchestration, and approval (P1-04/05/06)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.exceptions import (
    DocumentNotFoundError,
    FileTooLargeError,
    UnsupportedFileTypeError,
)
from app.models.document import Document
from app.models.patient import Patient
from app.schemas.document import (
    ExtractedEntity,
    ExtractionApproval,
    ExtractionField,
    ExtractionResult,
)
from app.services.audit_service import AuditService
from app.services.extraction import ExtractionPipeline
from app.services.filetype import describe_unsupported, sniff_file_type
from app.services.graph_service import GraphService
from app.services.lab_safety_service import LabSafetyService
from app.services.storage import compute_sha256, get_storage

# Comfortably longer than any real scan filename, short enough to be a bounded column value.
MAX_FILE_NAME_CHARS = 255

_BYTES_PER_MB = 1024 * 1024


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


class DocumentService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.audit = AuditService(db)
        self.storage = get_storage()
        self.pipeline = ExtractionPipeline()

    async def _get_patient(self, account_id: uuid.UUID, patient_id: uuid.UUID) -> Patient:
        from app.services.patient_service import PatientService

        return await PatientService(self.db).get(account_id, patient_id)

    async def upload(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        file_name: str,
        data: bytes,
    ) -> Document:
        await self._get_patient(account_id, patient_id)  # ownership + existence check

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
            raise UnsupportedFileTypeError(
                f"{UnsupportedFileTypeError().message} {describe_unsupported(data)}",
                detail=f"unrecognised magic bytes: {data[:12]!r}",
            )

        sha256 = compute_sha256(data)

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
            return dup

        storage_path = self.storage.write(str(patient_id), sha256, file_name, data)
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
        # Phase 1: extraction runs synchronously. (Plan P1-05c upgrades this to a Redis
        # Stream worker with SSE progress; the pipeline interface is unchanged.)
        await self._run_extraction(account_id, document, data)
        await self.db.commit()
        await self.db.refresh(document)
        return document

    async def _run_extraction(self, account_id: uuid.UUID, document: Document, data: bytes) -> None:
        document.extraction_status = "processing"
        document.extraction_started_at = datetime.now(UTC)
        await self.db.flush()

        result = self.pipeline.run(data, document.file_type)
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
            "approved": False,
        }
        document.document_type = result.document_type
        document.extraction_model = result.model
        document.ocr_fallback_used = result.ocr_fallback_used
        document.extraction_completed_at = datetime.now(UTC)
        document.extraction_status = "needs_confirmation" if confirmation_required else "completed"
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

    async def get(
        self, account_id: uuid.UUID, patient_id: uuid.UUID, doc_id: uuid.UUID
    ) -> Document:
        result = await self.db.execute(
            select(Document).where(
                Document.id == doc_id,
                Document.patient_id == patient_id,
                Document.account_id == account_id,
                Document.is_deleted.is_(False),
            )
        )
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
        )

    async def approve(
        self,
        *,
        account_id: uuid.UUID,
        patient_id: uuid.UUID,
        doc_id: uuid.UUID,
        approval: ExtractionApproval,
    ) -> dict[str, int]:
        document = await self.get(account_id, patient_id, doc_id)
        patient = await self._get_patient(account_id, patient_id)
        meta = dict(document.extraction_metadata or {})
        raw_entities = meta.get("entities", [])

        # Apply clinician corrections to the stored extraction (audited per field).
        corrected_fields: list[dict] = []
        for corr in approval.corrections:
            if 0 <= corr.entity_index < len(raw_entities):
                for f in raw_entities[corr.entity_index]["fields"]:
                    if f["name"] == corr.field_name:
                        f["value"] = corr.value
                        f["confidence"] = 1.0
                        f["confidence_band"] = "high"
                        f["needs_confirmation"] = False
                        corrected_fields.append(
                            {"entity_index": corr.entity_index, "field": corr.field_name}
                        )

        # Build the merge payload, skipping rejected entities.
        rejected = set(approval.rejected_entity_indexes)
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

        counts = await GraphService(self.db).merge_entities(
            patient=patient, document=document, entities=merge_payload
        )

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

        await self.audit.record(
            action="extraction_approved",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="document",
            entity_id=document.id,
            payload={"merged": counts, "rejected_count": len(rejected)},
        )
        await self.audit.record(
            action="graph_merged",
            account_id=account_id,
            patient_id=patient_id,
            entity_type="patient",
            entity_id=patient_id,
            payload=counts,
        )
        await self.db.commit()
        return counts
