"""Vision-extraction provider dispatch.

The deployment runs OpenRouter as its primary LLM provider, so the extraction path has to
route through OpenRouter rather than falling through to an Anthropic client with no key.
These tests stub the SDKs — no network, no API key needed.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services.extraction import claude_client

_PAYLOAD = {
    "document_type": "prescription",
    "entities": [
        {
            "entity_type": "medication",
            "fields": {"brand_name_raw": "Crocin", "dose": "500", "dose_unit": "mg"},
            "confidence": {"brand_name_raw": 0.95, "dose": 0.9},
        }
    ],
}

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF_BYTES = b"%PDF-1.7\n" + b"\x00" * 32


class _FakeChatCompletions:
    """Captures the create() kwargs so tests can assert on what was actually sent."""

    def __init__(self, sink: dict, content: str) -> None:
        self._sink = sink
        self._content = content

    def create(self, **kwargs):
        self._sink.update(kwargs)

        class _Msg:
            content = self._content

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        return _Completion()


def _fake_openai(sink: dict, content: str):
    class FakeOpenAI:
        def __init__(self, **kwargs):
            sink["client_kwargs"] = kwargs
            self.chat = type("Chat", (), {"completions": _FakeChatCompletions(sink, content)})()

    return FakeOpenAI


@pytest.fixture
def openrouter_only(monkeypatch):
    """OpenRouter is the only configured provider — the production deployment's shape."""
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")


# --- OpenRouter routing ----------------------------------------------------------------


def test_openrouter_only_config_extracts_images_via_openrouter(monkeypatch, openrouter_only):
    """Regression: dispatch used to send every non-OpenAI provider to the Anthropic SDK,
    so an OpenRouter-only deployment called Anthropic with an empty key and extraction
    silently degraded to the deterministic parser on every document."""
    import openai

    sink: dict = {}
    monkeypatch.setattr(openai, "OpenAI", _fake_openai(sink, json.dumps(_PAYLOAD)))

    entities, doc_type = claude_client.extract(PNG_BYTES, "image/png")

    assert doc_type == "prescription"
    assert [e.entity_type for e in entities] == ["medication"]
    assert sink["client_kwargs"]["base_url"] == settings.openrouter_base_url
    assert sink["client_kwargs"]["api_key"] == "or-key"
    assert sink["model"] == settings.openrouter_model


def test_openrouter_sends_images_as_a_data_url(monkeypatch, openrouter_only):
    import openai

    sink: dict = {}
    monkeypatch.setattr(openai, "OpenAI", _fake_openai(sink, json.dumps(_PAYLOAD)))
    claude_client.extract(PNG_BYTES, "image/png")

    parts = sink["messages"][1]["content"]
    image_part = next(p for p in parts if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_openrouter_sends_pdfs_as_a_file_part(monkeypatch, openrouter_only):
    import openai

    sink: dict = {}
    monkeypatch.setattr(openai, "OpenAI", _fake_openai(sink, json.dumps(_PAYLOAD)))
    claude_client.extract(PDF_BYTES, "pdf")

    parts = sink["messages"][1]["content"]
    file_part = next(p for p in parts if p["type"] == "file")
    assert file_part["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_openrouter_rejects_unsupported_types(monkeypatch, openrouter_only):
    import openai

    monkeypatch.setattr(openai, "OpenAI", _fake_openai({}, json.dumps(_PAYLOAD)))
    with pytest.raises(RuntimeError):
        claude_client.extract(b"junk", "text/plain")


def test_openrouter_client_gets_the_configured_timeout(monkeypatch, openrouter_only):
    import openai

    sink: dict = {}
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 9.5)
    monkeypatch.setattr(openai, "OpenAI", _fake_openai(sink, json.dumps(_PAYLOAD)))
    claude_client.extract(PNG_BYTES, "image/png")
    assert sink["client_kwargs"]["timeout"] == 9.5


# --- Fallback chain --------------------------------------------------------------------


def test_failing_primary_falls_through_to_the_next_provider(monkeypatch):
    """OpenAI cannot take PDFs, so a PDF must reach the Anthropic tier rather than fail."""
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "an-key")
    monkeypatch.setattr(settings, "openrouter_api_key", "")

    called: list[str] = []

    def fake_anthropic(file_bytes, file_type):
        called.append(file_type)
        return json.dumps(_PAYLOAD)

    monkeypatch.setitem(claude_client._EXTRACTORS, "anthropic", fake_anthropic)

    entities, doc_type = claude_client.extract(PDF_BYTES, "pdf")
    assert called == ["pdf"]
    assert doc_type == "prescription"
    assert entities


def test_extract_raises_when_every_provider_fails(monkeypatch, openrouter_only):
    def boom(file_bytes, file_type):
        raise RuntimeError("upstream 503")

    monkeypatch.setitem(claude_client._EXTRACTORS, "openrouter", boom)
    with pytest.raises(RuntimeError, match="upstream 503"):
        claude_client.extract(PNG_BYTES, "image/png")


def test_extract_raises_when_no_provider_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    with pytest.raises(RuntimeError, match="No LLM provider"):
        claude_client.extract(PNG_BYTES, "image/png")


def test_non_json_response_is_treated_as_a_provider_failure(monkeypatch, openrouter_only):
    import openai

    monkeypatch.setattr(openai, "OpenAI", _fake_openai({}, "I cannot read this document."))
    with pytest.raises(RuntimeError):
        claude_client.extract(PNG_BYTES, "image/png")


def test_prose_wrapped_json_is_still_parsed(monkeypatch, openrouter_only):
    """Models routinely wrap JSON in commentary or a fenced block."""
    import openai

    wrapped = f"Here is the extraction:\n```json\n{json.dumps(_PAYLOAD)}\n```\nDone."
    monkeypatch.setattr(openai, "OpenAI", _fake_openai({}, wrapped))
    entities, doc_type = claude_client.extract(PNG_BYTES, "image/png")
    assert doc_type == "prescription"
    assert entities


# --- Model labelling -------------------------------------------------------------------


def test_model_label_reports_the_active_provider(monkeypatch, openrouter_only):
    assert claude_client.model_label() == settings.openrouter_model


def test_model_label_for_openai_primary(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    assert claude_client.model_label() == settings.openai_model


def test_model_label_falls_back_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    assert claude_client.model_label() == settings.anthropic_model


def test_is_available_tracks_configured_providers(monkeypatch, openrouter_only):
    assert claude_client.is_available() is True
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    assert claude_client.is_available() is False


# --- Payload coercion ------------------------------------------------------------------


def test_entities_without_usable_fields_are_dropped(monkeypatch, openrouter_only):
    import openai

    payload = {
        "document_type": "lab_report",
        "entities": [
            {"entity_type": "lab_result", "fields": {"marker_name": None}, "confidence": {}},
            {
                "entity_type": "lab_result",
                "fields": {"marker_name": "HbA1c", "value_numeric": 8.1},
                "confidence": {"marker_name": 0.99},
            },
        ],
    }
    monkeypatch.setattr(openai, "OpenAI", _fake_openai({}, json.dumps(payload)))

    entities, doc_type = claude_client.extract(PNG_BYTES, "image/png")
    assert doc_type == "lab_report"
    assert len(entities) == 1
    assert {f.name for f in entities[0].fields} == {"marker_name", "value_numeric"}


def test_missing_confidence_defaults_rather_than_crashing(monkeypatch, openrouter_only):
    import openai

    payload = {
        "document_type": "prescription",
        "entities": [{"entity_type": "medication", "fields": {"brand_name_raw": "Dolo 650"}}],
    }
    monkeypatch.setattr(openai, "OpenAI", _fake_openai({}, json.dumps(payload)))

    entities, _ = claude_client.extract(PNG_BYTES, "image/png")
    assert entities[0].fields[0].confidence == pytest.approx(0.7)


# --- OpenAI and Anthropic request shapes -----------------------------------------------


def test_openai_sends_an_image_data_url(monkeypatch):
    import openai

    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 11.0)

    sink: dict = {}
    monkeypatch.setattr(openai, "OpenAI", _fake_openai(sink, json.dumps(_PAYLOAD)))
    entities, _ = claude_client.extract(PNG_BYTES, "image/png")

    assert entities
    assert sink["client_kwargs"]["timeout"] == 11.0
    assert "base_url" not in sink["client_kwargs"]  # the direct OpenAI endpoint
    image_part = next(p for p in sink["messages"][1]["content"] if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_refuses_pdfs_so_the_chain_moves_on(monkeypatch):
    """The chat-completions vision path takes no PDF input; raising is what lets the
    Anthropic/OpenRouter tiers get a chance at it."""
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    with pytest.raises(RuntimeError, match="does not support"):
        claude_client._extract_openai(PDF_BYTES, "pdf")


class _FakeAnthropic:
    def __init__(self, sink: dict, text: str) -> None:
        self._sink = sink
        self._text = text

    def __call__(self, **kwargs):
        self._sink["client_kwargs"] = kwargs
        return self

    @property
    def messages(self):
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer._sink.update(kwargs)

                class _Block:
                    type = "text"
                    text = outer._text

                class _Message:
                    content = [_Block()]

                return _Message()

        return _Messages()


def test_anthropic_sends_pdfs_as_a_document_block(monkeypatch):
    import anthropic

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "an-key")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")

    sink: dict = {}
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic(sink, json.dumps(_PAYLOAD)))
    entities, _ = claude_client.extract(PDF_BYTES, "pdf")

    assert entities
    doc_part = next(p for p in sink["messages"][0]["content"] if p["type"] == "document")
    assert doc_part["source"]["media_type"] == "application/pdf"


def test_anthropic_sends_images_as_an_image_block(monkeypatch):
    import anthropic

    monkeypatch.setattr(settings, "llm_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "an-key")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")

    sink: dict = {}
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic(sink, json.dumps(_PAYLOAD)))
    claude_client.extract(PNG_BYTES, "image/png")

    image_part = next(p for p in sink["messages"][0]["content"] if p["type"] == "image")
    assert image_part["source"]["media_type"] == "image/png"
    assert sink["client_kwargs"]["timeout"] == settings.llm_request_timeout_seconds


def test_anthropic_rejects_unsupported_types(monkeypatch):
    import anthropic

    monkeypatch.setattr(settings, "anthropic_api_key", "an-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic({}, ""))
    with pytest.raises(RuntimeError, match="Unsupported file type"):
        claude_client._extract_anthropic(b"junk", "text/plain")


# --- Demo helpers ----------------------------------------------------------------------


def test_demo_active_only_without_a_real_provider(monkeypatch, openrouter_only):
    monkeypatch.setattr(settings, "llm_demo_fallback", True)
    assert claude_client.demo_active() is False  # a real key is configured

    monkeypatch.setattr(settings, "openrouter_api_key", "")
    assert claude_client.demo_active() is True


def test_demo_extraction_is_tagged_and_non_empty(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "llm_demo_fallback", True)

    entities, doc_type = claude_client.demo_extract()
    assert entities and doc_type
    assert claude_client.demo_data.DEMO_TAG in claude_client.demo_model_label()
