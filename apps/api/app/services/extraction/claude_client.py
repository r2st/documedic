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
from app.core.logsafe import describe_exception
from app.services.extraction.text_parser import ParsedEntity, ParsedField

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = """You are a clinical document extraction engine for an Indian \
primary-care CDSS. Extract structured data from the supplied medical document \
(prescription, lab report, or discharge summary).

Return ONLY a JSON object with this shape:
{
  "document_type": "prescription|lab_report|discharge_summary|other",
  "document_date": "YYYY-MM-DD",
  "entities": [
    {"entity_type": "medication", "fields": {"brand_name_raw": str, "dose": str,
      "dose_unit": str, "frequency": str, "route": str, "event_type": "continue|start|stop"},
     "confidence": {"brand_name_raw": 0-1, "dose": 0-1, ...}},
    {"entity_type": "lab_result", "fields": {"marker_name": str, "value_numeric": number,
      "unit": str, "reference_range_low": number, "reference_range_high": number,
      "sample_date": "YYYY-MM-DD"},
     "confidence": {...}},
    {"entity_type": "condition", "fields": {"condition_name": str, "status": str}, "confidence": {...}},
    {"entity_type": "allergy", "fields": {"allergen_name": str, "reaction_description": str},
     "confidence": {...}}
  ]
}
Per-field confidence in [0,1] reflects extraction certainty. Never invent data; if a field \
is illegible, omit it. Preserve original Indian brand names verbatim in brand_name_raw.

Dates: "document_date" is the date the sample was collected or the prescription written, as \
printed on the document — prefer a collection/sample date over a report or printing date. Set \
"sample_date" on a lab_result only when that individual result is dated differently from the \
rest of the document; otherwise omit it and it inherits "document_date". Never guess a date \
that is not printed on the document: omit the field instead. Do NOT emit the document's \
administrative header lines (collection date, patient age, accession or bill number, referring \
doctor) as lab_result entities — they are not clinical markers."""

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
            ParsedField(name=k, value=v, confidence=_confidence(conf_map.get(k)))
            for k, v in fields_map.items()
            if v is not None
        ]
        if fields:
            entities.append(ParsedEntity(entity_type=etype, fields=fields))
    _inherit_document_date(entities, payload.get("document_date"))
    return entities, payload.get("document_type")


def _confidence(raw: object) -> float:
    """A per-field confidence from the model, defaulting when it is missing or unusable.

    The model is asked for a number in [0,1] per field and mostly obliges, but "high" and null
    both turn up, and ``float()`` on either raised straight out of extraction — losing a whole
    document's worth of correctly-read values over one malformed score. Out-of-range numbers
    are clamped rather than dropped: the band they land in is what the value means.
    """
    try:
        return min(1.0, max(0.0, float(raw)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.7


def _inherit_document_date(entities: list[ParsedEntity], document_date: object) -> None:
    """Stamp the document's date onto lab results the model did not date individually.

    A lab report prints one collection date in its header and no date at all next to each
    marker, so asking the model to repeat it on every result invites it to invent one where a
    panel spans two draws. It reports the date once; results that need a different one override
    it. Mirrors ``text_parser._apply_sample_date`` so both extraction paths land the same shape
    in ``GraphService._merge_lab``.
    """
    if not isinstance(document_date, str) or not document_date.strip():
        return
    for entity in entities:
        if entity.entity_type != "lab_result":
            continue
        if any(f.name == "sample_date" for f in entity.fields):
            continue
        entity.fields.append(ParsedField("sample_date", document_date.strip(), 0.8))


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

    # list[Any] rather than list[dict]: the SDK types ``content`` as an iterable of block
    # TypedDicts, and a plain dict literal list is not assignable to that union.
    content: list[Any] = [{"type": "text", "text": "Extract structured clinical data."}]
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
                "Vision extraction via %r failed for file_type=%s: %s",
                provider,
                file_type,
                describe_exception(exc),
            )
    # The document bytes went upstream, so an upstream error can quote them back — and on the
    # failure that matters most (a reply with no JSON object in it) the exception text is the
    # model's response, which is the chart. See app.core.logsafe.
    raise RuntimeError(describe_exception(last_err))
