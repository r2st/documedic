"""LLM client for the reasoning agents (architecture §7.1).

Provider-agnostic wrapper that returns parsed JSON for structured agent output. The
fallback chain is OpenAI (GPT, primary) -> Anthropic Claude -> OpenRouter -> simulated
demo data (see ``settings.llm_provider`` / ``settings.llm_fallback_enabled`` /
``settings.llm_openrouter_fallback`` / ``settings.llm_demo_fallback``). Each provider SDK
is imported lazily and only used when its API key is configured. When no provider is
available (no key / offline / error) callers fall back to deterministic reasoning, and
the case is marked ``degraded`` — the system never silently produces output.

Temperature is fixed at 0.0 for deterministic clinical output. The verifier uses a
separate client instance with no view of other agents' chain-of-thought (independent
re-check).
"""

from __future__ import annotations

import json
from typing import Any

from app.agents import demo_data
from app.config import settings


class LLMUnavailable(RuntimeError):
    """Raised when no LLM provider can be reached so the caller can degrade gracefully."""


def _provider_order() -> list[str]:
    """Fallback order: primary, then the other openai/anthropic, then OpenRouter."""
    primary = settings.llm_provider
    order = [primary]
    if settings.llm_fallback_enabled:
        order.append("anthropic" if primary == "openai" else "openai")
    if settings.llm_openrouter_fallback:
        order.append("openrouter")
    return order


def _key_for(provider: str) -> str:
    if provider == "openai":
        return settings.openai_api_key
    if provider == "anthropic":
        return settings.anthropic_api_key
    if provider == "openrouter":
        return settings.openrouter_api_key
    return ""


def available_providers() -> list[str]:
    """Providers (in priority order) that have an API key configured."""
    return [p for p in _provider_order() if _key_for(p)]


def demo_fallback_enabled() -> bool:
    """Whether the simulated-response safety net is allowed to kick in."""
    return settings.llm_demo_fallback


def using_simulated_llm() -> bool:
    """True when the engine is CURRENTLY serving simulated demo data.

    This is the case when no provider key is configured and the demo fallback is enabled, so
    every ``complete_json`` call returns simulated output. (When keys exist but all calls fail
    mid-run, individual responses carry a ``_demo`` marker instead.)
    """
    return demo_fallback_enabled() and not available_providers()


def is_available() -> bool:
    """True when the engine can produce LLM-style output — a real provider OR the demo net."""
    return bool(available_providers()) or demo_fallback_enabled()


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


def _complete_openrouter(system: str, user: str, model: str, max_tokens: int) -> str:
    # OpenRouter exposes an OpenAI-compatible API, so reuse the openai SDK with a
    # custom base_url. The optional referer/title headers identify the app to OpenRouter.
    from openai import OpenAI

    client = OpenAI(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        default_headers={
            "HTTP-Referer": "https://documedic.aiknol.com",
            "X-Title": "Documedic (Aether Clinician)",
        },
    )
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


class LLMClient:
    """Synchronous, provider-agnostic client (called from a worker thread by the orchestrator).

    Tries each configured provider in priority order (OpenAI primary, then Anthropic, then
    OpenRouter), so a transient failure or missing key on one degrades to the next rather
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
        if provider == "openrouter":
            model = self._model_override or settings.openrouter_model
            max_tokens = self._max_tokens_override or settings.openrouter_max_tokens
            return _complete_openrouter(system, user, model, max_tokens)
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
            # No provider configured: serve simulated data if the demo net is on, else degrade.
            if demo_fallback_enabled():
                return demo_data.simulated_response(system, user)
            raise LLMUnavailable("No LLM provider API key configured")

        last_err: Exception | None = None
        for provider in providers:
            for _ in range(retries + 1):
                try:
                    return _extract_json(self._complete(provider, system, user))
                except Exception as exc:  # noqa: BLE001 — surfaced to caller as LLMUnavailable
                    last_err = exc
        # Every configured provider failed. Final safety net: simulated demo data if enabled.
        if demo_fallback_enabled():
            return demo_data.simulated_response(system, user)
        raise LLMUnavailable(str(last_err))
