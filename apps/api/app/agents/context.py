"""Shared execution context passed to every agent node.

Carries the LLM clients (a separate one for the independent Verifier), a guideline retriever
callable (injected by the ReasoningService), a deterministic drug-safety evaluator, and an async
event emitter used to stream the Reasoning Theatre over SSE.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.agents.llm import LLMClient

# (query, k) -> list of guideline-chunk dicts with keys:
#   section_id, source, document_title, heading, content, score, corpus_version, page_range
#
# May be sync or async. The retriever the ReasoningService injects is async because the dense
# re-ranker embeds the query on a worker thread; the empty default and the ones tests inject are
# ordinary functions, and requiring those to be coroutines would be ceremony for no gain at an
# injection point whose whole purpose is to be easy to substitute. ``resolve_retrieved`` is how
# agents consume either.
Retriever = Callable[[str, int], "list[dict[str, Any]] | Awaitable[list[dict[str, Any]]]"]
# Text naming zero or more drugs -> the deterministic engine's flags for those drugs against
# this patient. "" means "the patient's current medications", which is what the safety node's
# first pass asks for; any other string is screened for the drug names inside it, which is how a
# guideline management option gets checked against the chart it is about to be shown beside.
#
# May be sync or async, for the same reason ``Retriever`` may: the evaluator the ReasoningService
# injects has to reach the database (the contraindication and interaction rules it needs depend
# on which drugs the text turns out to name, so they cannot all be preloaded), while the ones
# tests inject are ordinary functions. ``resolve_safety`` is how the node consumes either.
SafetyEvaluator = Callable[[str], "list[dict[str, Any]] | Awaitable[list[dict[str, Any]]]"]
EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _noop_emit(event: str, data: dict[str, Any]) -> None:  # pragma: no cover
    return None


def _empty_retriever(query: str, k: int) -> list[dict[str, Any]]:
    return []


async def resolve_retrieved(result: Any) -> list[dict[str, Any]]:
    """Await a retriever's result if it is awaitable, else pass it through.

    ``ReasoningContext.retrieve`` is an injection point that may be either shape (see
    ``Retriever``). Awaiting the coroutine here rather than at each agent keeps the "did the
    injected callable happen to be async" question in one place.
    """
    return await result if inspect.isawaitable(result) else result


def _empty_safety(drug: str) -> list[dict[str, Any]]:
    return []


async def resolve_safety(result: Any) -> list[dict[str, Any]]:
    """Await a safety evaluator's result if it is awaitable, else pass it through.

    The twin of :func:`resolve_retrieved`, for the same reason and at the same kind of
    injection point — see :data:`SafetyEvaluator`.
    """
    return await result if inspect.isawaitable(result) else result


@dataclass
class ReasoningContext:
    llm: LLMClient = field(default_factory=LLMClient)
    verifier_llm: LLMClient = field(default_factory=LLMClient)
    retrieve: Retriever = _empty_retriever
    evaluate_safety: SafetyEvaluator = _empty_safety
    emit: EventEmitter = _noop_emit
    # Tuning knobs (architecture §3.2).
    info_gain_threshold: float = 0.35
    question_cap: int = 6
    retrieval_threshold: float = 0.75

    def llm_available(self) -> bool:
        return self.llm.available()

    def verifier_llm_available(self) -> bool:
        """Whether the *gate's* client is configured — which is not the same question.

        ``verifier_llm`` exists so the gate can be pointed at a different model from the agents
        it checks. Once it can be a different client it can be separately unconfigured, and a
        node asking ``llm_available()`` about a call it made with ``verifier=True`` gets an
        answer about the wrong provider.
        """
        return self.verifier_llm.available()
