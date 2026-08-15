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
from app.agents.circuit import breaker, is_availability_failure
from app.agents.llm import available_providers, callable_providers, demo_fallback_enabled
from app.config import settings
from app.core.logsafe import describe_exception
from app.services.extraction.text_parser import ParsedEntity, ParsedField

# The merge is the authority on what can be charted, so the set it dispatches on is the set this
# parser is allowed to emit. Imported rather than restated because a second copy of this list is
# exactly how the three that had drifted apart came to disagree.
from app.services.graph_service import MERGEABLE_ENTITY_TYPES

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
      "reference_range_text": str, "sample_date": "YYYY-MM-DD"},
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
doctor) as lab_result entities — they are not clinical markers.

Reference ranges: set "reference_range_text" to the interval exactly as printed beside the \
result ("< 200", "Up to 40", "0.4 - 4.0", "Negative"), and set the numeric bounds only where \
the printed range gives them. A one-sided range fills one bound and leaves the other absent — \
"< 200" is reference_range_high 200 with no low, and "> 40" is reference_range_low 40 with no \
high. Do not turn a one-sided range into a two-ended one by supplying a bound the report does \
not state, and do not swap the ends: the low bound must never exceed the high bound."""

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


def _usable(value: object) -> bool:
    """Whether a model-supplied field value is one that can be carried onwards.

    Rejects ``None`` (the prompt asks for illegible fields to be omitted, and some models send
    an explicit null instead) and non-finite numbers. ``json.loads`` turns ``1e999`` into ``inf``
    and the literal ``NaN`` into a float without complaining, and neither can be serialised
    back: the approval response 500s with "Out of range float values are not JSON compliant"
    and the document's JSON meta column gets the non-standard token ``Infinity``. Unlike the
    deterministic parser, there is no source token to fall back to here -- the raw text was
    consumed by the model -- so the field is dropped. See ``text_parser._finite``.
    """
    if value is None:
        return False
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    return True


def _to_entities(payload: object) -> tuple[list[ParsedEntity], str | None]:
    """Build entities from a model response, keeping only the parts that are shaped as asked.

    Every level is checked rather than trusted. A model that returns ``"entities"`` as a list of
    bare strings, or ``"fields"`` as a string, made ``.get``/``.items`` an AttributeError that
    aborted the whole extraction -- ``extract`` caught it and moved to the next provider, so a
    single malformed entity cost the document every *other* entity that had extracted perfectly
    well, and usually the vision path entirely. Dropping the unusable entries leaves the rest.
    """
    if not isinstance(payload, dict):
        return [], None
    entities: list[ParsedEntity] = []
    raw_entities = payload.get("entities")
    for raw in raw_entities if isinstance(raw_entities, (list | tuple)) else []:
        if not isinstance(raw, dict):
            continue
        etype = raw.get("entity_type")
        if not isinstance(etype, str) or not etype.strip():
            # Nothing downstream can route an entity with no type: GraphService dispatches on
            # it, so an untyped entity is silently merged nowhere while still being shown to
            # the clinician for approval — which reads as "recorded".
            continue
        etype = etype.strip().lower()
        if etype not in MERGEABLE_ENTITY_TYPES:
            # The same failure one step over, and the one that actually happens: a *typed*
            # entity of a type nothing charts. The prompt asks for four types and the model
            # answers with a fifth — "vital_sign", "procedure", "immunization" are all things a
            # discharge summary contains and a plausible model volunteers. That entity passed
            # the guard above, was stored, was drawn on the review screen with a tick-box
            # reading "include in the record", was ticked, and was written nowhere.
            #
            # Refused here rather than at the merge because this is the boundary where the
            # model's output stops being a suggestion: past it, an entity is something a
            # clinician is being asked to approve, and offering to record something that cannot
            # be recorded is the whole defect. The rest of the document is unaffected — a
            # discharge summary's drugs still extract when its vitals do not.
            logger.warning(
                "Extraction returned entity_type %r, which nothing charts; dropping it.", etype
            )
            continue
        fields_map = raw.get("fields")
        conf_map = raw.get("confidence")
        if not isinstance(conf_map, dict):
            conf_map = {}
        fields = [
            ParsedField(name=k, value=v, confidence=_confidence(conf_map.get(k)))
            for k, v in (fields_map.items() if isinstance(fields_map, dict) else [])
            if isinstance(k, str) and _usable(v)
        ]
        if fields:
            entities.append(ParsedEntity(entity_type=etype, fields=fields))
    _inherit_document_date(entities, payload.get("document_date"))
    doc_type = payload.get("document_type")
    return entities, doc_type.strip() if isinstance(doc_type, str) and doc_type.strip() else None


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

    Shares ``app.agents.circuit``'s breaker with the reasoning engine, because it shares the
    providers. Extraction runs *inline in the upload request*, so during a provider outage this
    is the path where the timeout is most visible: a clinician uploading a prescription waited
    the full chain — three providers at ``llm_request_timeout_seconds`` each — before the
    deterministic text/OCR parser they were always going to end up on got a look at the file.
    A tripped breaker skips straight to it.

    A provider that cannot handle the *file type* raises before any network call, and that is
    not an availability failure: it is a fact about PDFs and the chat-completions vision path,
    and it must not accumulate towards taking OpenAI out of service for the reasoning engine.
    :func:`~app.agents.circuit.is_availability_failure` is what draws that line.
    """
    if not available_providers():
        raise RuntimeError("No LLM provider API key configured")

    providers = callable_providers()
    last_err: Exception | None = None
    for provider in providers:
        extractor = _EXTRACTORS.get(provider)
        if extractor is None:  # pragma: no cover — guards against a new provider name
            continue
        if not breaker.allow(provider):
            continue
        try:
            text = extractor(file_bytes, file_type)
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end == -1:
                raise RuntimeError("No JSON object in model response")
            payload = json.loads(text[start : end + 1])
        except Exception as exc:  # noqa: BLE001 — try next provider, then deterministic path
            last_err = exc
            reason = describe_exception(exc)
            logger.warning(
                "Vision extraction via %r failed for file_type=%s: %s",
                provider,
                file_type,
                reason,
            )
            if is_availability_failure(exc):
                breaker.record_failure(provider, reason=reason)
            else:
                breaker.record_success(provider)
        else:
            breaker.record_success(provider)
            return _to_entities(payload)
    if last_err is None:
        # Nothing was called: every configured provider is in cooldown. Named as such rather
        # than reported as "unknown", which is what describe_exception(None) would say and which
        # would send an operator looking for a request that was never made.
        raise RuntimeError("Every configured LLM provider is circuit-broken")
    # The document bytes went upstream, so an upstream error can quote them back — and on the
    # failure that matters most (a reply with no JSON object in it) the exception text is the
    # model's response, which is the chart. See app.core.logsafe.
    raise RuntimeError(describe_exception(last_err))
