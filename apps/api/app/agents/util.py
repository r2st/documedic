"""Shared helpers for agent nodes: safe LLM calls and band/keyword utilities."""

from __future__ import annotations

import asyncio
from typing import Any

from app.agents.context import ReasoningContext
from app.agents.llm import LLMUnavailable

_BANDS = {"high", "moderate", "low", "very_low"}


async def call_llm(
    ctx: ReasoningContext, system: str, user: str, *, verifier: bool = False
) -> dict[str, Any] | None:
    """Call Claude off the event loop. Return parsed JSON, or None to trigger fallback."""
    client = ctx.verifier_llm if verifier else ctx.llm
    if not client.available():
        return None
    try:
        return await asyncio.to_thread(client.complete_json, system, user)
    except LLMUnavailable:
        return None


def norm_band(value: Any, default: str = "low") -> str:
    band = str(value or "").strip().lower()
    return band if band in _BANDS else default


def as_text(value: Any) -> str:
    """Flatten an LLM-provided value to a plain string.

    Models that don't follow the requested schema sometimes return an object or list where a
    string was asked for — e.g. a hypothesis ``rationale`` arriving as
    ``{"evidence": ..., "probability": ...}``. Such a value would later be rendered directly as
    a React child and white-screen the UI, so we coerce it to readable text here. Empty/None
    yields "" so callers can treat it as "no value".
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        # Prefer a common free-text key; otherwise join the scalar values readably.
        for key in ("text", "rationale", "summary", "reason", "explanation", "evidence", "detail"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        return "; ".join(p for p in (as_text(v) for v in value.values()) if p)
    if isinstance(value, (list | tuple)):
        return "; ".join(p for p in (as_text(v) for v in value) if p)
    return str(value)


_NEGATIVE = {"no", "none", "nil", "denies", "negative", "n", "no.", "none.", "absent"}


def text_blob(state_complaint: str, snapshot_summary: str, intake: list[Any]) -> str:
    """Build the matching text from the complaint, record summary and AFFIRMATIVE answers.

    Negative answers ("no", "denies") deliberately do NOT carry the red-flag keywords from
    their question text, so a screened-out symptom cannot falsely trigger a can't-miss match.
    """
    parts = [state_complaint, snapshot_summary]
    for q in intake:
        answer = getattr(q, "answer", None)
        if not answer:
            continue
        if str(answer).strip().lower() in _NEGATIVE:
            continue
        parts.append(f"{q.text} {answer}")
    return "\n".join(parts)
