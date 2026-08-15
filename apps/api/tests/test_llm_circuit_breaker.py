"""The provider circuit breaker: what trips it, what it skips, and how it recovers.

The defect this closes is one a timeout cannot: ``llm_request_timeout_seconds`` bounds a single
socket, and then ``complete_json`` multiplies it by ``retries + 1`` and by the length of the
fallback chain — 270 seconds on the defaults for a chain of three that has stopped answering.
Nothing remembered that, so every agent call of every run paid it again. The tests here are
about that arithmetic and about the two ways of getting the fix wrong: opening the breaker on a
provider that is actually up (a bad completion is not an outage), and never closing it again.

Timing is injected rather than slept through — ``allow``/``record_failure``/``is_open`` all take
a monotonic ``now`` — so the cooldown behaviour is asserted at its real value instead of at a
value shrunk to make a test fast.
"""

from __future__ import annotations

import threading

import pytest

from app.agents import llm
from app.agents.circuit import ProviderCircuit, breaker, is_availability_failure
from app.agents.llm import LLMClient, LLMUnavailable, callable_providers
from app.config import settings
from app.services.extraction import claude_client


@pytest.fixture
def circuit():
    """A breaker isolated from the process-wide one, so a test cannot leak state into a peer."""
    return ProviderCircuit()


@pytest.fixture(autouse=True)
def _breaker_settings(monkeypatch):
    monkeypatch.setattr(settings, "llm_circuit_breaker_enabled", True)
    monkeypatch.setattr(settings, "llm_circuit_failure_threshold", 3)
    monkeypatch.setattr(settings, "llm_circuit_reset_seconds", 60.0)
    # No real backoff sleeps: several tests below exhaust a full retry sequence.
    monkeypatch.setattr(settings, "llm_retry_backoff_base_seconds", 0.0)


# --------------------------------------------------------------- classifying the failure


class _Timeout(Exception):
    """Stands in for the SDKs' APITimeoutError — matched on the type name, not the class."""


class _Status(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status


class _Response:
    def __init__(self, status: int) -> None:
        self.status_code = status


class _NestedStatus(Exception):
    """A provider error carrying its status on ``.response``, which is how httpx-based SDKs do."""

    def __init__(self, status: int) -> None:
        super().__init__("upstream said no")
        self.response = _Response(status)


@pytest.mark.parametrize(
    "exc",
    [
        _Timeout(),
        ConnectionError("refused"),
        _Status(500),
        _Status(503),
        _Status(429),
        _Status(408),
        _Status(401),
        _Status(403),
        _NestedStatus(502),
    ],
    ids=[
        "timeout",
        "connection-refused",
        "500",
        "503",
        "429",
        "408",
        "401-revoked-key",
        "403",
        "502-on-response",
    ],
)
def test_availability_failures_are_recognised(exc):
    assert is_availability_failure(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        _Status(400),
        _Status(404),
        _Status(422),
        LLMUnavailable("No JSON object in model response"),
        ValueError("Expecting value: line 1 column 1"),
        RuntimeError("OpenAI vision path does not support file type: pdf"),
    ],
    ids=["400", "404", "422", "no-json", "json-decode", "unsupported-file-type"],
)
def test_request_shaped_failures_are_not_availability_failures(exc):
    """A provider that answered is up, whatever it answered.

    This is the direction that costs patients rather than latency: opening the breaker because
    one prompt produced unparseable JSON takes a healthy provider out of service for every
    *other* chart for the length of the cooldown.
    """
    assert is_availability_failure(exc) is False


def test_none_is_not_a_failure():
    assert is_availability_failure(None) is False


# --------------------------------------------------------------- state machine


def test_breaker_stays_closed_below_the_threshold(circuit):
    for _ in range(settings.llm_circuit_failure_threshold - 1):
        circuit.record_failure("openai", reason="Timeout", now=0.0)
    assert circuit.allow("openai", now=0.0) is True
    assert circuit.is_open("openai", now=0.0) is False


def test_breaker_opens_at_the_threshold(circuit):
    for _ in range(settings.llm_circuit_failure_threshold):
        circuit.record_failure("openai", reason="Timeout", now=0.0)
    assert circuit.is_open("openai", now=0.0) is True
    assert circuit.allow("openai", now=0.0) is False


def test_a_success_resets_the_consecutive_count(circuit):
    """Consecutive, not cumulative — two failures an hour apart are not an outage."""
    circuit.record_failure("openai", reason="Timeout", now=0.0)
    circuit.record_failure("openai", reason="Timeout", now=1.0)
    circuit.record_success("openai")
    circuit.record_failure("openai", reason="Timeout", now=2.0)
    assert circuit.is_open("openai", now=2.0) is False


def test_one_provider_opening_does_not_affect_another(circuit):
    for _ in range(settings.llm_circuit_failure_threshold):
        circuit.record_failure("openai", reason="Timeout", now=0.0)
    assert circuit.is_open("openai", now=0.0) is True
    assert circuit.is_open("anthropic", now=0.0) is False
    assert circuit.allow("anthropic", now=0.0) is True


def test_breaker_admits_exactly_one_trial_after_the_cooldown(circuit):
    """Half-open is one caller, not all of them.

    The concurrency this runs under is a four-specialist hypothesis panel under
    ``asyncio.gather``. If a cooldown elapsing let all four through, each would pay the full
    timeout to learn the same thing — which is the stall the breaker exists to remove, arriving
    once per cooldown window instead of once per call.
    """
    for _ in range(settings.llm_circuit_failure_threshold):
        circuit.record_failure("openai", reason="Timeout", now=0.0)

    after = settings.llm_circuit_reset_seconds + 1
    assert circuit.allow("openai", now=after) is True
    assert circuit.allow("openai", now=after) is False
    assert circuit.allow("openai", now=after) is False


def test_a_successful_trial_closes_the_breaker(circuit):
    for _ in range(settings.llm_circuit_failure_threshold):
        circuit.record_failure("openai", reason="Timeout", now=0.0)
    after = settings.llm_circuit_reset_seconds + 1
    assert circuit.allow("openai", now=after) is True

    circuit.record_success("openai")

    assert circuit.is_open("openai", now=after) is False
    assert circuit.allow("openai", now=after) is True
    assert circuit.allow("openai", now=after) is True


def test_a_failed_trial_reopens_immediately_without_waiting_for_the_threshold(circuit):
    """One failed trial re-opens: the cooldown just elapsed and the provider is still down.

    Requiring another full threshold's worth of failures would admit a trial on every call for
    as long as the outage lasted, which is the un-broken behaviour with extra steps.
    """
    for _ in range(settings.llm_circuit_failure_threshold):
        circuit.record_failure("openai", reason="Timeout", now=0.0)
    after = settings.llm_circuit_reset_seconds + 1
    assert circuit.allow("openai", now=after) is True

    circuit.record_failure("openai", reason="Timeout", now=after)

    assert circuit.is_open("openai", now=after) is True
    assert circuit.allow("openai", now=after) is False
    # ...and the clock restarted from the trial, not from the original trip.
    assert circuit.allow("openai", now=after + settings.llm_circuit_reset_seconds - 1) is False
    assert circuit.allow("openai", now=after + settings.llm_circuit_reset_seconds + 1) is True


def test_only_one_thread_wins_the_half_open_trial(circuit):
    """The one-trial rule has to hold under the concurrency it was written for."""
    for _ in range(settings.llm_circuit_failure_threshold):
        circuit.record_failure("openai", reason="Timeout", now=0.0)
    after = settings.llm_circuit_reset_seconds + 1

    admitted: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def _try() -> None:
        start.wait()
        allowed = circuit.allow("openai", now=after)
        with lock:
            admitted.append(allowed)

    threads = [threading.Thread(target=_try) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(admitted) == 1, f"{sum(admitted)} threads were admitted to the half-open trial"


def test_disabling_the_breaker_restores_the_unbroken_behaviour(circuit, monkeypatch):
    monkeypatch.setattr(settings, "llm_circuit_breaker_enabled", False)
    for _ in range(10):
        circuit.record_failure("openai", reason="Timeout", now=0.0)
    assert circuit.allow("openai", now=0.0) is True
    assert circuit.is_open("openai", now=0.0) is False


def test_snapshot_reports_only_providers_that_have_failed(circuit):
    circuit.record_success("anthropic")
    for _ in range(settings.llm_circuit_failure_threshold):
        circuit.record_failure("openai", reason="TimeoutError", now=0.0)

    report = circuit.snapshot(now=10.0)

    assert "anthropic" not in report, "a provider that never failed needs no operator row"
    assert report["openai"]["state"] == "open"
    assert report["openai"]["consecutive_failures"] == settings.llm_circuit_failure_threshold
    assert report["openai"]["last_error"] == "TimeoutError"
    assert report["openai"]["seconds_until_retry"] == pytest.approx(
        settings.llm_circuit_reset_seconds - 10.0
    )


def test_reset_forgets_everything(circuit):
    for _ in range(settings.llm_circuit_failure_threshold):
        circuit.record_failure("openai", reason="Timeout", now=0.0)
    circuit.reset()
    assert circuit.snapshot(now=0.0) == {}
    assert circuit.allow("openai", now=0.0) is True


# --------------------------------------------------------------- the reasoning client


@pytest.fixture
def three_providers(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", True)
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "an-key")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    monkeypatch.setattr(settings, "llm_demo_fallback", False)


def _record_calls(monkeypatch, behaviour):
    """Replace ``LLMClient._complete`` with ``behaviour(provider)``, recording every call."""
    calls: list[str] = []

    def _complete(self, provider, system, user):
        calls.append(provider)
        return behaviour(provider)

    monkeypatch.setattr(LLMClient, "_complete", _complete)
    return calls


def test_a_dead_chain_is_called_once_per_provider_then_skipped_entirely(
    three_providers, monkeypatch
):
    """The headline number: the second agent call of a run against a dead chain opens no socket.

    Nine attempts (three providers x three tries) is 270 seconds on the default timeout, and
    before the breaker every subsequent agent call paid it again — seven sequential nodes plus a
    panel, which is how an upstream outage became half an hour of an empty Reasoning Theatre.
    """

    def _always_timeout(provider):
        raise _Timeout()

    calls = _record_calls(monkeypatch, _always_timeout)
    client = LLMClient()

    with pytest.raises(LLMUnavailable):
        client.complete_json("sys", "usr")
    first_pass = list(calls)
    calls.clear()

    with pytest.raises(LLMUnavailable) as second:
        client.complete_json("sys", "usr")

    # One agent call still pays the full chain, and that is the design: the default threshold is
    # exactly one exhausted attempt sequence, so a provider proves itself down over one call
    # rather than over a fraction of one.
    assert first_pass == ["openai"] * 3 + ["anthropic"] * 3 + ["openrouter"] * 3, first_pass
    # Every agent call after it costs nothing. This is the whole fix — seven sequential nodes
    # plus a four-way panel used to repeat the 270 seconds above, each of them.
    assert calls == [], f"the second call still reached providers: {calls}"
    assert "circuit-broken" in str(second.value)


def test_a_provider_is_abandoned_mid_retry_once_its_breaker_trips(three_providers, monkeypatch):
    """With a threshold of one, the retry loop must not spend its remaining attempts."""
    monkeypatch.setattr(settings, "llm_circuit_failure_threshold", 1)

    def _always_timeout(provider):
        raise _Timeout()

    calls = _record_calls(monkeypatch, _always_timeout)

    with pytest.raises(LLMUnavailable):
        LLMClient().complete_json("sys", "usr")

    assert calls == ["openai", "anthropic", "openrouter"], calls


def test_an_open_breaker_falls_the_chain_through_to_a_healthy_provider(
    three_providers, monkeypatch
):
    """Skipping is not giving up: the next provider in the chain still gets the call."""
    for _ in range(settings.llm_circuit_failure_threshold):
        breaker.record_failure("openai", reason="TimeoutError")

    def _anthropic_works(provider):
        if provider == "anthropic":
            return '{"ok": true}'
        raise _Timeout()

    calls = _record_calls(monkeypatch, _anthropic_works)

    assert LLMClient().complete_json("sys", "usr") == {"ok": True}
    assert "openai" not in calls, f"a circuit-broken provider was still called: {calls}"
    assert calls == ["anthropic"]


def test_a_malformed_response_does_not_open_the_breaker(three_providers, monkeypatch):
    """Six unusable completions in a row, and the provider is still called on the seventh.

    The provider answered every time. Tripping on this would take a working upstream out of
    service for every other patient because one prompt confused the model.
    """

    def _no_json(provider):
        return "I'm sorry, I can't help with that."

    calls = _record_calls(monkeypatch, _no_json)

    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            LLMClient().complete_json("sys", "usr")

    assert breaker.is_open("openai") is False
    assert calls.count("openai") == 6, calls


def test_a_bad_request_status_does_not_open_the_breaker(three_providers, monkeypatch):
    def _four_hundred(provider):
        raise _Status(400)

    _record_calls(monkeypatch, _four_hundred)
    with pytest.raises(LLMUnavailable):
        LLMClient().complete_json("sys", "usr")

    assert breaker.is_open("openai") is False
    assert breaker.is_open("anthropic") is False


def test_a_success_after_failures_keeps_the_provider_in_the_chain(three_providers, monkeypatch):
    """Two timeouts then a success must not leave the count one failure from tripping."""
    outcomes = iter([_Timeout(), _Timeout(), None, _Timeout(), _Timeout()])

    def _flaky(provider):
        if provider != "openai":
            raise _Timeout()
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome
        return '{"ok": true}'

    _record_calls(monkeypatch, _flaky)

    assert LLMClient().complete_json("sys", "usr") == {"ok": True}
    assert breaker.is_open("openai") is False


def test_callable_providers_hides_only_the_broken_one(three_providers):
    for _ in range(settings.llm_circuit_failure_threshold):
        breaker.record_failure("anthropic", reason="TimeoutError")

    assert llm.available_providers() == ["openai", "anthropic", "openrouter"]
    assert callable_providers() == ["openai", "openrouter"]


def test_the_breaker_never_narrows_what_health_reports_as_configured(three_providers):
    """``llm_mode`` answers "is a key configured" and must not flip to offline on a blip.

    Coupling the two would make a transient outage change what the demo net does and what the
    public probe tells a clinician about the deployment — neither of which is the breaker's
    business.
    """
    for provider in ("openai", "anthropic", "openrouter"):
        for _ in range(settings.llm_circuit_failure_threshold):
            breaker.record_failure(provider, reason="TimeoutError")

    assert llm.available_providers() == ["openai", "anthropic", "openrouter"]
    assert llm.is_available() is True
    assert callable_providers() == []


def test_the_demo_net_still_catches_a_fully_broken_chain(three_providers, monkeypatch):
    """With the demo fallback on, a circuit-broken chain degrades to it rather than raising."""
    monkeypatch.setattr(settings, "llm_demo_fallback", True)
    monkeypatch.setattr(settings, "app_env", "development")
    for provider in ("openai", "anthropic", "openrouter"):
        for _ in range(settings.llm_circuit_failure_threshold):
            breaker.record_failure(provider, reason="TimeoutError")

    calls = _record_calls(monkeypatch, lambda provider: '{"unused": true}')
    result = LLMClient().complete_json("Generate a differential diagnosis.", "chest pain")

    assert calls == [], "the demo net must not be reached by way of a provider call"
    assert result, "the demo fallback returned nothing for a circuit-broken chain"


def test_production_never_serves_demo_data_when_the_chain_is_broken(three_providers, monkeypatch):
    """The breaker must not become a new route to fabricated clinical reasoning.

    ``demo_fallback_enabled`` already refuses in production; this pins that the circuit-broken
    path goes through the same gate rather than around it.
    """
    monkeypatch.setattr(settings, "llm_demo_fallback", True)
    monkeypatch.setattr(settings, "app_env", "production")
    for provider in ("openai", "anthropic", "openrouter"):
        for _ in range(settings.llm_circuit_failure_threshold):
            breaker.record_failure(provider, reason="TimeoutError")

    with pytest.raises(LLMUnavailable):
        LLMClient().complete_json("sys", "usr")


# --------------------------------------------------------------- the extraction client


def test_extraction_shares_the_breaker_with_the_reasoning_engine(monkeypatch):
    """A document upload during an outage must not rediscover it socket by socket.

    Extraction runs inline in the upload request, so this is where the chain's timeout is most
    visible to a clinician: they wait it out before reaching the deterministic text/OCR parser
    they were always going to land on.
    """
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", False)
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "an-key")
    monkeypatch.setattr(settings, "openrouter_api_key", "")

    calls: list[str] = []

    def _fail(provider):
        def _inner(file_bytes, file_type):
            calls.append(provider)
            raise _Timeout()

        return _inner

    monkeypatch.setitem(claude_client._EXTRACTORS, "openai", _fail("openai"))
    monkeypatch.setitem(claude_client._EXTRACTORS, "anthropic", _fail("anthropic"))

    # Extraction makes one attempt per provider per document, so it takes three uploads for a
    # provider to reach the failure threshold — unlike ``complete_json``, which exhausts a
    # provider's retries within a single call.
    for _ in range(settings.llm_circuit_failure_threshold):
        with pytest.raises(RuntimeError):
            claude_client.extract(b"scan", "image/png")
    assert calls == ["openai", "anthropic"] * settings.llm_circuit_failure_threshold, calls
    calls.clear()

    with pytest.raises(RuntimeError, match="circuit-broken"):
        claude_client.extract(b"scan", "image/png")
    assert calls == [], f"a fourth upload still paid the provider timeouts: {calls}"


def test_an_unsupported_file_type_does_not_break_the_provider(monkeypatch):
    """OpenAI refusing a PDF is a fact about the vision path, not about OpenAI being down.

    Counting it would let a clinician uploading three PDFs take OpenAI out of the *reasoning*
    engine's chain, which is a different subsystem reached over a different endpoint.
    """
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", False)
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")

    for _ in range(settings.llm_circuit_failure_threshold + 2):
        with pytest.raises(RuntimeError):
            claude_client.extract(b"%PDF-1.4", "pdf")

    assert breaker.is_open("openai") is False
    assert callable_providers() == ["openai"]


def test_extraction_still_falls_through_to_a_healthy_provider(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", False)
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "an-key")
    monkeypatch.setattr(settings, "openrouter_api_key", "")

    for _ in range(settings.llm_circuit_failure_threshold):
        breaker.record_failure("openai", reason="TimeoutError")

    called: list[str] = []

    def _openai(file_bytes, file_type):
        called.append("openai")
        raise _Timeout()

    def _anthropic(file_bytes, file_type):
        called.append("anthropic")
        return (
            '{"document_type": "prescription", "entities": [{"entity_type": "medication", '
            '"fields": {"brand_name_raw": "Crocin"}}]}'
        )

    monkeypatch.setitem(claude_client._EXTRACTORS, "openai", _openai)
    monkeypatch.setitem(claude_client._EXTRACTORS, "anthropic", _anthropic)

    entities, doc_type = claude_client.extract(b"scan", "image/png")

    assert called == ["anthropic"], f"the broken provider was still called: {called}"
    assert doc_type == "prescription"
    assert [e.entity_type for e in entities] == ["medication"]


# --------------------------------------------------------------- the operator's view


async def test_health_dependencies_reports_the_breaker(auth_client, monkeypatch):
    """An absorbed outage has to be visible, or it is just reasoning quietly getting worse.

    The breaker's whole value is that a provider outage stops producing a symptom an operator
    would notice — no more four-minute agent calls, no more runs timing out against the lease.
    Degrading silently is not better than degrading loudly unless the state is reported
    somewhere, so it is reported here.
    """
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_openrouter_fallback", False)
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "an-key")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(llm, "_probe", lambda provider: None)
    llm.reset_probe_cache()

    before = (await auth_client.get("/health/dependencies")).json()
    assert before["llm_circuit_breaker"] == {}
    assert before["llm_callable_providers"] == ["openai", "anthropic"]

    for _ in range(settings.llm_circuit_failure_threshold):
        breaker.record_failure("openai", reason="APITimeoutError")

    after = (await auth_client.get("/health/dependencies")).json()

    # The probe still says OpenAI is fine — it is a models-list call, not real traffic. The
    # divergence between the two is the diagnosis, which is why both are reported.
    assert after["llm_providers"]["openai"]["reachable"] is True
    assert after["llm_circuit_breaker"]["openai"]["state"] == "open"
    assert after["llm_circuit_breaker"]["openai"]["last_error"] == "APITimeoutError"
    assert after["llm_circuit_breaker"]["openai"]["seconds_until_retry"] > 0
    assert after["llm_callable_providers"] == ["anthropic"]
    # And the configuration-shaped fields are untouched, so nothing downstream of them moves.
    assert after["llm_available_providers"] == ["openai", "anthropic"]
    assert after["llm_mode"] == "live"


async def test_the_public_health_probe_says_nothing_about_the_breaker(auth_client, monkeypatch):
    """``/health`` is unauthenticated. Breaker state is infrastructure detail, and stays behind
    the same gate the rest of the vendor inventory is behind."""
    monkeypatch.setattr(settings, "openai_api_key", "oa-key")
    for _ in range(settings.llm_circuit_failure_threshold):
        breaker.record_failure("openai", reason="APITimeoutError")

    body = (await auth_client.get("/health")).json()

    assert "llm_circuit_breaker" not in body
    assert "llm_callable_providers" not in body
