"""Extraction orchestrator: Claude vision -> Tesseract/text fallback -> normalization."""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile

from app.config import settings
from app.core.logsafe import describe_exception
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
        """Vision first; on miss, recover text and parse it deterministically.

        The two ways of ending up with nothing — no legible text at all, and legible text the
        parser found no entities in — are the same outcome for the caller, so they share one
        exit (``_no_entities``).
        """
        vision = self._try_vision(file_bytes, file_type)
        if vision is not None:
            return vision

        text, ocr_used = self._recover_text(file_bytes, file_type, raw_text)
        entities = parse_text(text) if text.strip() else []
        if not entities:
            return self._no_entities(ocr_used)
        return ExtractionResultInternal(entities, self._infer_doc_type(entities), ocr_used, None)

    def _try_vision(self, file_bytes: bytes, file_type: str) -> ExtractionResultInternal | None:
        """Claude vision, when configured and confident enough. ``None`` means "fall through"."""
        if not claude_client.is_available():
            return None
        try:
            entities, doc_type = claude_client.extract(file_bytes, file_type)
            if entities and self._mean_confidence(entities) >= settings.ocr_fallback_threshold:
                return ExtractionResultInternal(
                    entities, doc_type, False, claude_client.model_label()
                )
        except Exception as exc:  # noqa: BLE001 — fall through to the deterministic path
            # Never log the document bytes or the model response (patient data) — only the
            # exception type, so degraded extraction stays diagnosable. This appended
            # str(exc)[:200] as well, which is the model's own response text on the most
            # common failure here. See app.core.logsafe.
            logger.warning(
                "Vision extraction unavailable, falling back to the deterministic "
                "parser (file_type=%s): %s",
                file_type,
                describe_exception(exc),
            )
        return None

    def _recover_text(
        self, file_bytes: bytes, file_type: str, raw_text: str | None
    ) -> tuple[str, bool]:
        """Returns the document's text and whether Tesseract OCR produced it."""
        if raw_text:
            return raw_text, False
        if file_type == "pdf":
            text = _pdf_text(file_bytes)
            return (text if text.strip() else self._maybe_plain(file_bytes)), False
        if file_type.startswith("image/"):
            text = _tesseract_text(file_bytes, file_type)
            return text, bool(text.strip())
        return self._maybe_plain(file_bytes), False

    @staticmethod
    def _no_entities(ocr_used: bool) -> ExtractionResultInternal:
        """Final safety net: simulated sample data in demo mode so the ingestion flow always
        has something to show; otherwise an explicit empty result for manual confirmation."""
        if claude_client.demo_active():
            entities, doc_type = claude_client.demo_extract()
            return ExtractionResultInternal(
                entities, doc_type, ocr_used, claude_client.demo_model_label()
            )
        return ExtractionResultInternal([], None, ocr_used, None)

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
