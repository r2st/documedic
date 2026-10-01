"""Provider SDK clients are built once and reused, not rebuilt per call.

Each SDK client owns an httpx connection pool. Building one per call meant no connection was
ever reused: every agent call opened a new socket and paid a full TLS handshake — ~37ms a call
against OpenRouter from the production host, spent out of the same wall-clock budget
(``reasoning_llm_budget_seconds``) that decides whether the engine's later lanes run at all.
The clients were also dropped without ``close()``, and neither ``httpx.Client`` nor
``httpcore.ConnectionPool`` defines ``__del__``, so each pool's keep-alive connection stayed
open until the collector reached it.

These tests pin the reuse itself rather than the latency: latency is the reason, but a timing
assertion against a live provider is not a test. What is pinned is that N calls construct one
client, that a changed key or timeout is not served a stale one, and that the shared client
survives the concurrent use the hypothesis panel puts it under.
"""

from __future__ import annotations

import threading

import pytest

from app.agents import llm
from app.config import settings


class _FakeCompletions:
    def __init__(self, owner: _FakeOpenAI) -> None:
        self._owner = owner

    def create(self, **kwargs):  # noqa: ANN003, ANN201 — a stand-in for the SDK's shape
        self._owner.calls += 1

        class _Msg:
            content = '{"ok": true}'

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        return _Completion()


class _FakeOpenAI:
    """Counts how many times the SDK client itself was constructed."""

    constructed = 0
    instances: list[_FakeOpenAI] = []

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        type(self).constructed += 1
        type(self).instances.append(self)
        self.kwargs = kwargs
        self.calls = 0
        self.closed = False
        self.chat = type("C", (), {"completions": _FakeCompletions(self)})()
        # The reachability probe's call — the cheapest authenticated request the SDK offers.
        self.models = type("M", (), {"list": lambda _self=None: []})()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_openai(monkeypatch):
    """Install a counting stand-in for ``openai.OpenAI`` and start from a clean cache."""
    _FakeOpenAI.constructed = 0
    _FakeOpenAI.instances = []
    module = type("M", (), {"OpenAI": _FakeOpenAI})
    monkeypatch.setitem(__import__("sys").modules, "openai", module)
    llm.close_provider_clients()
    yield _FakeOpenAI
    llm.close_provider_clients()


def test_repeated_calls_build_one_client(fake_openai, monkeypatch):
    """Ten completions through one provider construct one client, not ten.

    This is the whole defect: the count used to equal the number of calls, and every one of
    those clients was a connection pool with nothing in it.
    """
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 30)

    for _ in range(10):
        llm._complete_openai("system", "user", "gpt-4o", 128)

    assert fake_openai.constructed == 1, (
        f"{fake_openai.constructed} clients built for 10 calls — each one is a fresh connection "
        "pool, so no TLS session is ever reused"
    )
    assert fake_openai.instances[0].calls == 10


def test_openrouter_and_openai_do_not_share_a_client(fake_openai, monkeypatch):
    """Same SDK, different endpoint and headers — they must not be served each other's client."""
    monkeypatch.setattr(settings, "openai_api_key", "sk-openai")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-openrouter")
    monkeypatch.setattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 30)

    llm._complete_openai("s", "u", "gpt-4o", 16)
    llm._complete_openrouter("s", "u", "openai/gpt-oss-20b:free", 16)
    llm._complete_openai("s", "u", "gpt-4o", 16)
    llm._complete_openrouter("s", "u", "openai/gpt-oss-20b:free", 16)

    assert fake_openai.constructed == 2
    by_url = {c.kwargs.get("base_url") for c in fake_openai.instances}
    assert by_url == {None, "https://openrouter.ai/api/v1"}
    # The OpenRouter client is the one carrying the identifying headers.
    router = next(c for c in fake_openai.instances if c.kwargs.get("base_url"))
    assert router.kwargs["default_headers"]["X-Title"] == "Documedic (DoAide Med) — A DoAide Product"


def test_a_rotated_key_is_not_served_the_old_client(fake_openai, monkeypatch):
    """The cache key includes the API key, so a rotated credential builds a new client.

    A cache keyed only on the provider name would keep authenticating with the revoked key for
    the life of the process — the failure mode a cache like this one has to be written against.
    """
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 30)
    monkeypatch.setattr(settings, "openai_api_key", "sk-old")
    llm._complete_openai("s", "u", "gpt-4o", 16)
    monkeypatch.setattr(settings, "openai_api_key", "sk-new")
    llm._complete_openai("s", "u", "gpt-4o", 16)

    assert fake_openai.constructed == 2
    assert [c.kwargs["api_key"] for c in fake_openai.instances] == ["sk-old", "sk-new"]


def test_a_changed_timeout_is_not_served_the_old_client(fake_openai, monkeypatch):
    """The probe's short timeout and the request timeout must not collapse onto one client.

    ``llm_health_probe_timeout_seconds`` exists precisely so a health check is not as slow as a
    completion. Serving the probe a client built with the 30s completion timeout would silently
    undo that.
    """
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 30)
    monkeypatch.setattr(settings, "llm_health_probe_timeout_seconds", 3)

    llm._complete_openai("s", "u", "gpt-4o", 16)
    llm._probe_openai_compatible("sk-test", None)

    assert fake_openai.constructed == 2
    assert sorted(c.kwargs["timeout"] for c in fake_openai.instances) == [3, 30]


def test_the_shared_client_survives_concurrent_calls(fake_openai, monkeypatch):
    """One client, reached from many threads at once, is built exactly once.

    Not a hypothetical arrangement: the hypothesis panel runs four specialists concurrently on
    ``llm_executor``'s threads, so the first four calls of a run race on an empty cache. A
    check-then-build without the lock would let each of them build its own — the very
    duplication the cache exists to stop, reappearing under exactly the load that matters.
    """
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 30)

    start = threading.Barrier(8)
    errors: list[BaseException] = []

    def call() -> None:
        try:
            start.wait(timeout=10)
            llm._complete_openai("s", "u", "gpt-4o", 16)
        except BaseException as exc:  # noqa: BLE001 — surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"concurrent callers raised: {errors!r}"
    assert fake_openai.constructed == 1
    assert sum(c.calls for c in fake_openai.instances) == 8


def test_close_provider_clients_closes_and_forgets_them(fake_openai, monkeypatch):
    """Shutdown releases the pooled connections, and the next call starts over."""
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 30)

    llm._complete_openai("s", "u", "gpt-4o", 16)
    first = fake_openai.instances[0]
    assert not first.closed

    llm.close_provider_clients()
    assert first.closed, "the pooled connections were never released"

    llm._complete_openai("s", "u", "gpt-4o", 16)
    assert fake_openai.constructed == 2, "the cache kept serving a client it had closed"


def test_close_is_best_effort_across_clients(fake_openai, monkeypatch):
    """One client that objects to being closed does not strand the others.

    This runs during shutdown, where a half-completed cleanup is the worst outcome available.
    """
    monkeypatch.setattr(settings, "llm_request_timeout_seconds", 30)
    monkeypatch.setattr(settings, "openai_api_key", "sk-one")
    llm._complete_openai("s", "u", "gpt-4o", 16)
    monkeypatch.setattr(settings, "openai_api_key", "sk-two")
    llm._complete_openai("s", "u", "gpt-4o", 16)

    def boom() -> None:
        raise RuntimeError("transport already torn down")

    fake_openai.instances[0].close = boom

    llm.close_provider_clients()  # must not raise

    assert fake_openai.instances[1].closed
    llm._complete_openai("s", "u", "gpt-4o", 16)
    assert fake_openai.constructed == 3, "a failed close left the cache populated"


def test_the_client_cache_is_bounded(fake_openai, monkeypatch):
    """A caller that varies the timeout cannot grow the cache without limit."""
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")

    for timeout in range(llm._MAX_CACHED_CLIENTS + 6):
        monkeypatch.setattr(settings, "llm_request_timeout_seconds", timeout + 1)
        llm._complete_openai("s", "u", "gpt-4o", 16)

    assert len(llm._client_cache) <= llm._MAX_CACHED_CLIENTS
