"""A configured LLM key is not a working LLM provider, and the health probes said it was.

Everything the health endpoints reported about reasoning was derived from ``_key_for`` —
whether an environment variable holds a string. A key that has been revoked, expired, run out
of quota, or is simply unreachable from the deployment's network passes that test. So the
status page published ``llm_mode: live`` and ``llm_available_providers: ["openrouter"]`` while
every eight-agent panel degraded in front of a clinician, and because production refuses the
simulated demo net outright there was no fallback to soften it. The operator's first signal
was somebody saying the engine had stopped working.

``GET /health/dependencies`` now calls each configured provider. These tests never make a
network call: the probe is monkeypatched, and with no key configured (the suite's default) no
provider is called at all.
"""

from __future__ import annotations

import pytest

from app.agents import llm
from app.config import settings


@pytest.fixture(autouse=True)
def clean_probe_cache():
    llm.reset_probe_cache()
    yield
    llm.reset_probe_cache()


@pytest.fixture
def openrouter_only(monkeypatch):
    """One configured provider, so a test asserts about a single probe."""
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "llm_fallback_enabled", False)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", True)
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-test")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")


class _Revoked(Exception):
    """Stands in for the SDK's 401: an upstream status, and a body we must not echo."""

    def __init__(self) -> None:
        super().__init__("Error code: 401 - {'message': 'User not found: sk-or-v1-9f3c'}")
        self.status_code = 401


# --- The probe itself ---------------------------------------------------------------------


def test_a_reachable_provider_reports_reachable(openrouter_only, monkeypatch):
    monkeypatch.setattr(llm, "_probe", lambda provider: None)
    assert llm.probe_provider("openrouter") == (True, None)


def test_a_revoked_key_reports_unreachable_with_its_status(openrouter_only, monkeypatch):
    def _fail(provider: str) -> None:
        raise _Revoked()

    monkeypatch.setattr(llm, "_probe", _fail)
    reachable, error = llm.probe_provider("openrouter")
    assert reachable is False
    assert error == "_Revoked (HTTP 401)"


def test_the_error_never_carries_the_providers_own_message(openrouter_only, monkeypatch):
    """The reported reason goes over HTTP, so it goes through ``describe_exception`` like every
    other place a provider error is rendered — no per-endpoint exception to that rule."""

    def _fail(provider: str) -> None:
        raise _Revoked()

    monkeypatch.setattr(llm, "_probe", _fail)
    _, error = llm.probe_provider("openrouter")
    assert "sk-or-v1-9f3c" not in (error or "")
    assert "User not found" not in (error or "")


def test_results_are_cached_so_a_status_poller_is_not_provider_traffic(
    openrouter_only, monkeypatch
):
    calls: list[str] = []
    monkeypatch.setattr(llm, "_probe", lambda provider: calls.append(provider))

    for _ in range(5):
        llm.probe_provider("openrouter")
    assert calls == ["openrouter"]


def test_a_failure_is_cached_too(openrouter_only, monkeypatch):
    """The unreachable provider is the slow one: re-probing per poll would make the health
    endpoint hang for the length of the outage."""
    calls: list[str] = []

    def _fail(provider: str) -> None:
        calls.append(provider)
        raise _Revoked()

    monkeypatch.setattr(llm, "_probe", _fail)
    for _ in range(5):
        assert llm.probe_provider("openrouter")[0] is False
    assert calls == ["openrouter"]


def test_the_cache_expires(openrouter_only, monkeypatch):
    monkeypatch.setattr(settings, "llm_health_probe_ttl_seconds", 0.0)
    calls: list[str] = []
    monkeypatch.setattr(llm, "_probe", lambda provider: calls.append(provider))

    llm.probe_provider("openrouter")
    llm.probe_provider("openrouter")
    assert len(calls) == 2


def test_an_unconfigured_provider_is_never_called(monkeypatch):
    """No key means nothing to authenticate with, so a deployment running one provider makes
    one network call rather than three."""
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")

    def _explode(provider: str) -> None:
        raise AssertionError(f"probed unconfigured provider {provider!r}")

    monkeypatch.setattr(llm, "_probe", _explode)
    report = llm.probe_all_providers()
    assert report
    assert all(
        state == {"configured": False, "reachable": None, "error": None}
        for state in report.values()
    )


# --- Through GET /health/dependencies ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_probe_contradicts_a_configured_but_dead_key(
    auth_client, openrouter_only, monkeypatch
):
    """The regression. ``llm_mode`` still says ``live`` — it is a statement about
    configuration and stays one — but ``llm_reachable`` says what an operator needs."""

    def _fail(provider: str) -> None:
        raise _Revoked()

    monkeypatch.setattr(llm, "_probe", _fail)
    resp = await auth_client.get("/health/dependencies")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["llm_mode"] == "live"
    assert body["llm_available_providers"] == ["openrouter"]
    assert body["llm_reachable"] is False
    assert body["llm_providers"]["openrouter"] == {
        "configured": True,
        "reachable": False,
        "error": "_Revoked (HTTP 401)",
    }


@pytest.mark.asyncio
async def test_a_working_provider_reports_reachable(auth_client, openrouter_only, monkeypatch):
    monkeypatch.setattr(llm, "_probe", lambda provider: None)
    body = (await auth_client.get("/health/dependencies")).json()
    assert body["llm_reachable"] is True
    assert body["llm_providers"]["openrouter"]["reachable"] is True


@pytest.mark.asyncio
async def test_one_dead_provider_still_leaves_the_deployment_reachable(auth_client, monkeypatch):
    """The fallback chain is the point: reasoning works as long as *someone* answers."""
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", "sk-dead")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-live")
    monkeypatch.setattr(settings, "openrouter_api_key", "")

    def _probe(provider: str) -> None:
        if provider == "openai":
            raise _Revoked()

    monkeypatch.setattr(llm, "_probe", _probe)
    body = (await auth_client.get("/health/dependencies")).json()
    assert body["llm_providers"]["openai"]["reachable"] is False
    assert body["llm_providers"]["anthropic"]["reachable"] is True
    assert body["llm_reachable"] is True


@pytest.mark.asyncio
async def test_no_key_configured_probes_nothing_and_reports_unreachable(auth_client):
    """The suite's default: every provider unconfigured, so the endpoint answers without
    touching the network."""
    body = (await auth_client.get("/health/dependencies")).json()
    assert body["llm_reachable"] is False
    assert all(state["configured"] is False for state in body["llm_providers"].values())
    assert all(state["reachable"] is None for state in body["llm_providers"].values())


@pytest.mark.asyncio
async def test_the_probe_stays_behind_authentication(client, openrouter_only, monkeypatch):
    """It names the deployment's vendor inventory and now its outage state too, which is more
    than the anonymous probes should publish."""

    def _explode(provider: str) -> None:
        raise AssertionError("an unauthenticated caller must not reach the provider")

    monkeypatch.setattr(llm, "_probe", _explode)
    assert (await client.get("/health/dependencies")).status_code == 401


@pytest.mark.asyncio
async def test_pool_occupancy_is_reported_for_diagnosing_exhaustion(auth_client):
    """Long-running reasoning routes hold a connection for the length of a panel, so pool
    occupancy is the number that moves first when a deployment runs out of them."""
    body = (await auth_client.get("/health/dependencies")).json()
    assert isinstance(body["database_pool"], dict)
