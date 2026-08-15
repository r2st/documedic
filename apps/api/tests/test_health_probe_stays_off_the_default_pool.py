"""An LLM outage must degrade reasoning and nothing else — including from the status page.

``app.agents.util.llm_executor`` exists for one reason, written out at length in its docstring
and again in ``config.llm_max_concurrent_calls``: the provider SDKs are synchronous, so every
call occupies a thread, and ``asyncio.to_thread`` puts it on the event loop's *default*
executor — the same pool bcrypt runs on behind ``verify_password``. A provider that accepts a
connection and then stops answering holds those threads for as long as it hangs, and when the
pool filled, logging in stopped working. Reasoning has a deterministic path to degrade onto;
authentication does not.

``/health/dependencies`` was calling every configured provider through ``asyncio.to_thread``,
under a comment claiming it used "the same offload idiom as ``agents.util.call_llm``" while
doing the opposite of what that function does. It is also the request most likely to be in
flight during an outage: a monitoring poller and whichever operators are refreshing the status
page, arriving together, with no single-flight around the probe cache to collapse them, each
holding a thread for up to ``llm_health_probe_timeout_seconds`` per provider.
"""

from __future__ import annotations

import threading

import pytest

from app.agents.util import llm_executor

DEPENDENCIES = "/health/dependencies"


@pytest.mark.asyncio
async def test_the_provider_probe_runs_on_the_llm_pool(auth_client, monkeypatch):
    """Asserted by the thread's name, which is the only thing that tells the pools apart.

    ``llm_executor`` names its threads ``llm_*``; the default executor's are ``asyncio_*``. A
    regression to ``asyncio.to_thread`` is invisible in behaviour and in every functional test
    — the endpoint answers correctly either way, and the difference only shows up as an
    unrelated outage — so the thread it ran on is the property worth pinning.
    """
    from app.routers import health

    observed: dict[str, str] = {}

    def probe() -> dict:
        observed["thread"] = threading.current_thread().name
        return {}

    monkeypatch.setattr(health, "probe_all_providers", probe)

    resp = await auth_client.get(DEPENDENCIES)

    assert resp.status_code == 200, resp.text
    assert observed["thread"].startswith("llm"), (
        f"the provider probe ran on {observed['thread']!r} — a hanging provider on that pool "
        "holds threads bcrypt needs, which is how an LLM outage took logins down"
    )


def test_the_probe_pool_is_bounded():
    """Containment is the point, not merely being off the default pool.

    An unbounded pool would keep authentication clear and still let the status page open a
    thread per concurrent poller against a provider that never answers.
    """
    from app.config import settings

    executor = llm_executor()

    assert 1 <= executor._max_workers <= max(1, settings.llm_max_concurrent_calls)


@pytest.mark.asyncio
async def test_a_hanging_provider_does_not_stop_the_endpoint_answering(auth_client, monkeypatch):
    """The degradation contract for the status page itself: it reports, it does not gate.

    A probe that raises is a provider that is not reachable, which is exactly what this
    endpoint exists to say — so the failure belongs in the body, not in the status code.
    """
    from app.agents import llm

    monkeypatch.setattr(llm, "_probe_cache", {})
    monkeypatch.setattr(llm.settings, "openai_api_key", "sk-not-a-real-key")

    def refuse(_provider: str) -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(llm, "_probe", refuse)

    resp = await auth_client.get(DEPENDENCIES)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["llm_reachable"] is False
    assert body["llm_providers"]["openai"]["reachable"] is False
    # The reason is reported, but through `describe_exception` — a provider's own error text can
    # quote the prompt on other paths, and this one is returned over HTTP.
    assert "connection refused" not in str(body["llm_providers"]["openai"]["error"])


@pytest.mark.asyncio
async def test_reasoning_is_still_reported_as_configured_when_it_is_unreachable(
    auth_client, monkeypatch
):
    """``llm_configured`` and ``llm_reachable`` answer different questions, and must keep doing.

    A revoked or out-of-quota key still reads as configured. Collapsing the two would restore
    the state this endpoint's probe was added to expose: a deployment publishing that reasoning
    works while every run in front of a clinician degrades.
    """
    from app.agents import llm

    monkeypatch.setattr(llm, "_probe_cache", {})
    monkeypatch.setattr(llm.settings, "openai_api_key", "sk-not-a-real-key")
    monkeypatch.setattr(llm, "_probe", lambda _provider: (_ for _ in ()).throw(RuntimeError("no")))

    body = (await auth_client.get(DEPENDENCIES)).json()

    assert body["llm_available_providers"], "a key is configured, so this must still say so"
    assert body["llm_reachable"] is False, "but nothing answered, and that is the useful field"
