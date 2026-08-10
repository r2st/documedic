"""LLM error-handling robustness: timeouts, retry backoff, and failure logging.

These close a gap where provider failures were silently swallowed (no timeout on the SDK
clients, no logging, instant retries with no backoff) -- see app.agents.llm.
"""

from __future__ import annotations

import logging

import pytest

from app.agents import llm
from app.agents.llm import LLMClient, LLMUnavailable
from app.agents.prompts import TRIAGE_INTAKE
from app.config import settings


def test_openai_client_gets_configured_timeout(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 12.5)

    recorded: dict = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            recorded["kwargs"] = kwargs
            raise RuntimeError("stop before network call")

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

    with pytest.raises(RuntimeError):
        llm._complete_openai("sys", "usr", "gpt-4o", 100)
    assert recorded["kwargs"]["timeout"] == 12.5


def test_anthropic_client_gets_configured_timeout(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "an-key")
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 7.0)

    recorded: dict = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            recorded["kwargs"] = kwargs
            raise RuntimeError("stop before network call")

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)

    with pytest.raises(RuntimeError):
        llm._complete_anthropic("sys", "usr", "claude", 100)
    assert recorded["kwargs"]["timeout"] == 7.0


def test_openrouter_client_gets_configured_timeout(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 9.0)

    recorded: dict = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            recorded["kwargs"] = kwargs
            raise RuntimeError("stop before network call")

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)

    with pytest.raises(RuntimeError):
        llm._complete_openrouter("sys", "usr", "some/model", 100)
    assert recorded["kwargs"]["timeout"] == 9.0


def test_retry_backs_off_between_attempts_of_same_provider(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "llm_fallback_enabled", False)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", False)
    monkeypatch.setattr(settings, "llm_demo_fallback", False)
    monkeypatch.setattr(settings, "llm_retry_backoff_base_seconds", 0.01)

    calls = {"n": 0}

    def flaky(system, user, model, max_tokens):
        calls["n"] += 1
        raise RuntimeError("transient")

    sleeps: list[float] = []
    monkeypatch.setattr(llm, "_complete_openai", flaky)
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(LLMUnavailable):
        LLMClient().complete_json(TRIAGE_INTAKE, "fever", retries=2)

    assert calls["n"] == 3  # initial attempt + 2 retries
    # Backoff sleeps between attempts only (2 gaps for 3 attempts), never after the last.
    assert sleeps == [0.01, 0.02]


def test_no_backoff_sleep_when_retries_is_zero(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "llm_fallback_enabled", False)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", False)
    monkeypatch.setattr(settings, "llm_demo_fallback", False)

    monkeypatch.setattr(
        llm,
        "_complete_openai",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
    )
    slept = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(LLMUnavailable):
        LLMClient().complete_json(TRIAGE_INTAKE, "fever", retries=0)
    assert slept == []


def test_backoff_is_capped(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "llm_fallback_enabled", False)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", False)
    monkeypatch.setattr(settings, "llm_demo_fallback", False)
    monkeypatch.setattr(settings, "llm_retry_backoff_base_seconds", 10.0)

    monkeypatch.setattr(
        llm,
        "_complete_openai",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(LLMUnavailable):
        LLMClient().complete_json(TRIAGE_INTAKE, "fever", retries=1)
    assert all(s <= 2.0 for s in sleeps)


def test_provider_failure_is_logged_without_leaking_prompt(monkeypatch, caplog):
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "llm_fallback_enabled", False)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", False)
    monkeypatch.setattr(settings, "llm_demo_fallback", False)

    secret_patient_detail = "PATIENT_SNAPSHOT_MARKER_xyz"

    def boom(system, user, model, max_tokens):
        raise RuntimeError("upstream 500")

    monkeypatch.setattr(llm, "_complete_openai", boom)

    with caplog.at_level(logging.WARNING, logger="app.agents.llm"):
        with pytest.raises(LLMUnavailable):
            LLMClient().complete_json(TRIAGE_INTAKE, secret_patient_detail, retries=0)

    assert any("openai" in rec.message and "RuntimeError" in rec.message for rec in caplog.records)
    assert all(secret_patient_detail not in rec.message for rec in caplog.records)


def test_all_providers_failed_logs_error(monkeypatch, caplog):
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "llm_fallback_enabled", False)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", False)
    monkeypatch.setattr(settings, "llm_demo_fallback", True)

    monkeypatch.setattr(
        llm,
        "_complete_openai",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
    )

    with caplog.at_level(logging.ERROR, logger="app.agents.llm"):
        result = LLMClient().complete_json(TRIAGE_INTAKE, "fever", retries=0)

    assert result["_demo"] is True
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)
