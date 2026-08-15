"""The application's logging configuration, which until now there was none of.

Nothing in this codebase ever called ``basicConfig`` or ``dictConfig``, and ``uvicorn`` does not
configure the root logger — its ``LOGGING_CONFIG`` names ``uvicorn``, ``uvicorn.error`` and
``uvicorn.access`` and stops there. So in the deployed process the root logger had no handler
and sat at its default level of WARNING, and everything under ``app.*`` inherited that. Two
consequences, both of which were shipping:

**Every INFO line was discarded.** ``logging.getLogger("app.…").isEnabledFor(INFO)`` was False.
That is not a quiet loss of chatter — it is the whole diagnostic channel this codebase builds
deliberately. ``AetherError.detail`` exists so that the mechanism ("token type 'refresh',
expected 'access'") stays out of the clinician-facing message and goes to the log instead;
``app.main.aether_error_handler`` logs it at INFO. The 401/403/409 storm an operator is trying
to explain produced no output at all. Same for the malformed-``X-Request-Id`` notice, the
startup seed counts, and the reasoning run's progress lines.

**Everything above INFO was unattributable.** With no handler anywhere, WARNING and above fell
through to :data:`logging.lastResort`, which writes the bare message to stderr with no
timestamp, no level, no logger name — and no request id. A production traceback in
``journalctl`` was a wall of text with nothing to sort, filter or correlate it by.

So this module does two things: it installs a handler and a level, and it puts the correlation
id from :mod:`app.core.request_context` on every record that passes through. The id is added by
a filter rather than by call sites, which is what makes it retroactive — some sixty existing
``logger.…`` calls across the services, agents and extraction pipeline become correlated without
being touched.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, TextIO

from app.core.request_context import NO_REQUEST_ID, current_request_id

# Marks the handler this module owns, so a second call replaces it instead of stacking another
# one alongside (which is how a process ends up printing everything twice).
_HANDLER_NAME = "aether"

TEXT_FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"

# Record attributes that logging populates itself. Anything on a record outside this set was put
# there by a caller via ``extra=``, and is carried into the JSON payload.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "request_id",
    "message",
    "asctime",
    "taskName",
}

# uvicorn's own loggers, which arrive already configured with their own handlers and
# ``propagate = False``. Left alone they would keep writing in uvicorn's format, so the same
# stream would carry two shapes of line and only half of them would be parseable — the access
# log especially, which is the one an operator greps by path.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class RequestIdFilter(logging.Filter):
    """Stamp the context's correlation id onto every record.

    A filter rather than a ``LoggerAdapter`` or an ``extra=`` argument at each call site,
    because the records that most need the id are the ones nobody thought to annotate: a
    ``logger.exception`` deep in the extraction pipeline, or a third-party library's warning.
    Attached to the handler rather than to a logger so it also covers records that propagate up
    from loggers this module never names.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # setattr rather than assignment because a record may already carry an explicit
        # request_id passed through ``extra=``; that caller knows better than the ambient
        # context (a background sweep processing one request's work, say).
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id() or NO_REQUEST_ID
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for a deployment that ships logs to an aggregator.

    Hand-rolled rather than pulled in as a dependency: the field set is small, and a log
    formatter is the last place to want a third-party import that can fail at interpreter
    shutdown while something is trying to report why it is shutting down.

    The fields are chosen to be the ones that cannot themselves be carrying patient data —
    level, logger, timestamp, correlation id — plus the message, which is subject to the same
    rule every log call in this codebase already follows (see :mod:`app.core.logsafe`: what a
    failure is allowed to *say* is decided there, not here).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", NO_REQUEST_ID),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        extras = {
            key: value for key, value in record.__dict__.items() if key not in _STANDARD_ATTRS
        }
        if extras:
            # ``default=str`` so an unserialisable extra degrades to its repr instead of
            # raising inside the handler, which logging would swallow into a lost line.
            payload["extra"] = extras
        return json.dumps(payload, default=str)


class StderrHandler(logging.StreamHandler):
    """A stream handler that resolves ``sys.stderr`` when it writes, not when it is built.

    ``logging.StreamHandler(sys.stderr)`` captures the stream object, which is wrong wherever
    something replaces ``sys.stderr`` after configuration: under pytest that happens once per
    test, so a handler built during one test would go on holding — and eventually writing to —
    a capture buffer that has since been closed, which logging reports as a handler error on a
    line that has nothing to do with the test that failed. Late binding is what the standard
    library's own last-resort handler does, for the same reason.
    """

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, value: TextIO) -> None:
        """Ignored. ``StreamHandler.__init__`` and ``setStream`` both assign here."""


def build_handler(log_format: str) -> logging.Handler:
    """The single stderr handler, formatted per ``log_format`` and carrying the id filter.

    stderr rather than stdout because that is where uvicorn writes and where a container
    runtime and systemd both collect from by default, so the application's lines interleave
    with the server's in the order they happened.
    """
    handler = StderrHandler()
    handler.name = _HANDLER_NAME
    handler.setFormatter(
        JsonFormatter() if log_format == "json" else logging.Formatter(TEXT_FORMAT)
    )
    handler.addFilter(RequestIdFilter())
    return handler


def configure_logging(*, level: str | None = None, log_format: str | None = None) -> None:
    """Install the root handler and level. Idempotent, and safe to call from a test.

    Idempotence is not a nicety here. ``create_app()`` is called once per process in the
    deployment and once per fixture in the test suite, and a handler appended on each call
    would multiply every line by the number of applications ever built.

    uvicorn's loggers are re-pointed at this handler rather than left with their own, so the
    access log and the application log come out in one format. That is safe in the order things
    actually run: uvicorn applies its ``LOGGING_CONFIG`` while constructing its ``Config``,
    before it imports the application, so this always runs second and wins.
    """
    from app.config import settings

    resolved_level = (level or settings.log_level).upper()
    resolved_format = (log_format or settings.log_format).lower()

    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.name == _HANDLER_NAME]:
        root.removeHandler(existing)
    root.addHandler(build_handler(resolved_format))
    # setLevel on the root logger rather than on the handler: the level is what decides whether
    # a record is created at all, and an INFO record that the root logger drops never reaches a
    # handler to be filtered by. This is the line that turns the INFO channel back on.
    root.setLevel(resolved_level)

    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
