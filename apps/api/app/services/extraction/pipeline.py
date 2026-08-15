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
from app.services.extraction.text_parser import ParsedDocument, ParsedEntity, parse_document

logger = logging.getLogger(__name__)


def _confidence_band(score: float) -> str:
    if score >= settings.confirmation_confidence_threshold:
        return "high"
    if score >= settings.ocr_fallback_threshold:
        return "medium"
    return "low"


def _truncate_text(text: str, *, source: str) -> str:
    """``text``, cut to ``settings.max_extracted_text_chars``. Logs when it cuts.

    The last bound before the deterministic parser, which walks every character of what it is
    given with a set of regexes. See ``settings.max_extracted_text_chars`` for why a bounded
    *file* does not imply bounded text.
    """
    limit = settings.max_extracted_text_chars
    if len(text) <= limit:
        return text
    logger.warning(
        "Extracted text from a %s document exceeded %d characters and was truncated; "
        "entities past the cut will not be read.",
        source,
        limit,
    )
    return text[:limit]


def _pdf_text(file_bytes: bytes) -> str:
    """The PDF's text layer, or '' when there is not one this parser can read.

    The empty string is a real answer here — a scanned prescription is a PDF wrapping a
    photograph and genuinely has no text layer — so a parse failure degrades to the same value
    and the caller falls through to OCR either way. That is why this is caught rather than
    raised, and exactly why it is logged: "pypdf could not read this file" and "this file is a
    scan" are the same '' downstream, and without a line in the log there is nothing that tells
    an operator which of the two a document that came back empty actually was. Never logs the
    bytes or the exception's own message, which can quote document content.

    Bounded twice — by page count and by extracted characters, both from
    ``settings.max_pdf_pages_extracted`` / ``max_extracted_text_chars``, which document why the
    upload size limit is not itself a bound on this work. The page loop is written as a loop
    rather than a generator expression precisely so it can stop: ``reader.pages`` is lazy, so
    pages past the limit are never decompressed at all, which is where nearly all of the cost
    would have been.

    Truncation is not failure. The pages that were read are returned and parsed as usual, so an
    over-long document is read as far as the limit and then reviewed like any other partial
    read — the same outcome as a scan whose later pages were illegible.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        page_limit = settings.max_pdf_pages_extracted
        char_limit = settings.max_extracted_text_chars
        pages: list[str] = []
        length = 0
        read = 0
        for page in reader.pages:
            if read >= page_limit or length >= char_limit:
                break
            text = page.extract_text() or ""
            pages.append(text)
            # +1 for the newline the join puts back, so the running total matches the result.
            length += len(text) + 1
            read += 1
        if read and read < len(reader.pages):
            logger.warning(
                "Read the text layer of %d of a PDF's %d pages before hitting an extraction "
                "limit; the rest of the document was not read.",
                read,
                len(reader.pages),
            )
        return _truncate_text("\n".join(pages), source="pdf")
    except Exception as exc:  # noqa: BLE001 — OCR is the fallback for any unreadable PDF
        logger.warning(
            "Could not read a text layer from a PDF (%s); falling back to OCR.",
            describe_exception(exc),
        )
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
        except (subprocess.SubprocessError, OSError) as exc:
            # Includes the 60s ``TimeoutExpired``, which ``subprocess.run`` raises only after
            # killing the child and reaping it — so nothing is left running behind this.
            # Logged for the reason ``_pdf_text``'s handler is: the caller cannot tell an OCR
            # that failed from an image that held no text, and this is the only place that can.
            logger.warning(
                "Tesseract could not read a %s document (%s); no text was recovered from it.",
                file_type,
                describe_exception(exc),
            )
            return ""


class ExtractionResultInternal:
    def __init__(
        self,
        entities: list[ParsedEntity],
        document_type: str | None,
        ocr_fallback_used: bool,
        model: str | None,
        unreadable_lines: int = 0,
    ) -> None:
        self.entities = entities
        self.document_type = document_type
        self.ocr_fallback_used = ocr_fallback_used
        self.model = model
        # Lines under a clinical section header that no grammar could read — see
        # ``text_parser.ParsedDocument``. Defaults to zero because only the deterministic parser
        # can know: the vision path is handed a page and returns entities, with no line-by-line
        # account of the page to compare them against.
        self.unreadable_lines = unreadable_lines


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
        confident, unsure = self._try_vision(file_bytes, file_type)
        if confident is not None:
            return confident

        text, ocr_used = self._recover_text(file_bytes, file_type, raw_text)
        parsed = parse_document(text) if text.strip() else ParsedDocument([], 0)
        if not parsed.entities:
            # Nothing deterministic to prefer over the vision read, so a vision read the
            # confidence gate rejected is better than the empty result that gate used to
            # produce. It was thrown away *before* the fallback was known to have failed: a
            # scanned prescription the model read at 0.4 mean confidence, in a PDF with no text
            # layer for pypdf to recover, extracted four drugs and then reported none — and the
            # document went into the chart as read-and-empty rather than as needing review.
            #
            # Every field in it bands "low" or "medium", so ``_run_extraction`` marks the
            # document ``needs_confirmation`` and no value reaches the record until a clinician
            # has confirmed it against the original. That is the state a doubtful read belongs
            # in; silence is not.
            if unsure is not None:
                unsure.ocr_fallback_used = ocr_used
                return unsure
            return self._no_entities(ocr_used)
        return ExtractionResultInternal(
            parsed.entities,
            self._infer_doc_type(parsed.entities),
            ocr_used,
            None,
            parsed.unreadable,
        )

    def _try_vision(
        self, file_bytes: bytes, file_type: str
    ) -> tuple[ExtractionResultInternal | None, ExtractionResultInternal | None]:
        """Claude vision, as ``(confident, unsure)``.

        ``confident`` is a read at or above ``settings.ocr_fallback_threshold`` and is used as
        it stands. ``unsure`` is a read below it: entities the model did produce, held back in
        favour of the deterministic parser but kept in case that finds nothing at all. Both are
        ``None`` when no provider is configured or every one of them failed.
        """
        if not claude_client.is_available():
            return None, None
        try:
            entities, doc_type = claude_client.extract(file_bytes, file_type)
            if entities:
                result = ExtractionResultInternal(
                    entities, doc_type, False, claude_client.model_label()
                )
                if self._mean_confidence(entities) >= settings.ocr_fallback_threshold:
                    return result, None
                return None, result
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
        return None, None

    def _recover_text(
        self, file_bytes: bytes, file_type: str, raw_text: str | None
    ) -> tuple[str, bool]:
        """Returns the document's text and whether Tesseract OCR produced it.

        Every branch leaves through ``_truncate_text``. ``_pdf_text`` bounds itself as well —
        it has to, because the cost it is bounding is incurred while *producing* the string —
        but OCR output and a plain-text upload are only bounded here, and the parser downstream
        walks whatever it is handed.
        """
        if raw_text:
            return _truncate_text(raw_text, source="supplied-text"), False
        if file_type == "pdf":
            text = _pdf_text(file_bytes)
            return (text if text.strip() else self._maybe_plain(file_bytes)), False
        if file_type.startswith("image/"):
            text = _truncate_text(_tesseract_text(file_bytes, file_type), source=file_type)
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
            return _truncate_text(file_bytes.decode("utf-8"), source="plain-text")
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
