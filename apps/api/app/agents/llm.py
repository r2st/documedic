"""LLM client for the reasoning agents (architecture §7.1).

Provider-agnostic wrapper that returns parsed JSON for structured agent output. OpenAI
(GPT) is the PRIMARY provider; Anthropic Claude is a configurable fallback (see
``settings.llm_provider`` / ``settings.llm_fallback_enabled``). Each provider SDK is
imported lazily and only used when its API key is configured. When no provider is
available (no key / offline / error) callers fall back to deterministic reasoning, and
the case is marked ``degraded`` — the system never silently produces output.

Temperature is fixed at 0.0 for deterministic clinical output. The verifier uses a
separate client instance with no view of other agents' chain-of-thought (independent
re-check).
"""

from __future__ import annotations

import json
from typing import Any

from app.config import settings


class LLMUnavailable(RuntimeError):
    """Raised when no LLM provider can be reached so the caller can degrade gracefully."""


def _provider_order() -> list[str]:
    """Primary provider first, then the other as fallback when enabled."""
    primary = settings.llm_provider
    order = [primary]
    if settings.llm_fallback_enabled:
        order.append("anthropic" if primary == "openai" else "openai")
    return order


def _key_for(provider: str) -> str:
    return settings.openai_api_key if provider == "openai" else settings.anthropic_api_key


def available_providers() -> list[str]:
    """Providers (in priority order) that have an API key configured."""
    return [p for p in _provider_order() if _key_for(p)]


def is_available() -> bool:
    return bool(available_providers())


def _extract_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMUnavailable("No JSON object in model response")
    return json.loads(text[start : end + 1])


def _complete_openai(system: str, user: str, model: str, max_tokens: int) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    completion = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return completion.choices[0].message.content or ""


def _complete_anthropic(system: str, user: str, model: str, max_tokens: int) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.0,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in message.content if b.type == "text")


class LLMClient:
    """Synchronous, provider-agnostic client (called from a worker thread by the orchestrator).

    Tries each configured provider in priority order (OpenAI primary, Anthropic fallback),
    so a transient failure or missing key on the primary degrades to the fallback rather
    than to the deterministic path.
    """

    def __init__(self, model: str | None = None, max_tokens: int | None = None) -> None:
        self._model_override = model
        self._max_tokens_override = max_tokens

    def available(self) -> bool:
        return is_available()

    def _complete(self, provider: str, system: str, user: str) -> str:
        if provider == "openai":
            model = self._model_override or settings.openai_model
            max_tokens = self._max_tokens_override or settings.openai_max_tokens
            return _complete_openai(system, user, model, max_tokens)
        model = self._model_override or settings.anthropic_model
        max_tokens = self._max_tokens_override or settings.anthropic_max_tokens
        return _complete_anthropic(system, user, model, max_tokens)

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        """Return the parsed JSON object from a single-turn completion.

        Tries each configured provider in order, retrying each before moving to the next.
        Raises ``LLMUnavailable`` when no provider succeeds so the agent degrades.
        """
        providers = available_providers()
        if not providers:
            raise LLMUnavailable("No LLM provider API key configured")

        last_err: Exception | None = None
        for provider in providers:
            for _ in range(retries + 1):
                try:
                    return _extract_json(self._complete(provider, system, user))
                except Exception as exc:  # noqa: BLE001 — surfaced to caller as LLMUnavailable
                    last_err = exc
        raise LLMUnavailable(str(last_err))
