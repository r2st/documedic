"""What ``GET /health/ready`` promises: a bounded answer, the right status, and no detail.

A readiness probe is a question about the present, so the only useful thing it can do when a
dependency stops responding is say so quickly. ``await db.execute(text("SELECT 1"))`` waits as
long as the connection does, and the outage this probe exists to catch is not a database that
refuses — that one answers immediately — but one that accepted the connection and went quiet.
Unbounded, the probe simply never answered: the orchestrator saw a hanging request rather than
an unready instance, kept it in rotation until its own deadline, and every poll meanwhile held
a pooled connection open for the duration.

Two smaller properties travel with that. The failure is a 503, not the generic 500 the
unhandled path produced, because a load balancer reads those differently — 500 is "this
instance is broken", 503 is "not ready yet". And the body says nothing about why: the probe
answers before any authentication runs, so it is as public as ``/health/dependencies`` was
before that route was closed off.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db.session import get_db


@pytest.fixture
def hanging_db(app):
    """Override the session dependency with one whose ``execute`` never returns.

    A database that accepted the connection and stopped answering, which is the state the
    timeout exists for and the one no test could previously distinguish from a healthy one.
    """

    class _Hanging:
        async def execute(self, *_args, **_kwargs):
            await asyncio.Event().wait()

    async def _override():
        yield _Hanging()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override
    yield
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest.fixture
def failing_db(app):
    """A database that refuses, with a driver error whose text quotes the connection."""

    class _Failing:
        async def execute(self, *_args, **_kwargs):
            raise OperationalError(
                "SELECT 1",
                {},
                Exception(
                    "connection to server at 10.0.0.7 port 5432 failed: "
                    'FATAL: password authentication failed for user "aether"'
                ),
            )

    async def _override():
        yield _Failing()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override
    yield
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


@pytest.mark.asyncio
async def test_a_healthy_database_answers_ready(client):
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_a_hung_database_answers_rather_than_hanging(client, hanging_db, monkeypatch):
    """The regression. Before the timeout this call did not return at all."""
    monkeypatch.setattr(settings, "readiness_timeout_seconds", 0.05)

    resp = await asyncio.wait_for(client.get("/health/ready"), timeout=5)

    assert resp.status_code == 503
    assert resp.json()["code"] == "not_ready"


@pytest.mark.asyncio
async def test_the_timeout_is_the_configured_one(client, hanging_db, monkeypatch):
    """Bounded by the setting, not by whatever the caller happens to be waiting with."""
    monkeypatch.setattr(settings, "readiness_timeout_seconds", 0.2)

    loop = asyncio.get_running_loop()
    started = loop.time()
    resp = await client.get("/health/ready")
    elapsed = loop.time() - started

    assert resp.status_code == 503
    # Generous on both sides: the assertion is that it is governed by the setting at all, not
    # that a CI box hits a stopwatch.
    assert 0.15 <= elapsed < 3.0


@pytest.mark.asyncio
async def test_a_refusing_database_is_not_ready_either(client, failing_db):
    resp = await client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["code"] == "not_ready"


@pytest.mark.asyncio
async def test_the_failure_body_carries_nothing_internal(client, failing_db):
    """This route is answered before any authentication, so its body is public. A driver error
    quotes the host, the port, the user and the failure mode; ``/health/dependencies`` was made
    authenticated to stop publishing exactly that."""
    body = (await client.get("/health/ready")).text
    for internal in ("10.0.0.7", "5432", "aether", "password", "OperationalError", "SELECT 1"):
        assert internal not in body, f"{internal!r} leaked into the readiness body"


@pytest.mark.asyncio
async def test_the_failure_is_diagnosable_from_the_log(client, failing_db, caplog):
    """What is kept out of the body still has to reach the operator. ``AetherError.detail`` is
    the channel for that, and it is logged against the request id."""
    import logging

    with caplog.at_level(logging.INFO, logger="app.main"):
        await client.get("/health/ready")
    assert any("NotReadyError" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_liveness_still_answers_while_readiness_fails(client, hanging_db, monkeypatch):
    """The whole reason the two probes are separate. A database outage must not restart the
    process — the deterministic safety engine and the record are what a restart would
    interrupt, and neither is what is broken."""
    monkeypatch.setattr(settings, "readiness_timeout_seconds", 0.05)

    assert (await client.get("/health/ready")).status_code == 503
    assert (await client.get("/health/live")).json() == {"status": "alive"}


@pytest.mark.asyncio
async def test_liveness_touches_no_dependency_at_all(client, failing_db):
    """Liveness is a restart-me signal, so it must not be able to fail for a reason a restart
    would not fix."""
    assert (await client.get("/health/live")).status_code == 200


@pytest.mark.asyncio
async def test_an_unreachable_llm_does_not_make_the_instance_unready(client, monkeypatch):
    """Pinned deliberately, against a future round adding "check the LLM" to readiness.

    Every deterministic drug-safety, allergy, contraindication and lab check runs without a
    provider (CLAUDE.md rule 8), so an instance with no LLM is still an instance a clinician
    needs. Gating readiness on provider reachability would take the whole deployment out of
    rotation during a third-party outage and turn "AI reasoning paused" into "no access to the
    chart". Provider state belongs on ``/health/dependencies``, which reports without gating.
    """
    from app.agents.circuit import breaker

    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    for provider in ("openai", "anthropic", "openrouter"):
        for _ in range(10):
            breaker.record_failure(provider, reason="probe")

    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_the_readiness_failure_carries_the_correlation_headers(client, failing_db):
    """A 503 is still a response, and gets the same envelope every other error does."""
    resp = await client.get("/health/ready", headers={"X-Request-Id": "ready-corr"})
    assert resp.status_code == 503
    assert resp.headers["X-Request-Id"] == "ready-corr"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
