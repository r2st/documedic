"""LLM client for the reasoning agents (architecture §7.1).

Provider-agnostic wrapper that returns parsed JSON for structured agent output. The
configured primary provider (``settings.llm_provider``: openai, anthropic, or openrouter)
is tried first, then the remaining providers as fallbacks, then simulated demo data — see
``_provider_order`` and ``settings.llm_fallback_enabled`` /
``settings.llm_openrouter_fallback`` / ``settings.llm_demo_fallback``. Each provider SDK
is imported lazily and only used when its API key is configured. When no provider is
available (no key / offline / error) callers fall back to deterministic reasoning, and
the case is marked ``degraded`` — the system never silently produces output.

Temperature is fixed at 0.0 for deterministic clinical output. The verifier uses a
separate client instance with no view of other agents' chain-of-thought (independent
re-check).

Every provider call is bounded by ``settings.llm_request_timeout_seconds`` so a hung upstream
connection fails over instead of blocking the calling worker thread indefinitely. Failures are
logged (provider, attempt, exception type — never the prompt/patient snapshot, which lives in
``system``/``user``) so degraded-mode episodes are diagnosable in production. Retries of the
same provider back off briefly between attempts to avoid hammering a struggling upstream, and a
provider that keeps failing is taken out of the chain by ``app.agents.circuit`` rather than
being rediscovered as broken on every agent call — see that module for why the timeout alone was
not enough.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.agents import demo_data
from app.agents.circuit import breaker, is_availability_failure
from app.config import settings
from app.core.logsafe import describe_exception

logger = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    """Raised when no LLM provider can be reached so the caller can degrade gracefully.

    ``log_safe_message`` says the message is written here rather than by a provider, so it may
    be logged in full — which is only true as long as every raise site passes provider errors
    through :func:`describe_exception` first. See ``app.core.logsafe``.
    """

    log_safe_message = True


def _provider_order() -> list[str]:
    """Provider priority order: the configured primary first, then the rest as fallbacks.

    The list is deduplicated so a provider named as the primary is never retried again as
    a fallback. Examples (with both fallback flags on):
      - llm_provider=openai     -> ["openai", "anthropic", "openrouter"]
      - llm_provider=anthropic  -> ["anthropic", "openai", "openrouter"]
      - llm_provider=openrouter -> ["openrouter", "openai", "anthropic"]
    With OpenRouter primary the slow OpenAI/Anthropic retries only happen if OpenRouter
    itself fails, so the common path returns without that latency.
    """
    primary: str = settings.llm_provider
    order: list[str] = [primary]
    if settings.llm_fallback_enabled:
        for provider in ("openai", "anthropic"):
            if provider not in order:
                order.append(provider)
    if settings.llm_openrouter_fallback and "openrouter" not in order:
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


def callable_providers() -> list[str]:
    """Configured providers whose circuit breaker is not currently open.

    Deliberately separate from :func:`available_providers`, which answers "is a key configured"
    and is what the health endpoints and the demo-net decisions are written against. Narrowing
    *that* to exclude tripped breakers would make ``llm_mode`` flip to ``offline`` during a
    transient blip, and would let a cooldown decide whether the simulated demo net engages — two
    couplings the breaker has no business having. This is the call-site question instead: which
    providers is it worth opening a socket to right now.
    """
    return [p for p in available_providers() if not breaker.is_open(p)]


def demo_fallback_enabled() -> bool:
    """Whether the simulated-response safety net is allowed to kick in.

    Never in production, whatever ``LLM_DEMO_FALLBACK`` says. ``assert_production_config``
    already refuses to start a production process with the flag on, but that gate lives in the
    app lifespan — a worker that skips lifespan, or any code path importing the agents directly,
    would otherwise still be able to hand a clinician fabricated ``[DEMO MODE]`` reasoning about
    a real patient. Production degrades to the deterministic offline path instead.
    """
    return settings.llm_demo_fallback and not settings.is_production


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


# --- Reachability probes -------------------------------------------------------------------
#
# Everything above answers "is a key configured", which is what the health endpoints reported
# as ``llm_mode: live``. A key is not a working provider: revoked, expired, out of quota, or
# simply unreachable from this network, it still reads as configured. So a deployment whose
# OpenRouter key had lapsed published ``live`` while every reasoning run degraded to
# ``offline_paused`` in front of a clinician — and in production the demo net is refused
# outright, so there is no fallback to soften it either. The operator's first signal was a
# clinician saying the engine had stopped working.
#
# The probe is the cheapest authenticated call each SDK offers (list models), bounded by its
# own short timeout — separate from ``llm_request_timeout_seconds``, because a probe that
# takes as long as a real completion is no use to a health endpoint — and cached, so a
# monitoring poller cannot turn a status page into provider traffic.

_probe_cache: dict[str, tuple[float, bool, str | None]] = {}


def _probe_openai_compatible(api_key: str, base_url: str | None) -> None:
    from openai import OpenAI

    # base_url=None is what the SDK already means by "use the default endpoint", so OpenAI and
    # OpenRouter differ only in this argument.
    OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=settings.llm_health_probe_timeout_seconds,
    ).models.list()


def _probe_anthropic(api_key: str) -> None:
    import anthropic

    anthropic.Anthropic(
        api_key=api_key, timeout=settings.llm_health_probe_timeout_seconds
    ).models.list()


def _probe(provider: str) -> None:
    """Make the probe call for ``provider``, raising whatever the SDK raises."""
    if provider == "openai":
        _probe_openai_compatible(settings.openai_api_key, None)
    elif provider == "openrouter":
        _probe_openai_compatible(settings.openrouter_api_key, settings.openrouter_base_url)
    else:
        _probe_anthropic(settings.anthropic_api_key)


def probe_provider(provider: str) -> tuple[bool, str | None]:
    """Whether ``provider`` answers right now, and a log-safe reason when it does not.

    Cached for ``llm_health_probe_ttl_seconds`` per provider, including negative results: an
    unreachable provider is exactly the one whose probe is slowest, so re-running it on every
    poll would make the health endpoint hang for as long as the outage lasts.

    The reason comes from :func:`~app.core.logsafe.describe_exception`. A provider's own error
    text can quote the prompt on other paths, and while a models-list call carries no patient
    data, this value is returned over HTTP and the rule about never laundering upstream
    message text does not get a per-endpoint exception.
    """
    now = time.monotonic()
    cached = _probe_cache.get(provider)
    if cached is not None and now - cached[0] < settings.llm_health_probe_ttl_seconds:
        return cached[1], cached[2]

    try:
        _probe(provider)
        result: tuple[bool, str | None] = (True, None)
    except Exception as exc:  # noqa: BLE001 — any failure is "not reachable"
        logger.warning(
            "LLM provider %r failed its reachability probe: %s", provider, describe_exception(exc)
        )
        result = (False, describe_exception(exc))
    _probe_cache[provider] = (now, result[0], result[1])
    return result


def probe_all_providers() -> dict[str, dict[str, Any]]:
    """Reachability for every provider in the fallback chain, configured or not.

    Unconfigured providers are reported without being called — there is nothing to probe and
    no key to authenticate with — so a deployment with one provider makes one network call.
    """
    report: dict[str, dict[str, Any]] = {}
    for provider in _provider_order():
        if not _key_for(provider):
            report[provider] = {"configured": False, "reachable": None, "error": None}
            continue
        reachable, error = probe_provider(provider)
        report[provider] = {"configured": True, "reachable": reachable, "error": error}
    return report


def reset_probe_cache() -> None:
    """Forget every cached probe result. For tests, and for nothing else."""
    _probe_cache.clear()


def _extract_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMUnavailable("No JSON object in model response")
    return json.loads(text[start : end + 1])


def _complete_openai(system: str, user: str, model: str, max_tokens: int) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key, timeout=settings.llm_request_timeout_seconds)
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

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key, timeout=settings.llm_request_timeout_seconds
    )
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
        timeout=settings.llm_request_timeout_seconds,
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

    Tries each configured provider in priority order (the ``settings.llm_provider`` primary
    first, then the remaining providers — see ``_provider_order``), so a transient failure or
    missing key on one degrades to the next rather than to the deterministic path.
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

        Tries each configured provider in order, retrying each (with a short backoff between
        attempts) before moving to the next. Raises ``LLMUnavailable`` when no provider
        succeeds so the agent degrades. Never logs ``system``/``user`` — they carry the
        patient snapshot — only provider name, attempt number, and exception type.

        A provider whose breaker is open is skipped without a socket, and the retry loop for a
        provider stops the moment its breaker trips rather than spending the remaining attempts
        on an upstream that has just proved it is not answering. Both are what turns a dead
        chain from 270 seconds of nothing into an immediate degrade — see ``app.agents.circuit``.
        """
        if not available_providers():
            # No provider configured: serve simulated data if the demo net is on, else degrade.
            if demo_fallback_enabled():
                return demo_data.simulated_response(system, user)
            raise LLMUnavailable("No LLM provider API key configured")

        providers = callable_providers()
        last_err: Exception | None = None
        for provider in providers:
            for attempt in range(retries + 1):
                # Re-checked per attempt, not just per provider: a concurrent caller (the
                # hypothesis panel runs four at once) may have tripped this breaker since the
                # list was taken, and the attempts left on this loop are the ones that would
                # each pay a full timeout to learn what is already known.
                if not breaker.allow(provider):
                    break
                try:
                    parsed = _extract_json(self._complete(provider, system, user))
                except Exception as exc:  # noqa: BLE001 — surfaced to caller as LLMUnavailable
                    last_err = exc
                    reason = describe_exception(exc)
                    logger.warning(
                        "LLM provider %r failed (attempt %d/%d): %s",
                        provider,
                        attempt + 1,
                        retries + 1,
                        reason,
                    )
                    if is_availability_failure(exc):
                        breaker.record_failure(provider, reason=reason)
                    else:
                        # The provider answered; the answer was unusable. That is a fact about
                        # this completion, not about the provider, so it must not accumulate
                        # towards opening the breaker — but a run of them must not leave a
                        # stale count behind either, or three bad completions spread across an
                        # hour would trip on the next real timeout.
                        breaker.record_success(provider)
                    if attempt < retries:
                        backoff = min(settings.llm_retry_backoff_base_seconds * (attempt + 1), 2.0)
                        time.sleep(backoff)
                else:
                    breaker.record_success(provider)
                    return parsed
        # Every provider worth calling failed or was skipped. Final safety net: simulated demo
        # data if enabled.
        logger.error(
            "No LLM provider produced a completion (attempted: %s; skipped by breaker: %s); "
            "last error: %s",
            ", ".join(providers) or "none",
            ", ".join(p for p in available_providers() if p not in providers) or "none",
            describe_exception(last_err),
        )
        if demo_fallback_enabled():
            return demo_data.simulated_response(system, user)
        if last_err is None:
            # Nothing was called at all: every configured provider is in cooldown. Distinct
            # from a provider error, and the message says so — an operator reading "TimeoutError"
            # in a log would go looking for a socket that this run never opened.
            raise LLMUnavailable("Every configured LLM provider is circuit-broken")
        # Not ``str(last_err)``: that laundered a provider's response body into an app-owned
        # exception, which every downstream handler then treats as safe to log and persist.
        raise LLMUnavailable(describe_exception(last_err))
