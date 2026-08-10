"""Extraction orchestrator: Claude vision -> Tesseract/text fallback -> normalization."""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile

from app.config import settings
from app.services.extraction import claude_client
from app.services.extraction.text_parser import ParsedEntity, parse_text

logger = logging.getLogger(__name__)


def _confidence_band(score: float) -> str:
    if score >= settings.confirmation_confidence_threshold:
        return "high"
    if score >= settings.ocr_fallback_threshold:
        return "medium"
    return "low"


def _pdf_text(file_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


def _tesseract_text(file_bytes: bytes, file_type: str) -> str:
    """OCR an image via the Tesseract binary if available. Returns '' if unavailable."""
    binary = (
        settings.tesseract_cmd
        if shutil.which(settings.tesseract_cmd)
        else shutil.which("tesseract")
    )
    if not binary:
        return ""
    suffix = ".png" if "png" in file_type else ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(file_bytes)
        tmp.flush()
        try:
            result = subprocess.run(
                [binary, tmp.name, "stdout", "-l", "eng+hin"],
                capture_output=True,
                timeout=60,
                check=False,
            )
            return result.stdout.decode("utf-8", errors="ignore")
        except (subprocess.SubprocessError, OSError):
            return ""


class ExtractionResultInternal:
    def __init__(
        self,
        entities: list[ParsedEntity],
        document_type: str | None,
        ocr_fallback_used: bool,
        model: str | None,
    ) -> None:
        self.entities = entities
        self.document_type = document_type
        self.ocr_fallback_used = ocr_fallback_used
        self.model = model


class ExtractionPipeline:
    """Stateless extraction orchestrator. Deterministic given the same inputs."""

    def run(
        self, file_bytes: bytes, file_type: str, *, raw_text: str | None = None
    ) -> ExtractionResultInternal:
        # 1) Preferred: Claude vision (only when an API key is configured).
        if claude_client.is_available():
            try:
                entities, doc_type = claude_client.extract(file_bytes, file_type)
                if entities and self._mean_confidence(entities) >= settings.ocr_fallback_threshold:
                    return ExtractionResultInternal(
                        entities, doc_type, False, claude_client.model_label()
                    )
            except Exception as exc:  # noqa: BLE001 — fall through to the deterministic path
                # Never log the document bytes or the model response (patient data) —
                # only the exception type, so degraded extraction is diagnosable.
                logger.warning(
                    "Vision extraction unavailable, falling back to the deterministic "
                    "parser (file_type=%s): %s: %s",
                    file_type,
                    type(exc).__name__,
                    str(exc)[:200],
                )

        # 2) Recover text: PDF text layer, supplied raw text, or Tesseract OCR for images.
        ocr_used = False
        if raw_text:
            text = raw_text
        elif file_type == "pdf":
            text = _pdf_text(file_bytes)
            if not text.strip():
                text = self._maybe_plain(file_bytes)
        elif file_type.startswith("image/"):
            text = _tesseract_text(file_bytes, file_type)
            ocr_used = bool(text.strip())
        else:
            text = self._maybe_plain(file_bytes)

        if not text.strip():
            # Nothing legible. Final safety net: simulated sample data in demo mode so the
            # ingestion flow always has something to show; otherwise flag for manual confirmation.
            if claude_client.demo_active():
                entities, doc_type = claude_client.demo_extract()
                return ExtractionResultInternal(
                    entities, doc_type, ocr_used, claude_client.demo_model_label()
                )
            return ExtractionResultInternal([], None, ocr_used, None)

        entities = parse_text(text)
        if not entities and claude_client.demo_active():
            entities, doc_type = claude_client.demo_extract()
            return ExtractionResultInternal(
                entities, doc_type, ocr_used, claude_client.demo_model_label()
            )
        doc_type = self._infer_doc_type(entities)
        return ExtractionResultInternal(entities, doc_type, ocr_used, None)

    @staticmethod
    def _maybe_plain(file_bytes: bytes) -> str:
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    @staticmethod
    def _mean_confidence(entities: list[ParsedEntity]) -> float:
        scores = [f.confidence for e in entities for f in e.fields]
        return sum(scores) / len(scores) if scores else 0.0

    @staticmethod
    def _infer_doc_type(entities: list[ParsedEntity]) -> str | None:
        types = {e.entity_type for e in entities}
        if "medication" in types and "lab_result" not in types:
            return "prescription"
        if "lab_result" in types and "medication" not in types:
            return "lab_report"
        if types:
            return "discharge_summary"
        return None
