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


def objects(value: Any) -> list[dict[str, Any]]:
    """The dict entries of a model-supplied list, with everything else dropped.

    Every agent reads its output as a list of objects -- ``result.get("hypotheses", [])``, then
    ``h.get("diagnosis_name")`` on each item. A model that answers with a list of bare strings
    (``"hypotheses": ["Angina", "GERD"]``) makes that ``.get`` an AttributeError, and a model
    that answers with an object instead of a list makes it iterate the *keys*, which are also
    strings, for the same result. Either way the exception escapes the agent's ``run``, and no
    node in ``graph.run_reasoning`` is wrapped -- so one malformed field fails the whole
    reasoning session, after the model had already answered.

    What makes that worse than it sounds is that every one of these agents has a deterministic
    fallback for exactly this situation, and none of them get to use it: the crash happens while
    parsing the response, past the point where "the model gave us nothing usable" would have
    routed to the offline path. Dropping the unusable entries instead leaves the caller with an
    empty list, which is the input its fallback is already written for.

    The Verifier has guarded its own ``verdicts`` list this way since it was hardened (see
    ``verifier._recognised``); this is that guard, shared, for the six nodes that did not get it.
    """
    if not isinstance(value, (list | tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def text_list(value: Any) -> list[str]:
    """Coerce a model-supplied "list of strings" field to exactly that.

    Three shapes have to survive, because models produce all three where a list of sentences was
    asked for. A list is read item by item, through ``as_text``, so an item that arrived as an
    object is flattened rather than dropped. A bare string is *one* item, not a list of
    characters -- which is what ``list("...")`` would have made of it, and what iterating it
    downstream does. Anything else carries no readable text and yields nothing.

    Empty entries are dropped: they reach the Reasoning Theatre as list items, and the frontend
    already defends against them, so they are real rather than hypothetical.
    """
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, (list | tuple)):
        return []
    return [text for text in (as_text(v) for v in value) if text]


def as_float(value: Any, default: float) -> float:
    """A float from a model-supplied value, falling back when it is not a usable number.

    ``float(result.get("info_gain_score", 0.5))`` is how the intake round read its own score, and
    a model that answered ``"high"`` -- or ``null``, which is what "I don't know" tends to look
    like -- raised straight out of the first request a clinician makes on a case. Booleans are
    rejected rather than read as 1.0/0.0: ``True`` where a score belongs is a non-answer, not a
    score of one. Non-finite values are rejected for the same reason, since NaN silently defeats
    every threshold comparison downstream.
    """
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result and result not in (float("inf"), float("-inf")) else default


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
