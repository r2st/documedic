"""The application configures logging, and every record it emits is correlated.

Nothing in this codebase configured logging at all. uvicorn's ``LOGGING_CONFIG`` names three
loggers of its own and leaves the root logger untouched, which means the deployed process ran
with a root logger at WARNING and no handler on it. Two things followed, and both of them
shipped:

* Every ``logger.info`` under ``app.*`` was discarded before a handler could see it —
  ``isEnabledFor(INFO)`` was literally False. That is the channel ``AetherError.detail`` exists
  for: the mechanism behind a 401/403/409 is deliberately kept out of the clinician-facing
  message and logged instead, and it was being logged into nothing.
* Everything louder fell through to :data:`logging.lastResort`, which writes the bare message.
  No timestamp, no level, no logger name, no request id — a production traceback in
  ``journalctl`` with nothing to sort or correlate it by.

So these tests are about the two properties an operator actually depends on: that an INFO
record survives, and that whatever survives can be tied back to one request.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.logging_config import (
    _HANDLER_NAME,
    JsonFormatter,
    RequestIdFilter,
    build_handler,
    configure_logging,
)
from app.core.request_context import NO_REQUEST_ID, bind_request_id, reset_request_id
from app.main import create_app


def _aether_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if h.name == _HANDLER_NAME]


def _format(record: logging.LogRecord, handler: logging.Handler) -> str:
    for filt in handler.filters:
        filt.filter(record)
    assert handler.formatter is not None
    return handler.formatter.format(record)


def _record(level: int = logging.INFO, message: str = "line", **kwargs) -> logging.LogRecord:
    record = logging.LogRecord("app.demo", level, __file__, 1, message, None, None)
    for key, value in kwargs.items():
        setattr(record, key, value)
    return record


# --- The INFO channel exists at all ------------------------------------------------------


def test_info_records_survive_configuration():
    """The regression itself: without a root level, `app.*` INFO was dropped before a handler."""
    configure_logging()
    assert logging.getLogger("app.services.reasoning_service").isEnabledFor(logging.INFO)


def test_building_the_app_configures_logging():
    """`create_app` is the only entry point the deployment has; it must not leave this to luck."""
    logging.getLogger().handlers = [
        h for h in logging.getLogger().handlers if h.name != _HANDLER_NAME
    ]
    logging.getLogger().setLevel(logging.WARNING)

    create_app()

    assert _aether_handlers(), "create_app() left the root logger without a handler"
    assert logging.getLogger("app.anything").isEnabledFor(logging.INFO)


def test_configuration_is_idempotent():
    """One handler however many times it is called.

    ``create_app()`` runs once per process in the deployment and once per fixture here, and a
    handler appended per call multiplies every line by the number of applications ever built.
    """
    for _ in range(4):
        configure_logging()
    assert len(_aether_handlers()) == 1


def test_the_level_is_configurable():
    try:
        configure_logging(level="WARNING")
        assert not logging.getLogger("app.demo").isEnabledFor(logging.INFO)
    finally:
        configure_logging()


# --- Correlation on the record ------------------------------------------------------------


def test_the_filter_stamps_the_context_request_id():
    token = bind_request_id("req-from-context")
    try:
        record = _record()
        RequestIdFilter().filter(record)
        assert record.request_id == "req-from-context"
    finally:
        reset_request_id(token)


def test_a_record_outside_any_request_is_marked_as_such():
    """Startup seeding and sweeps are not requests, and must not wear an id that correlates
    with nothing."""
    record = _record()
    RequestIdFilter().filter(record)
    assert record.request_id == NO_REQUEST_ID


def test_an_explicit_request_id_beats_the_ambient_context():
    """A caller passing ``extra={"request_id": ...}`` knows better than the ambient context —
    a background task finishing one request's work while another is in flight."""
    token = bind_request_id("ambient")
    try:
        record = _record(request_id="explicit")
        RequestIdFilter().filter(record)
        assert record.request_id == "explicit"
    finally:
        reset_request_id(token)


# --- What the two formats actually emit ---------------------------------------------------


def test_text_format_carries_level_logger_and_request_id():
    handler = build_handler("text")
    token = bind_request_id("corr-1")
    try:
        line = _format(_record(logging.WARNING, "disk is filling"), handler)
    finally:
        reset_request_id(token)
    assert "WARNING" in line
    assert "[corr-1]" in line
    assert "app.demo" in line
    assert "disk is filling" in line


def test_json_format_is_one_parseable_object_per_line():
    handler = build_handler("json")
    token = bind_request_id("corr-2")
    try:
        line = _format(_record(logging.ERROR, "provider %s failed", args=("openrouter",)), handler)
    finally:
        reset_request_id(token)
    payload = json.loads(line)
    assert "\n" not in line
    assert payload["level"] == "ERROR"
    assert payload["logger"] == "app.demo"
    assert payload["request_id"] == "corr-2"
    assert payload["message"] == "provider openrouter failed"
    assert payload["ts"]


def test_json_format_renders_a_traceback_without_breaking_the_line():
    """A traceback is multi-line text; a line-delimited format has to keep it inside the
    object or every aggregator reading the stream loses the frames after the first."""
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = _record(logging.ERROR, "failed")
        record.exc_info = sys.exc_info()
    line = _format(record, build_handler("json"))
    payload = json.loads(line)
    assert "\n" not in line
    assert "RuntimeError" in payload["exception"]


def test_json_format_survives_an_unserialisable_extra():
    """A handler that raises loses the line it was trying to write, which is the worst possible
    moment for a logging failure."""
    line = _format(_record(extra_object=object()), build_handler("json"))
    assert json.loads(line)["extra"]["extra_object"]


def test_json_format_does_not_leak_the_record_internals():
    """The payload is a chosen field set, not a dump of ``record.__dict__`` — which carries
    ``args``, source paths and the raw ``msg`` template."""
    payload = json.loads(_format(_record(), build_handler("json")))
    assert set(payload) == {"ts", "level", "logger", "request_id", "message"}


def test_json_formatter_is_used_when_configured(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "log_format", "json")
    try:
        configure_logging()
        assert isinstance(_aether_handlers()[0].formatter, JsonFormatter)
    finally:
        monkeypatch.undo()
        configure_logging()


# --- One stream, not several --------------------------------------------------------------


def test_uvicorn_loggers_are_routed_through_the_one_handler():
    """Left with their own handlers and ``propagate = False``, uvicorn's lines keep uvicorn's
    format — so the same stream carries two shapes and only half of it parses. The access log
    is the half an operator greps by path."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(logging.NullHandler())
        logging.getLogger(name).propagate = False

    configure_logging()

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        assert logger.handlers == []
        assert logger.propagate is True


def test_the_handler_resolves_stderr_late():
    """It must not hold the stream object it was built with.

    ``logging.StreamHandler(sys.stderr)`` captures the stream, so a handler built while one
    ``sys.stderr`` was installed goes on writing to it after it has been replaced — and under
    pytest that buffer is closed, which turns any later log line into a handler error attached
    to an unrelated test.
    """
    import io
    import sys

    handler = build_handler("text")
    replacement = io.StringIO()
    monkeypatched = sys.stderr
    sys.stderr = replacement
    try:
        handler.emit(_record(message="late-bound"))
    finally:
        sys.stderr = monkeypatched
    assert "late-bound" in replacement.getvalue()


# --- The channel this was costing us -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_domain_error_detail_reaches_the_log(client, caplog):
    """``AetherError.detail`` is never serialized — the log is its *only* destination.

    With the root logger left at WARNING that destination did not exist, so the one place the
    mechanism behind a refusal was written was a place nothing was reading.
    """
    configure_logging()
    with caplog.at_level(logging.INFO, logger="app.main"):
        resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert resp.status_code == 401
    assert any(
        record.levelno == logging.INFO and "invalid_token" in record.getMessage()
        for record in caplog.records
    ), "the domain error's detail never reached the log"
    # And the detail stayed out of the response, which is the other half of the contract.
    assert "detail" not in resp.json()
