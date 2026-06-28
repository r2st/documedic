"""Multimodal vision extraction client (production path).

Provider-agnostic: OpenAI (GPT) vision is the primary path, Anthropic Claude is the
configurable fallback (same selection logic as the reasoning engine —
``settings.llm_provider`` / ``settings.llm_fallback_enabled``). The chosen SDK is sent
the document with a structured-output instruction and the JSON response is parsed into
ParsedEntity objects. When no provider can handle the document, callers fall back to the
deterministic text parser.

Note: OpenAI vision handles image inputs; PDF documents are routed to Anthropic when
available, otherwise the deterministic text/OCR path is used.
"""

from __future__ import annotations

import base64
import json

from app.agents.llm import available_providers
from app.config import settings
from app.services.extraction.text_parser import ParsedEntity, ParsedField

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


def model_label() -> str:
    """Human-readable label of the provider/model that would handle extraction."""
    providers = available_providers()
    if providers and providers[0] == "openai":
        return settings.openai_model
    return settings.anthropic_model


def _extract_openai(file_bytes: bytes, file_type: str) -> str:
    """OpenAI (GPT) vision extraction. Handles images via data URLs.

    PDFs are not supported on the chat-completions vision path; raise so the caller can
    fall back to the next provider (Anthropic) or the deterministic parser.
    """
    if file_type not in _MEDIA_TYPES:
        raise RuntimeError(f"OpenAI vision path does not support file type: {file_type}")

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    data_url = f"data:{_MEDIA_TYPES[file_type]};base64,{base64.b64encode(file_bytes).decode('ascii')}"
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


def _extract_anthropic(file_bytes: bytes, file_type: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

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


def extract(file_bytes: bytes, file_type: str) -> tuple[list[ParsedEntity], str | None]:
    """Run vision extraction and return (entities, document_type).

    Tries each configured provider in priority order (OpenAI primary, Anthropic fallback).
    Raises RuntimeError when no provider succeeds so the pipeline can fall back
    deterministically.
    """
    providers = available_providers()
    if not providers:
        raise RuntimeError("No LLM provider API key configured")

    last_err: Exception | None = None
    for provider in providers:
        try:
            if provider == "openai":
                text = _extract_openai(file_bytes, file_type)
            else:
                text = _extract_anthropic(file_bytes, file_type)
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end == -1:
                raise RuntimeError("No JSON object in model response")
            payload = json.loads(text[start : end + 1])
            return _to_entities(payload)
        except Exception as exc:  # noqa: BLE001 — try next provider, then deterministic path
            last_err = exc
    raise RuntimeError(str(last_err))
