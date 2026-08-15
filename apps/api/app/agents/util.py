"""Shared helpers for agent nodes: safe LLM calls and band/keyword utilities."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.agents.context import ReasoningContext
from app.agents.llm import LLMUnavailable
from app.agents.untrusted import UNTRUSTED_DATA_FRAMING
from app.config import settings

logger = logging.getLogger(__name__)

_BANDS = {"high", "moderate", "low", "very_low"}

_llm_executor: ThreadPoolExecutor | None = None
_llm_executor_lock = threading.Lock()


def llm_executor() -> ThreadPoolExecutor:
    """The thread pool provider SDK calls run on — deliberately not the loop's default one.

    The SDKs are synchronous, so a call has to leave the event loop, and ``asyncio.to_thread``
    would put it on the default executor. That executor is shared with everything else in the
    process that leaves the loop: bcrypt behind ``verify_password``, document blob I/O, upload
    hashing, the guideline embedder. It holds ``min(32, cpu_count + 4)`` threads — six on the box
    this runs on — and one reasoning run's hypothesis panel takes four of them at once.

    A provider that accepts the connection and then stops answering holds each of those threads
    for the full retry-and-failover ladder, minutes at a time. Two clinicians running the panel
    during such an outage filled the pool, and every unrelated request that needed a thread
    queued behind them: logging in stopped working because a third party's socket was hanging.
    An LLM outage is supposed to degrade reasoning, which the deterministic path exists for, and
    nothing else.

    Bounded at ``settings.llm_max_concurrent_calls``, so saturation is contained here: reasoning
    queues for a slot while auth, uploads and the record continue at full speed. Built lazily
    under a lock because the setting is read at first use rather than import — tests and workers
    both override it — and because a module-level pool would start threads in every process that
    imports an agent, including ones that never make a call.
    """
    global _llm_executor
    if _llm_executor is None:
        with _llm_executor_lock:
            if _llm_executor is None:
                _llm_executor = ThreadPoolExecutor(
                    max_workers=max(1, settings.llm_max_concurrent_calls),
                    thread_name_prefix="llm",
                )
    return _llm_executor


def reset_llm_executor() -> None:
    """Drop the pool so the next call rebuilds it at the current size. For tests."""
    global _llm_executor
    with _llm_executor_lock:
        executor, _llm_executor = _llm_executor, None
    if executor is not None:
        executor.shutdown(wait=False)


async def call_llm(
    ctx: ReasoningContext, system: str, user: str, *, verifier: bool = False
) -> dict[str, Any] | None:
    """Call Claude off the event loop. Return parsed JSON, or None to trigger fallback.

    Every system prompt leaves here carrying :data:`~app.agents.untrusted.UNTRUSTED_DATA_FRAMING`,
    appended at this one point rather than written into each prompt in
    :mod:`app.agents.prompts`. The user message is assembled from patient-record text that
    reached us through OCR of a document someone handed over, so the boundary clause is a
    property of *making an LLM call at all*, not of any one agent remembering it. An agent added
    later (the module pattern in CLAUDE.md invites exactly that) gets it without knowing it
    exists, and cannot be the one that ships without it.

    It goes last so it is the final thing in the system prompt, after each agent's own hard
    rules — closest to the untrusted text it governs.

    Refuses to start a call once the run is out of budget (``ReasoningContext.deadline``), and
    returns ``None`` to do it — the same answer a provider failure gives, so every agent's
    existing deterministic fallback handles it and the run comes back marked ``degraded`` and
    escalated to flag-for-review. Nothing bounded a run before that: ``llm_request_timeout_seconds``
    bounds one socket and the run lease bounds one *claim*, but a provider that hung rather than
    refusing was retried and failed over at every one of the pipeline's nodes in turn, and the
    clinician watched a Theatre that had stopped emitting for half an hour. See
    ``settings.reasoning_llm_budget_seconds`` for why the check is here, before the call, rather
    than as a cancellation around it.

    The check is deliberately not an error. A run that runs out of budget has already produced
    real output from the nodes that answered in time, and the deterministic path covers the rest;
    failing the run instead would throw away both and leave the clinician with nothing.
    """
    client = ctx.verifier_llm if verifier else ctx.llm
    if not client.available():
        return None
    if ctx.out_of_budget():
        logger.warning(
            "Reasoning run exceeded its LLM budget of %ss; remaining agents will run on the "
            "deterministic path and the case will be marked degraded.",
            settings.reasoning_llm_budget_seconds,
        )
        return None
    framed = f"{system}\n{UNTRUSTED_DATA_FRAMING}"
    # What ``asyncio.to_thread`` does, against our own pool instead of the default one: the
    # current context is copied so contextvars set per request (the request id the logs are
    # correlated by) still resolve inside the worker thread.
    context = contextvars.copy_context()

    def call() -> dict[str, Any]:
        return context.run(client.complete_json, framed, user)

    try:
        return await asyncio.get_running_loop().run_in_executor(llm_executor(), call)
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
