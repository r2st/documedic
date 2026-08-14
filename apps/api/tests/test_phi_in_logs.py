"""Patient data must not reach a log line, an audit payload, or the SSE stream.

``patients.full_name``/``phone``/``address_text``/``notes`` are encrypted at rest because they
are DPDP-sensitive. That protects the table and nothing else: application logs ship to an
aggregator, ``audit_logs.payload`` is unencrypted and never pruned, and the reasoning stream
goes straight to the browser. Every one of those is a way around the column encryption, and
none of them is covered by the response-body rules in ``app.main``.

The failure mode is not someone deliberately logging a name. It is ``str(exc)`` on a path that
just sent the chart somewhere:

  * OpenRouter — the primary provider — answers a moderation refusal with the flagged input in
    the error body, and the flagged input is the prompt.
  * A model that replies with prose instead of JSON fails with its whole reply as the message.
  * A DB constraint error quotes the offending row.

So the tests here are of two kinds. The first pin :func:`app.core.logsafe.describe_exception`,
which is the one place that decides what a failure is allowed to say. The second run real
requests with sentinel values in the patient fields and assert that nothing the application
logged contains them — a black-box check that does not care which module did the logging.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import select

from app.core.logsafe import describe_exception
from app.models.audit_log import AuditLog
from tests.conftest import create_patient

# Distinctive enough that a substring hit is a real leak and not a coincidence.
SENTINEL_NAME = "Zephyrine Qadirbux"
SENTINEL_PHONE = "9876500042"
SENTINEL_ADDRESS = "14B Marigold Lane, Kolar Gold Fields"
SENTINEL_NOTES = "Longstanding brittle diabetes; prior anaphylaxis to sulfonamides."
SENTINELS = (SENTINEL_NAME, SENTINEL_PHONE, SENTINEL_ADDRESS, SENTINEL_NOTES)


@pytest.fixture
def captured_logs():
    """Every record emitted anywhere under the ``app`` logger tree, fully formatted.

    Formatting matters: the leak is usually a lazy ``%s`` argument, so a test that reads
    ``record.msg`` alone sees the template and misses the value substituted into it.
    """
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    root = logging.getLogger("app")
    previous_level, previous_propagate = root.level, root.propagate
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        root.propagate = previous_propagate


def assert_no_phi(records: list[logging.LogRecord], *, expect_traceback: bool = False) -> None:
    """Assert no sentinel appears in what the application logged.

    ``expect_traceback`` covers the one deliberate exception. The 500 path calls
    ``logger.exception``, and a traceback carries the exception's own message;
    that is the only diagnostic an operator has for a crash, and suppressing it would trade a
    debuggable outage for an undebuggable one. The formatted messages are still swept — the
    point of this file is the *deliberate* writes (log arguments, audit payloads, the SSE
    stream), which do not get that exemption.
    """
    parts = [f"{record.name} {record.getMessage()}" for record in records]
    if not expect_traceback:
        parts += [record.exc_text or "" for record in records]
    rendered = "\n".join(parts)

    for sentinel in SENTINELS:
        assert sentinel not in rendered, f"{sentinel!r} reached the application log:\n{rendered}"


# ---------------------------------------------------------------- describe_exception


class _ProviderError(Exception):
    """Stands in for an SDK error: a message from upstream and an HTTP status."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _AppError(Exception):
    log_safe_message = True


def test_a_third_party_message_is_dropped_entirely() -> None:
    moderation = _ProviderError(
        "Error code: 403 - {'flagged_input': 'Patient Zephyrine Qadirbux, HbA1c 11.4%'}", 403
    )

    described = describe_exception(moderation)

    assert described == "_ProviderError (HTTP 403)"
    assert SENTINEL_NAME not in described


def test_the_status_code_survives_because_triage_needs_it() -> None:
    """429 and 500 call for different responses; the number itself cannot carry a chart."""
    assert describe_exception(_ProviderError("rate limited", 429)) == "_ProviderError (HTTP 429)"


def test_a_status_taken_from_a_response_object_is_used_too() -> None:
    class _WithResponse(Exception):
        response = type("R", (), {"status_code": 502})()

    assert describe_exception(_WithResponse("bad gateway")) == "_WithResponse (HTTP 502)"


def test_a_non_integer_status_is_ignored_rather_than_interpolated() -> None:
    class _Odd(Exception):
        status_code = "no idea"

    assert describe_exception(_Odd("x")) == "_Odd"


def test_a_bool_is_not_mistaken_for_a_status_code() -> None:
    """``status_code = True`` is an int in Python; rendering "HTTP 1" would be nonsense."""

    class _Flagged(Exception):
        status_code = True

    assert describe_exception(_Flagged("x")) == "_Flagged"


def test_a_message_this_codebase_authored_is_kept() -> None:
    assert describe_exception(_AppError("No LLM provider API key configured")) == (
        "_AppError: No LLM provider API key configured"
    )


def test_an_opted_in_exception_with_an_empty_message_degrades_to_its_type() -> None:
    assert describe_exception(_AppError("  ")) == "_AppError"


def test_none_is_described_rather_than_crashing_the_log_call() -> None:
    """The all-providers-failed path passes ``last_err``, which is None if nothing was tried."""
    assert describe_exception(None) == "unknown"


def test_llm_unavailable_opts_in_but_never_carries_a_provider_message() -> None:
    """The wrap site launders: ``LLMUnavailable(str(provider_err))`` would be logged in full."""
    from app.agents.llm import LLMUnavailable

    assert LLMUnavailable.log_safe_message is True
    assert describe_exception(LLMUnavailable("No LLM provider API key configured")) == (
        "LLMUnavailable: No LLM provider API key configured"
    )


# ---------------------------------------------------------------- end-to-end log sweep


async def test_creating_and_reading_a_patient_logs_none_of_their_details(
    auth_client, captured_logs
):
    patient = await create_patient(
        auth_client,
        full_name=SENTINEL_NAME,
        phone=SENTINEL_PHONE,
        address_text=SENTINEL_ADDRESS,
        notes=SENTINEL_NOTES,
    )

    assert (await auth_client.get(f"/api/v1/patients/{patient['id']}")).status_code == 200
    assert (await auth_client.get(f"/api/v1/patients/{patient['id']}/record")).status_code == 200
    assert (await auth_client.get("/api/v1/patients?q=Zephyrine")).status_code == 200

    assert_no_phi(captured_logs)


async def test_a_rejected_patient_payload_is_not_echoed_into_the_log(auth_client, captured_logs):
    """A 422 logs nothing of the body — and the body is the whole patient on a `missing` error."""
    resp = await auth_client.post(
        "/api/v1/patients",
        json={
            "phone": SENTINEL_PHONE,
            "address_text": SENTINEL_ADDRESS,
            "notes": SENTINEL_NOTES,
            "consent_given": True,
        },
    )

    assert resp.status_code == 422
    assert SENTINEL_NOTES not in resp.text, "the rejected body must not come back in the response"
    assert_no_phi(captured_logs)


async def test_an_unsupported_upload_does_not_log_its_leading_bytes(auth_client, captured_logs):
    """The rejected file's first bytes were logged verbatim; for a text file that is the note."""
    patient = await create_patient(auth_client, full_name=SENTINEL_NAME)
    note = f"{SENTINEL_NOTES} Patient: {SENTINEL_NAME}.".encode()

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("note.txt", note, "text/plain")},
    )

    assert resp.status_code == 422
    assert_no_phi(captured_logs)


async def test_an_unresolvable_drug_is_not_logged_against_the_patient(auth_client, captured_logs):
    """The clinician sees the name they typed; the log gets the reference id and nothing else."""
    patient = await create_patient(auth_client, full_name=SENTINEL_NAME)

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Zephyrimycin 250"},
    )

    assert resp.status_code == 422
    # Echoing it back to the clinician who just typed it is the point of the message.
    assert "Zephyrimycin 250" in resp.text
    rendered = "\n".join(record.getMessage() for record in captured_logs)
    assert "Zephyrimycin 250" not in rendered, (
        "the proposed drug is this patient's prescribing; the log line is correlated to them "
        "by request id"
    )


async def test_a_failed_reasoning_run_writes_only_the_exception_type_to_the_audit_trail(
    db, auth_client, monkeypatch, captured_logs
):
    """audit_logs.payload is unencrypted, immutable and never pruned — the strictest sink here."""
    from app.services import reasoning_service as rs

    patient = await create_patient(auth_client, full_name=SENTINEL_NAME)
    start = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "fever and joint pain for four days"},
    )
    assert start.status_code == 201, start.text

    async def _boom(state, ctx):
        raise RuntimeError(f"graph failed rendering chart for {SENTINEL_NAME}")

    monkeypatch.setattr(rs.graph, "run_reasoning", _boom)

    session_id = start.json()["session"]["id"]
    # That the *body* stays clean is pinned in test_health_and_errors; it is re-checked here
    # only because this failure is the realistic one, carrying a name in its message. What
    # this test is actually about is what was written on the way past — the audit row and the
    # session's error_detail, both committed before the exception reached the middleware.
    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert resp.status_code == 500
    assert SENTINEL_NAME not in resp.text

    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "reasoning_session_failed")))
        .scalars()
        .all()
    )
    assert rows, "a failed run must still be audited"
    for row in rows:
        assert row.payload == {"error": "RuntimeError"}
    assert_no_phi(captured_logs, expect_traceback=True)


async def test_the_sse_stream_relays_the_failure_type_and_not_the_exception_text(
    db, monkeypatch, captured_logs
):
    """SSE is the one response channel that bypasses ``app.main``'s exception handlers."""
    from app.services.reasoning_service import ReasoningService

    service = ReasoningService(db)

    async def _boom(*args, **kwargs):
        raise RuntimeError(f"pipeline died on {SENTINEL_NAME} — {SENTINEL_NOTES}")

    monkeypatch.setattr(service, "run", _boom)

    events = [item async for item in service.stream(uuid.uuid4(), uuid.uuid4())]

    assert events == [("error", {"message": "RuntimeError"})]
    assert_no_phi(captured_logs)
