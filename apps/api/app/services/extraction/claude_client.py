"""Multimodal vision extraction client (production path).

Provider-agnostic: the provider order is the same one the reasoning engine uses
(``settings.llm_provider`` primary, then the remaining providers as fallbacks — see
``app.agents.llm._provider_order``), so an OpenRouter-primary deployment extracts through
OpenRouter too. The chosen SDK is sent the document with a structured-output instruction
and the JSON response is parsed into ParsedEntity objects. When no provider can handle the
document, callers fall back to the deterministic text parser.

File-type support per provider:
  - OpenAI      — images only (chat-completions vision path takes no PDF input).
  - OpenRouter  — images via data URL; PDFs via OpenRouter's ``file`` content part.
  - Anthropic   — images and PDFs (native ``document`` block).
A provider that cannot handle the supplied type raises so ``extract`` moves to the next one,
and the deterministic text/OCR path catches whatever is left.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from app.agents import demo_data
from app.agents.llm import available_providers, demo_fallback_enabled
from app.config import settings
from app.services.extraction.text_parser import ParsedEntity, ParsedField

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = """You are a clinical document extraction engine for an Indian \
primary-care CDSS. Extract structured data from the supplied medical document \
(prescription, lab report, or discharge summary).

Return ONLY a JSON object with this shape:
{
  "document_type": "prescription|lab_report|discharge_summary|other",
  "entities": [
    {"entity_type": "medication", "fields": {"brand_name_raw": str, "dose": str,
      "dose_unit": str, "frequency": str, "route": str, "event_type": "continue|start|stop"},
     "confidence": {"brand_name_raw": 0-1, "dose": 0-1, ...}},
    {"entity_type": "lab_result", "fields": {"marker_name": str, "value_numeric": number,
      "unit": str, "reference_range_low": number, "reference_range_high": number},
     "confidence": {...}},
    {"entity_type": "condition", "fields": {"condition_name": str, "status": str}, "confidence": {...}},
    {"entity_type": "allergy", "fields": {"allergen_name": str, "reaction_description": str},
     "confidence": {...}}
  ]
}
Per-field confidence in [0,1] reflects extraction certainty. Never invent data; if a field \
is illegible, omit it. Preserve original Indian brand names verbatim in brand_name_raw."""

_MEDIA_TYPES = {
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
}


def is_available() -> bool:
    return bool(available_providers())


def demo_active() -> bool:
    """True when no vision provider is configured but the demo fallback should serve samples."""
    return demo_fallback_enabled() and not available_providers()


def demo_model_label() -> str:
    return f"{demo_data.DEMO_TAG} simulated-extraction"


def demo_extract() -> tuple[list[ParsedEntity], str | None]:
    """Return simulated sample extraction (clearly marked) when no LLM is available."""
    return _to_entities(demo_data.extraction_payload())


def _to_entities(payload: dict) -> tuple[list[ParsedEntity], str | None]:
    entities: list[ParsedEntity] = []
    for raw in payload.get("entities", []):
        etype = raw.get("entity_type")
        fields_map = raw.get("fields", {})
        conf_map = raw.get("confidence", {})
        fields = [
            ParsedField(name=k, value=v, confidence=float(conf_map.get(k, 0.7)))
            for k, v in fields_map.items()
            if v is not None
        ]
        if fields:
            entities.append(ParsedEntity(entity_type=etype, fields=fields))
    return entities, payload.get("document_type")


_MODEL_BY_PROVIDER = {
    "openai": lambda: settings.openai_model,
    "openrouter": lambda: settings.openrouter_model,
    "anthropic": lambda: settings.anthropic_model,
}


def model_label() -> str:
    """Human-readable label of the provider/model that would handle extraction."""
    providers = available_providers()
    resolver = _MODEL_BY_PROVIDER.get(providers[0] if providers else "", None)
    return resolver() if resolver else settings.anthropic_model


def _extract_openai(file_bytes: bytes, file_type: str) -> str:
    """OpenAI (GPT) vision extraction. Handles images via data URLs.

    PDFs are not supported on the chat-completions vision path; raise so the caller can
    fall back to the next provider (Anthropic) or the deterministic parser.
    """
    if file_type not in _MEDIA_TYPES:
        raise RuntimeError(f"OpenAI vision path does not support file type: {file_type}")

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key, timeout=settings.llm_request_timeout_seconds)
    data_url = (
        f"data:{_MEDIA_TYPES[file_type]};base64,{base64.b64encode(file_bytes).decode('ascii')}"
    )
    completion = client.chat.completions.create(
        model=settings.openai_model,
        max_tokens=settings.openai_max_tokens,
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract structured clinical data."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    )
    return completion.choices[0].message.content or ""


def _extract_openrouter(file_bytes: bytes, file_type: str) -> str:
    """OpenRouter vision extraction (OpenAI-compatible API, custom base_url).

    Images go through the standard ``image_url`` data-URL part. PDFs use OpenRouter's
    ``file`` content part, which its PDF-capable models accept. Anything else raises so the
    caller can fall back to the next provider or the deterministic parser.
    """
    from openai import OpenAI

    if file_type in _MEDIA_TYPES:
        data_url = (
            f"data:{_MEDIA_TYPES[file_type]};base64,{base64.b64encode(file_bytes).decode('ascii')}"
        )
        media_part: Any = {"type": "image_url", "image_url": {"url": data_url}}
    elif file_type == "pdf":
        data_url = f"data:application/pdf;base64,{base64.b64encode(file_bytes).decode('ascii')}"
        media_part = {
            "type": "file",
            "file": {"filename": "document.pdf", "file_data": data_url},
        }
    else:
        raise RuntimeError(f"OpenRouter vision path does not support file type: {file_type}")

    client = OpenAI(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        timeout=settings.llm_request_timeout_seconds,
        default_headers={
            "HTTP-Referer": "https://documedic.aiknol.com",
            "X-Title": "Documedic (Aether Clinician)",
        },
    )
    # Typed as list[Any]: the content part is a union (image_url vs OpenRouter's file part),
    # which the openai SDK's per-role TypedDicts cannot narrow.
    messages: list[Any] = [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Extract structured clinical data."},
                media_part,
            ],
        },
    ]
    completion = client.chat.completions.create(
        model=settings.openrouter_model,
        max_tokens=settings.openrouter_max_tokens,
        messages=messages,
    )
    return completion.choices[0].message.content or ""


def _extract_anthropic(file_bytes: bytes, file_type: str) -> str:
    import anthropic

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key, timeout=settings.llm_request_timeout_seconds
    )

    content: list[dict] = [{"type": "text", "text": "Extract structured clinical data."}]
    if file_type in _MEDIA_TYPES:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _MEDIA_TYPES[file_type],
                    "data": base64.b64encode(file_bytes).decode("ascii"),
                },
            }
        )
    elif file_type == "pdf":
        content.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(file_bytes).decode("ascii"),
                },
            }
        )
    else:
        raise RuntimeError(f"Unsupported file type for vision extraction: {file_type}")

    message = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=settings.anthropic_max_tokens,
        system=EXTRACTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(block.text for block in message.content if block.type == "text")


_EXTRACTORS = {
    "openai": _extract_openai,
    "openrouter": _extract_openrouter,
    "anthropic": _extract_anthropic,
}


def extract(file_bytes: bytes, file_type: str) -> tuple[list[ParsedEntity], str | None]:
    """Run vision extraction and return (entities, document_type).

    Tries each configured provider in ``available_providers()`` priority order (the
    configured primary first — OpenRouter, OpenAI, or Anthropic — then the rest). Raises
    RuntimeError when no provider succeeds so the pipeline can fall back deterministically.

    Failures are logged with provider and exception type only — never the document bytes or
    the model response, which carry patient data.
    """
    providers = available_providers()
    if not providers:
        raise RuntimeError("No LLM provider API key configured")

    last_err: Exception | None = None
    for provider in providers:
        extractor = _EXTRACTORS.get(provider)
        if extractor is None:  # pragma: no cover — guards against a new provider name
            continue
        try:
            text = extractor(file_bytes, file_type)
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end == -1:
                raise RuntimeError("No JSON object in model response")
            payload = json.loads(text[start : end + 1])
            return _to_entities(payload)
        except Exception as exc:  # noqa: BLE001 — try next provider, then deterministic path
            last_err = exc
            logger.warning(
                "Vision extraction via %r failed for file_type=%s: %s: %s",
                provider,
                file_type,
                type(exc).__name__,
                str(exc)[:200],
            )
    raise RuntimeError(str(last_err))
