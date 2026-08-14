"""What the API does when a dependency it does not control fails underneath a request.

Every other test in this suite runs against dependencies that work. These run against ones
that do not, because the interesting question about a clinical system is not what it does when
everything is up — it is whether a clinician mid-consultation is left with a blank screen, a
stack trace, or a chart they cannot tell the state of.

Three failures, each with its own correct answer:

* **the LLM provider is down.** Degrade, do not fail. Reasoning falls back to the deterministic
  path, marks the case degraded, and escalates to human review; the drug-safety and lab checks
  that never needed a model keep running unchanged. A 500 here would be the wrong answer twice
  over — the work is still useful without a model, and it is the clinician who decides anyway.
* **the database connection drops.** Fail, do not degrade. There is no useful answer without
  the chart, so the request must end in a structured 500 that says nothing was written, and
  the transaction must be gone rather than half-applied.
* **the object store cannot take the bytes.** Fail without leaving a record of a scan that is
  not there.

In all three the response body is a contract: ``{code, message}``, a correlation id, and none
of the underlying error's text — which routinely quotes the prompt, the row, or the path.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.agents import llm
from app.config import settings
from app.db.session import get_db
from app.models.document import Document
from app.models.patient import Patient
from tests.conftest import create_patient

# The suite's standard scan: a metformin prescription alongside the creatinine that makes it a
# renal hard block. Shared so this file exercises the same deterministic safety path the safety
# tests do, with no model involved at any point.
from tests.test_documents import PRESCRIPTION

# A name that would be a direct identifier if it escaped into a response body or a log line.
# Both fakes below carry it the way a real failure does: a provider quoting the prompt it was
# sent, a driver quoting the row it choked on.
PHI_IN_THE_ERROR = "Asha Reddy, dob 1979-02-11"


@pytest_asyncio.fixture
async def failing_client(app, auth_client):
    """The signed-in clinician, over a transport that returns the 500 instead of re-raising.

    ``ASGITransport`` re-raises an unhandled exception by default, which is what the rest of
    the suite wants. Here the response *is* the thing under test: these tests are about what
    the clinician's browser receives when the process below it has already failed.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.headers["Authorization"] = auth_client.headers["Authorization"]
        yield client


# ======================================================================================
# The LLM provider is down
# ======================================================================================


@pytest.fixture
def openrouter_outage(monkeypatch):
    """OpenRouter configured and keyed, and every call to it failing.

    This is the shape of a real outage rather than a missing key: ``available_providers()``
    reports a provider, ``is_available()`` is True, and the agents therefore *try* — which is
    the path that had no end-to-end coverage. The demo net stays off (conftest disables it), so
    nothing catches the failure except the deterministic fallback.
    """
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-test")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    # Retries are the point of the fallback chain, but their backoff is not under test here.
    monkeypatch.setattr(settings, "llm_retry_backoff_base_seconds", 0.0)

    calls: list[str] = []

    def _down(system: str, user: str, model: str, max_tokens: int) -> str:
        calls.append(user)
        raise ConnectionError(f"upstream 502; flagged_input: {PHI_IN_THE_ERROR}")

    monkeypatch.setattr(llm, "_complete_openrouter", _down)
    return calls


async def _run_reasoning(client, patient_id: str) -> dict:
    started = await client.post(
        f"/api/v1/patients/{patient_id}/reasoning",
        json={"presenting_complaint": "breathless climbing stairs for a month"},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session"]["id"]
    await client.post(f"/api/v1/reasoning/{session_id}/answers", json={"answers": []})
    run = await client.post(f"/api/v1/reasoning/{session_id}/run")
    assert run.status_code == 200, run.text
    return run.json()


async def test_a_provider_outage_still_produces_a_result(auth_client, openrouter_outage):
    """The consultation does not stop because a vendor is having an afternoon."""
    patient = await create_patient(auth_client)
    result = await _run_reasoning(auth_client, patient["id"])

    assert openrouter_outage, "the provider was never called; this is not testing an outage"
    assert result["case_state"]["degraded"] is True
    assert result["suggestions"], "degraded mode produced nothing at all for the clinician"


async def test_an_outage_escalates_the_case_to_a_human(auth_client, openrouter_outage):
    """Degraded output is never auto-executed: the tier drops to flag_for_review.

    This is the whole safety argument for degrading rather than failing — the fallback is
    allowed to be worse than the model because a person is guaranteed to read it.
    """
    patient = await create_patient(auth_client)
    result = await _run_reasoning(auth_client, patient["id"])

    assert result["session"]["autonomy_tier"] == "flag_for_review"
    assert result["session"]["status"] == "awaiting_review"


async def test_the_clinician_is_told_the_reasoning_ran_degraded(auth_client, openrouter_outage):
    """Silently worse output is the failure mode this caveat exists to prevent."""
    patient = await create_patient(auth_client)
    result = await _run_reasoning(auth_client, patient["id"])

    caveats = [
        caveat
        for verdict in result["case_state"]["verifier_verdicts"]
        for caveat in verdict["caveats"]
    ]
    assert any("degraded mode" in caveat for caveat in caveats), caveats


async def test_the_provider_error_text_reaches_neither_the_response_nor_the_audit_trail(
    auth_client, openrouter_outage
):
    """A moderation refusal quotes the prompt back, and the prompt is the chart."""
    patient = await create_patient(auth_client)
    result = await _run_reasoning(auth_client, patient["id"])
    assert PHI_IN_THE_ERROR not in str(result)

    audit = await auth_client.get(f"/api/v1/patients/{patient['id']}/audit", params={"limit": 200})
    assert audit.status_code == 200
    assert PHI_IN_THE_ERROR not in audit.text
    # Not a bare "502": a hex id can contain those digits. The provider's own wording is what
    # would only be there if its response body had been written through.
    assert "flagged_input" not in audit.text
    assert "upstream 502" not in audit.text


async def test_the_outage_is_logged_by_provider_and_exception_type_only(
    auth_client, openrouter_outage, caplog
):
    """Operators need to know OpenRouter is down; they must not learn it from the chart."""
    patient = await create_patient(auth_client)
    with caplog.at_level(logging.WARNING, logger="app.agents.llm"):
        await _run_reasoning(auth_client, patient["id"])

    text = caplog.text
    assert "openrouter" in text
    assert "ConnectionError" in text
    assert PHI_IN_THE_ERROR not in text
    assert "breathless climbing stairs" not in text


async def test_drug_safety_still_answers_with_every_provider_down(auth_client, openrouter_outage):
    """`/health` calls this out: offline does not mean unsafe, it means un-reasoned.

    The interaction and contraindication checks are table lookups. If an LLM outage took them
    with it, the deterministic core would be no more available than the model it backs up.
    """
    patient = await create_patient(auth_client)
    uploaded = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    approved = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{uploaded.json()['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approved.status_code == 200, approved.text

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_reference_id": "MET-500"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_hard_block"] is True, "the renal hard block went down with the LLM"
    assert any(f["check_type"] == "renal_dose" for f in body["flags"])


async def test_the_standing_safety_screen_answers_with_every_provider_down(
    auth_client, openrouter_outage
):
    """The other half of the same rule, and the one a clinician actually opens.

    ``POST ../check`` is the pre-prescription question. ``GET ../flags`` is the standing picture
    of the chart — every current medication re-evaluated against every other, plus the
    chart-level notes — and it is what the safety screen renders on load. An outage that left
    ``check`` working and this returning 500 would present as the safety screen being down.

    Asserted on the shape rather than on one finding, because the check list grows: this file
    named ``renal_dose`` above and could not speak for the two cumulative checks added since.
    """
    patient = await create_patient(auth_client)
    uploaded = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )
    assert uploaded.status_code == 201, uploaded.text
    approved = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{uploaded.json()['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approved.status_code == 200, approved.text

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/drug-safety/flags")

    assert resp.status_code == 200, resp.text
    flags = resp.json()["flags"]
    assert flags, "the standing safety picture came back empty during the outage"
    assert all(f["summary"] for f in flags), "a flag arrived without the text that explains it"


async def test_the_lab_critical_value_screen_answers_with_every_provider_down(
    auth_client, openrouter_outage
):
    """The third deterministic surface. Panic values are a table of thresholds and a unit
    conversion, and they are the findings with the shortest fuse on the whole chart."""
    patient = await create_patient(auth_client)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/labs/critical-flags")

    assert resp.status_code == 200, resp.text


async def test_health_still_reports_the_provider_as_configured_during_an_outage(
    auth_client, openrouter_outage
):
    """A keyed provider that is failing reads as ``live``, which is the honest answer.

    The probe reports configuration, not reachability — calling upstream on every health check
    would turn a vendor's outage into a failing readiness probe and take the API down with it.
    ``degraded`` on the case is where a *specific* failure surfaces.
    """
    resp = await auth_client.get("/health/dependencies")
    assert resp.status_code == 200
    assert resp.json()["llm_mode"] == "live"
    assert "openrouter" in resp.json()["llm_available_providers"]


def test_a_hung_provider_is_bounded_by_the_configured_timeout(monkeypatch):
    """Every attempt is bounded, so a hung socket cannot pin the worker thread forever.

    The bound that matters is the product: providers x attempts x timeout. Left unbounded on
    any factor, a single reasoning request outlives the clinician's patience and holds a
    connection while it does.
    """
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-test")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "llm_retry_backoff_base_seconds", 0.0)

    attempts = 0

    def _hang(system: str, user: str, model: str, max_tokens: int) -> str:
        nonlocal attempts
        attempts += 1
        raise TimeoutError("Request timed out.")

    monkeypatch.setattr(llm, "_complete_openrouter", _hang)

    with pytest.raises(llm.LLMUnavailable) as caught:
        llm.LLMClient().complete_json("system", "user", retries=2)

    assert attempts == 3, "a hung provider was retried more times than configured"
    assert str(caught.value) == "TimeoutError", "the provider's own text became our message"
    assert settings.llm_request_timeout_seconds > 0


def test_an_outage_on_one_provider_fails_over_to_the_next(monkeypatch):
    """The chain exists so one vendor's outage is not the system's outage."""
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", "sk-or-test")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "llm_fallback_enabled", True)
    monkeypatch.setattr(settings, "llm_retry_backoff_base_seconds", 0.0)

    monkeypatch.setattr(
        llm,
        "_complete_openrouter",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("502")),
    )
    monkeypatch.setattr(llm, "_complete_openai", lambda *a, **k: '{"ok": true}')

    assert llm.LLMClient().complete_json("system", "user") == {"ok": True}


# ======================================================================================
# The database connection drops
# ======================================================================================


class DroppedConnection(Exception):
    """Stands in for asyncpg's ``ConnectionDoesNotExistError``.

    Its message quotes a row, which is what a real driver error does and why none of it may
    reach a response body.
    """


def dropped_connection_error(statement: str = "SELECT 1") -> OperationalError:
    return OperationalError(
        statement,
        {},
        DroppedConnection(
            f"connection was closed in the middle of operation; last row: {PHI_IN_THE_ERROR}"
        ),
    )


class FailingSession:
    """A real session whose connection drops after ``healthy_calls`` statements.

    Delegates everything it does not intercept, so authentication, the rate limiter and the
    ownership checks all run for real and the failure lands mid-request — which is the case
    that matters. Failing from the very first statement only ever tests the auth dependency.
    """

    def __init__(self, inner, *, fail_on: set[str], healthy_calls: int = 0) -> None:
        self._inner = inner
        self._fail_on = fail_on
        self._remaining = healthy_calls
        self.rolled_back = False

    def __getattr__(self, name: str):
        attribute = getattr(self._inner, name)
        if name not in self._fail_on:
            return attribute

        async def _drop(*args, **kwargs):
            if self._remaining > 0:
                self._remaining -= 1
                return await attribute(*args, **kwargs)
            raise dropped_connection_error()

        return _drop

    async def rollback(self) -> None:
        self.rolled_back = True
        await self._inner.rollback()


def break_the_database(app, sessionmaker, *, fail_on: set[str], healthy_calls: int = 0):
    """Point the app's request-scoped session at a connection that drops. Returns the sessions."""
    broken: list[FailingSession] = []

    async def _override() -> AsyncGenerator[FailingSession, None]:
        async with sessionmaker() as session:
            failing = FailingSession(session, fail_on=fail_on, healthy_calls=healthy_calls)
            broken.append(failing)
            try:
                yield failing
            except Exception:
                await failing.rollback()
                raise

    app.dependency_overrides[get_db] = _override
    return broken


async def _client_for(app) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


async def test_the_readiness_probe_fails_when_the_database_is_gone(app, sessionmaker):
    """A pod that cannot reach the chart must be taken out of rotation, not reported ready."""
    break_the_database(app, sessionmaker, fail_on={"execute"})

    async with await _client_for(app) as client:
        resp = await client.get("/health/ready")

    assert resp.status_code == 500
    assert resp.json()["code"] == "internal_error"


async def test_the_liveness_probe_still_answers_without_a_database(app, sessionmaker):
    """Liveness must not depend on the database, or an outage becomes a restart loop."""
    break_the_database(app, sessionmaker, fail_on={"execute"})

    async with await _client_for(app) as client:
        resp = await client.get("/health/live")

    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


async def test_the_dependency_probe_reports_the_outage_instead_of_failing(
    app, sessionmaker, failing_client
):
    """The operator-facing probe is the one place a dead database is a *field*, not a 500."""
    break_the_database(app, sessionmaker, fail_on={"execute"})

    resp = await failing_client.get("/health/dependencies")

    assert resp.status_code == 200, resp.text
    assert resp.json()["database"] == "error"


async def test_a_read_that_loses_the_connection_returns_a_structured_error(
    app, sessionmaker, failing_client
):
    """No blank page and no stack trace: a code, a message, and a reference to quote."""
    break_the_database(app, sessionmaker, fail_on={"execute"})

    resp = await failing_client.get("/api/v1/patients")

    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "internal_error"
    assert body["request_id"] == resp.headers["X-Request-Id"]
    assert "nothing was saved to the chart" in body["message"]


async def test_the_driver_error_text_never_reaches_the_client(app, sessionmaker, failing_client):
    """A driver quotes the row it choked on, and the row is a patient."""
    break_the_database(app, sessionmaker, fail_on={"execute"})

    resp = await failing_client.get("/api/v1/patients")

    assert PHI_IN_THE_ERROR not in resp.text
    for leak in ("OperationalError", "DroppedConnection", "Traceback", "SELECT", "sqlalchemy"):
        assert leak not in resp.text, f"{leak!r} leaked into the error body"


async def test_the_failure_is_logged_against_the_request_id_the_client_was_given(
    app, sessionmaker, failing_client, caplog
):
    """The reference in the clinician's message is only useful if it is in the log too."""
    break_the_database(app, sessionmaker, fail_on={"execute"})

    with caplog.at_level(logging.ERROR, logger="app.main"):
        resp = await failing_client.get("/api/v1/patients")

    assert resp.headers["X-Request-Id"] in caplog.text
    # The traceback belongs in the log, which is exactly why it must not be in the response.
    assert "OperationalError" in caplog.text


async def test_a_write_that_loses_the_connection_leaves_nothing_behind(
    app, sessionmaker, failing_client, db
):
    """ "Retrying is safe" is a promise the message makes; this is the test that keeps it."""
    broken = break_the_database(app, sessionmaker, fail_on={"commit"})

    resp = await failing_client.post(
        "/api/v1/patients",
        json={
            "full_name": "Half Written",
            "sex": "female",
            "date_of_birth": "1979-02-11",
            "consent_given": True,
        },
    )

    assert resp.status_code == 500
    assert resp.json()["code"] == "internal_error"
    assert broken[-1].rolled_back, "the request-scoped session was not rolled back"
    # Read back through a session that never saw the failure.
    names = (await db.execute(select(Patient.full_name))).scalars().all()
    assert not any(name for name in names if "Half Written" in str(name))


async def test_the_request_scoped_session_is_rolled_back_when_the_handler_raises(monkeypatch):
    """``get_db`` owns this, and it is what makes a mid-request failure recoverable.

    Without the rollback the connection goes back to the pool inside a failed transaction, and
    the *next* request to draw it fails on a statement that had nothing wrong with it.
    """
    import app.db.session as session_module

    rolled_back = False

    class _Session:
        async def rollback(self) -> None:
            nonlocal rolled_back
            rolled_back = True

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: _Session)

    generator = get_db()
    await generator.asend(None)
    with pytest.raises(OperationalError):
        await generator.athrow(dropped_connection_error())

    assert rolled_back


async def test_one_broken_request_does_not_break_the_next(app, sessionmaker, failing_client, db):
    """Each request gets its own session, so a dropped connection is not sticky."""
    break_the_database(app, sessionmaker, fail_on={"execute"})
    assert (await failing_client.get("/api/v1/patients")).status_code == 500

    # The connection comes back: restore the healthy dependency and retry the same call.
    async def _healthy() -> AsyncGenerator:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_db] = _healthy
    recovered = await failing_client.get("/api/v1/patients")

    assert recovered.status_code == 200, recovered.text


# ======================================================================================
# The object store cannot take the bytes
# ======================================================================================


async def test_a_storage_failure_does_not_leave_a_document_row_pointing_at_nothing(
    failing_client, db, monkeypatch
):
    """A row whose bytes are missing is worse than no row: the chart claims a scan it lost."""
    patient = await create_patient(failing_client)

    from app.services import storage as storage_module

    def _disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage_module.LocalStorage, "write", _disk_full)

    resp = await failing_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )

    assert resp.status_code == 500
    assert resp.json()["code"] == "internal_error"
    assert "No space left" not in resp.text
    documents = (await db.execute(select(Document))).scalars().all()
    assert documents == []


async def test_the_upload_can_be_retried_once_storage_comes_back(failing_client, db, monkeypatch):
    """Nothing about the failed attempt blocks the retry — no half-written dedup entry."""
    patient = await create_patient(failing_client)
    from app.services import storage as storage_module

    original_write = storage_module.LocalStorage.write
    monkeypatch.setattr(
        storage_module.LocalStorage,
        "write",
        lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device")),
    )
    failed = await failing_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )
    assert failed.status_code == 500

    monkeypatch.setattr(storage_module.LocalStorage, "write", original_write)
    retried = await failing_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )

    assert retried.status_code == 201, retried.text
    assert uuid.UUID(retried.json()["id"])
