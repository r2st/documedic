"""Extraction pipeline: vision -> PDF text layer -> OCR -> deterministic parser.

Every rung of the ladder has to degrade to the next without raising, because a failed
extraction should surface as "nothing legible, please confirm manually" — never as a 500 on
a clinician's upload.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.extraction import claude_client
from app.services.extraction.pipeline import ExtractionPipeline
from app.services.extraction.text_parser import ParsedEntity, ParsedField

PRESCRIPTION_TEXT = """
Dr A Sharma, MBBS MD
Rx
Tab Crocin 650mg 1-0-1 x 5 days
Tab Metformin 500mg 1-0-1 continue
"""

LAB_TEXT = """
LABORATORY REPORT
HbA1c            8.4 %      (4.0 - 5.6)
Serum creatinine 1.3 mg/dL  (0.7 - 1.3)
"""


@pytest.fixture
def no_providers(monkeypatch):
    """No LLM keys and no demo net — the deterministic path only."""
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_demo_fallback", False)


def _entity(confidence: float) -> ParsedEntity:
    return ParsedEntity(
        entity_type="medication",
        fields=[ParsedField(name="brand_name_raw", value="Crocin", confidence=confidence)],
    )


# --- Deterministic path ----------------------------------------------------------------


def test_supplied_raw_text_is_parsed_without_any_provider(no_providers):
    result = ExtractionPipeline().run(b"", "pdf", raw_text=PRESCRIPTION_TEXT)
    assert result.entities
    assert result.model is None  # no LLM was involved
    assert result.ocr_fallback_used is False


def test_document_type_is_inferred_from_a_prescription(no_providers):
    result = ExtractionPipeline().run(b"", "pdf", raw_text=PRESCRIPTION_TEXT)
    assert result.document_type == "prescription"


def test_document_type_is_inferred_from_a_lab_report(no_providers):
    result = ExtractionPipeline().run(b"", "pdf", raw_text=LAB_TEXT)
    assert result.document_type == "lab_report"


def test_a_mixed_document_is_classified_as_a_discharge_summary(no_providers):
    result = ExtractionPipeline().run(b"", "pdf", raw_text=PRESCRIPTION_TEXT + LAB_TEXT)
    assert result.document_type == "discharge_summary"


def test_plain_utf8_bytes_are_readable_without_a_pdf_layer(no_providers):
    result = ExtractionPipeline().run(PRESCRIPTION_TEXT.encode(), "text/plain")
    assert result.entities


def test_undecodable_bytes_yield_an_empty_result_not_an_error(no_providers):
    result = ExtractionPipeline().run(b"\xff\xfe\x00\x01\x02", "application/octet-stream")
    assert result.entities == []
    assert result.document_type is None


def test_an_unreadable_pdf_degrades_to_an_empty_result(no_providers):
    """A scanned-image PDF has no text layer; with no OCR and no vision key there is
    nothing to extract, and that must be reported rather than guessed at."""
    result = ExtractionPipeline().run(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n", "pdf")
    assert result.entities == []


def test_an_image_without_tesseract_degrades_quietly(no_providers, monkeypatch):
    monkeypatch.setattr("app.services.extraction.pipeline.shutil.which", lambda _cmd: None)
    result = ExtractionPipeline().run(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")
    assert result.entities == []
    assert result.ocr_fallback_used is False


def test_ocr_output_is_parsed_and_flagged_as_a_fallback(no_providers, monkeypatch):
    monkeypatch.setattr(
        "app.services.extraction.pipeline._tesseract_text",
        lambda data, ftype: PRESCRIPTION_TEXT,
    )
    result = ExtractionPipeline().run(b"\x89PNG\r\n\x1a\n", "image/png")
    assert result.entities
    assert result.ocr_fallback_used is True


# --- Vision path and its fallbacks ------------------------------------------------------


def test_high_confidence_vision_output_is_used_directly(monkeypatch):
    monkeypatch.setattr(claude_client, "is_available", lambda: True)
    monkeypatch.setattr(claude_client, "model_label", lambda: "test-model")
    monkeypatch.setattr(
        claude_client, "extract", lambda data, ftype: ([_entity(0.95)], "prescription")
    )

    result = ExtractionPipeline().run(b"\x89PNG", "image/png")
    assert result.model == "test-model"
    assert result.document_type == "prescription"
    assert result.ocr_fallback_used is False


def test_low_confidence_vision_output_falls_through_to_the_text_path(no_providers, monkeypatch):
    """Below ocr_fallback_threshold the model is guessing; prefer deterministic parsing."""
    monkeypatch.setattr(claude_client, "is_available", lambda: True)
    monkeypatch.setattr(
        claude_client, "extract", lambda data, ftype: ([_entity(0.10)], "prescription")
    )

    result = ExtractionPipeline().run(b"", "pdf", raw_text=PRESCRIPTION_TEXT)
    assert result.model is None
    assert result.entities


def test_a_raising_provider_falls_through_instead_of_propagating(no_providers, monkeypatch):
    def boom(data, ftype):
        raise RuntimeError("provider 503")

    monkeypatch.setattr(claude_client, "is_available", lambda: True)
    monkeypatch.setattr(claude_client, "extract", boom)

    result = ExtractionPipeline().run(b"", "pdf", raw_text=PRESCRIPTION_TEXT)
    assert result.entities  # deterministic parse still succeeded
    assert result.model is None


def test_a_provider_failure_is_logged(no_providers, monkeypatch, caplog):
    """Degraded extraction has to be diagnosable in production — but the log must not
    contain the document or the model response, which carry patient data."""

    def boom(data, ftype):
        raise RuntimeError("provider 503")

    monkeypatch.setattr(claude_client, "is_available", lambda: True)
    monkeypatch.setattr(claude_client, "extract", boom)

    with caplog.at_level("WARNING"):
        ExtractionPipeline().run(PRESCRIPTION_TEXT.encode(), "pdf", raw_text=PRESCRIPTION_TEXT)

    messages = [rec.getMessage() for rec in caplog.records]
    assert any("RuntimeError" in m for m in messages)
    assert not any("Crocin" in m for m in messages)


def test_empty_vision_output_falls_through(no_providers, monkeypatch):
    monkeypatch.setattr(claude_client, "is_available", lambda: True)
    monkeypatch.setattr(claude_client, "extract", lambda data, ftype: ([], None))

    result = ExtractionPipeline().run(b"", "pdf", raw_text=PRESCRIPTION_TEXT)
    assert result.entities
    assert result.model is None


# --- Demo safety net --------------------------------------------------------------------


def test_the_demo_net_supplies_samples_when_nothing_is_legible(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_demo_fallback", True)

    result = ExtractionPipeline().run(b"\xff\xfe\x00\x01", "application/octet-stream")
    assert result.entities
    assert result.model and "simulated" in result.model


def test_demo_samples_are_clearly_marked(monkeypatch):
    """Simulated clinical data must never be mistakable for a real extraction."""
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_demo_fallback", True)

    result = ExtractionPipeline().run(b"\xff\xfe\x00\x01", "application/octet-stream")
    assert claude_client.demo_data.DEMO_TAG in result.model


def test_mean_confidence_of_an_empty_extraction_is_zero():
    assert ExtractionPipeline._mean_confidence([]) == 0.0


def test_infer_doc_type_of_nothing_is_none():
    assert ExtractionPipeline._infer_doc_type([]) is None
