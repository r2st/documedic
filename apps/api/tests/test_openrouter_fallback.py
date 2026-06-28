"""OpenRouter fallback tier — the chain is OpenAI -> Anthropic -> OpenRouter -> demo.

These tests verify provider ordering, key resolution, that the OpenRouter SDK call is
wired through the OpenAI-compatible client with a custom base_url, that it engages only
after the openai/anthropic tiers fail, and that health reporting surfaces its status.
"""

from __future__ import annotations

import pytest

from app.agents import llm
from app.agents.llm import (
    LLMClient,
    LLMUnavailable,
    _key_for,
    _provider_order,
    available_providers,
)
from app.agents.prompts import TRIAGE_INTAKE
from app.config import settings

# --------------------------------------------------------------------- provider ordering


def test_openrouter_is_last_in_provider_order(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", True)
    assert _provider_order() == ["openai", "anthropic", "openrouter"]


def test_openrouter_omitted_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", False)
    assert "openrouter" not in _provider_order()


def test_key_for_openrouter(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key-123")
    assert _key_for("openrouter") == "or-key-123"
    assert _key_for("unknown-provider") == ""


def test_available_providers_includes_openrouter_when_keyed(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "llm_openrouter_fallback", True)
    assert available_providers() == ["openrouter"]


def test_llm_configured_true_with_only_openrouter(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "llm_openrouter_fallback", True)
    assert settings.llm_configured is True


# --------------------------------------------------------------------- routing / fallthrough


def test_openrouter_used_when_it_is_the_only_keyed_provider(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "llm_openrouter_fallback", True)

    captured: dict = {}

    def fake_openrouter(system, user, model, max_tokens):
        captured["model"] = model
        return '{"questions": [], "_via": "openrouter"}'

    monkeypatch.setattr(llm, "_complete_openrouter", fake_openrouter)

    result = LLMClient().complete_json(TRIAGE_INTAKE, "fever")
    assert result["_via"] == "openrouter"
    assert captured["model"] == settings.openrouter_model


def test_openrouter_engages_only_after_anthropic_fails(monkeypatch):
    # All three keyed. OpenAI + Anthropic raise; OpenRouter succeeds.
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "an-key")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", True)

    order: list[str] = []

    def boom(name):
        def _fn(system, user, model, max_tokens):
            order.append(name)
            raise RuntimeError(f"{name} down")

        return _fn

    def ok_openrouter(system, user, model, max_tokens):
        order.append("openrouter")
        return '{"questions": []}'

    monkeypatch.setattr(llm, "_complete_openai", boom("openai"))
    monkeypatch.setattr(llm, "_complete_anthropic", boom("anthropic"))
    monkeypatch.setattr(llm, "_complete_openrouter", ok_openrouter)

    result = LLMClient().complete_json(TRIAGE_INTAKE, "fever", retries=0)
    assert result == {"questions": []}
    # OpenRouter was tried last, after both higher tiers were attempted.
    assert order[0] == "openai"
    assert order[-1] == "openrouter"
    assert "anthropic" in order


def test_demo_fallback_after_openrouter_also_fails(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "llm_openrouter_fallback", True)
    monkeypatch.setattr(settings, "llm_demo_fallback", True)

    def boom(system, user, model, max_tokens):
        raise RuntimeError("openrouter down")

    monkeypatch.setattr(llm, "_complete_openrouter", boom)

    result = LLMClient().complete_json(TRIAGE_INTAKE, "fever", retries=0)
    # Falls through to the simulated demo net rather than raising.
    assert result["_demo"] is True


def test_raises_when_openrouter_fails_and_demo_disabled(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "llm_openrouter_fallback", True)
    monkeypatch.setattr(settings, "llm_demo_fallback", False)

    def boom(system, user, model, max_tokens):
        raise RuntimeError("openrouter down")

    monkeypatch.setattr(llm, "_complete_openrouter", boom)

    with pytest.raises(LLMUnavailable):
        LLMClient().complete_json(TRIAGE_INTAKE, "fever", retries=0)


def test_complete_openrouter_uses_openai_sdk_with_custom_base_url(monkeypatch):
    """The OpenRouter call must hit the OpenAI-compatible client with the configured base_url."""
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")

    recorded: dict = {}

    class FakeCompletions:
        def create(self, **kwargs):
            recorded["create_kwargs"] = kwargs

            class _Msg:
                content = '{"ok": true}'

            class _Choice:
                message = _Msg()

            class _Resp:
                choices = [_Choice()]

            return _Resp()

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            recorded["init_kwargs"] = kwargs
            self.chat = FakeChat()

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

    out = llm._complete_openrouter("sys", "usr", "some/model:free", 1234)
    assert out == '{"ok": true}'
    assert recorded["init_kwargs"]["api_key"] == "or-key"
    assert recorded["init_kwargs"]["base_url"] == "https://openrouter.ai/api/v1"
    assert recorded["create_kwargs"]["model"] == "some/model:free"
    assert recorded["create_kwargs"]["max_tokens"] == 1234
    assert recorded["create_kwargs"]["temperature"] == 0.0


# --------------------------------------------------------------------- health reporting


@pytest.mark.asyncio
async def test_health_dependencies_report_openrouter(client, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "llm_openrouter_fallback", True)

    body = (await client.get("/health/dependencies")).json()
    assert body["llm_openrouter_fallback"] is True
    assert body["llm_openrouter_configured"] is True
    assert "openrouter" in body["llm_available_providers"]
