"""Cross-cutting middleware: request-id correlation and security headers (P1-11b)."""

from __future__ import annotations

import logging
import time
import uuid

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.exceptions import RequestTooLargeError

logger = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
}

HSTS_VALUE = "max-age=63072000; includeSubDomains"


def apply_security_headers(response: Response) -> None:
    """Set the security headers on a response, leaving any already-set value alone.

    Split out of the middleware so the 500 path can reuse it. A response built by
    Starlette's ``ServerErrorMiddleware`` never passes back through this middleware (see
    ``RequestContextMiddleware.dispatch``), and nothing about a server error makes
    ``nosniff`` or ``frame-ancestors 'none'`` less necessary — an error body is still a body
    a browser will sniff and still a page an attacker would like to frame.
    """
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if settings.is_production:
        response.headers.setdefault("Strict-Transport-Security", HSTS_VALUE)


def internal_error_response(request_id: str | None) -> JSONResponse:
    """The 500 body, in the same ``{code, message}`` shape as every other error.

    Nothing internal goes in it. An unhandled exception can easily be carrying patient data
    in its ``args`` — a database constraint error quotes the offending row — and that must
    never reach the response body, a frontend error reporter, or a proxy access log.

    The request id is in the body as well as the ``X-Request-Id`` header because nobody opens
    devtools mid-consultation: the reference the clinician can read aloud to an administrator
    is the only thing tying their report to the logged traceback. "Retrying is safe" is a
    claim, not filler — ``get_db`` rolls the request's transaction back — and it answers the
    question a clinician actually has, which is whether a half-written record is now in the
    chart.
    """
    return JSONResponse(
        status_code=500,
        content={
            "code": "internal_error",
            "message": (
                "This action did not complete and nothing was saved to the chart. Retrying "
                "is safe. If it keeps happening, report reference "
                f"{request_id or 'unknown'} to your administrator."
            ),
            "request_id": request_id,
        },
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Turned into a 500 here rather than left to Starlette's ServerErrorMiddleware,
            # which is installed *outside* every user middleware. A response produced there
            # skips this middleware and CORSMiddleware both, so the one response where
            # correlation matters most was shipping without the security headers and without
            # `Access-Control-Allow-Origin` — meaning a browser on the dashboard's origin
            # could not read the body at all. The clinician got a bare network failure
            # instead of the reference id the body exists to hand them.
            #
            # Only pre-response-start failures land here: once the app has emitted
            # `http.response.start`, BaseHTTPMiddleware surfaces the exception while
            # streaming the body, after this returns, so this cannot try to replace a
            # response already on the wire (the reasoning SSE stream is the case that
            # matters). `except Exception` also leaves CancelledError alone, so a clinician
            # navigating away mid-request stays a cancellation rather than becoming a logged
            # server error.
            logger.exception("Unhandled exception (request_id=%s)", request_id)
            response = internal_error_response(request_id)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
        apply_security_headers(response)
        return response


class RequestBodyLimitMiddleware:
    """Refuse a request body larger than ``settings.max_request_bytes``, with 413.

    The upload route streams its file and aborts one chunk past 20 MB, so *that* body has been
    bounded for a while. Every other route buffers: FastAPI reads the whole body before Pydantic
    is handed anything to validate, so a body's cost is paid in full before the first rule that
    could reject it runs. A 64 MB JSON body to ``POST /auth/login`` was read into memory and
    then answered 422 — unauthenticated, so no per-account ceiling applies to it, and cheap to
    repeat.

    The only thing standing in front of that was ``client_max_body_size 25m`` in the reference
    ``nginx.conf``, which protects exactly the deployments that put nginx in front of the API
    and left the container port unreachable. That is a deployment property, not a property of
    this application — the same gap as ``TRUSTED_PROXY_HOPS`` — so the ceiling belongs here too.

    Pure ASGI rather than ``BaseHTTPMiddleware`` because the useful check happens *before* the
    body is read: ``BaseHTTPMiddleware`` gives no way to reject one without consuming it first,
    which is the cost being avoided.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @property
    def limit(self) -> int:
        """Read per request rather than captured at construction, like every other ceiling in
        this application (see ``dependencies.limit_for``). The app is built once per process,
        so a captured value could not be changed without rebuilding it."""
        return settings.max_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit
        # Declared length: the common case, and the only one that can be refused for free —
        # nothing has been read yet, so an oversized body costs one response.
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._refuse(scope, send, f"declared {declared}B", limit)
                    return
            except ValueError:
                # A malformed Content-Length is not this middleware's error to answer; the
                # server rejects it on its own terms. Fall through to the counting path.
                pass

        # No declared length: a chunked body has told us nothing to check in advance, so it is
        # counted as it arrives and cut off at the ceiling. Without this the header check is
        # sidestepped by simply omitting the header.
        state = {"exceeded": False, "answered": False}
        await self.app(
            scope,
            self._counted(scope, receive, limit, state),
            self._guarded(scope, send, limit, state),
        )

    def _counted(self, scope: Scope, receive: Receive, limit: int, state: dict) -> Receive:
        """Wrap ``receive`` so an oversized chunked body stops accumulating at the ceiling.

        Past the limit the stream is cut — the application is handed an end-of-body rather than
        the rest of the bytes — and the request is flagged. That bounds the memory, which is the
        whole point; :meth:`_guarded` is what turns the flag into the 413.

        Cutting rather than raising because an exception thrown here does not survive the trip
        out. FastAPI wraps body parsing in ``except Exception`` and re-raises it as a flat 400
        ("There was an error parsing the body"), so a :class:`RequestTooLargeError` raised from
        a receive channel reached the client as a 400 with no code a client could match on.
        """
        total = 0

        async def wrapped() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] != "http.request":
                return message
            total += len(message.get("body", b""))
            if total <= limit:
                return message
            state["exceeded"] = True
            logger.warning(
                "Refused an oversized request body on %s (streamed past %sB, limit %sB)",
                scope.get("path", "?"),
                total,
                limit,
            )
            return {"type": "http.request", "body": b"", "more_body": False}

        return wrapped

    def _guarded(self, scope: Scope, send: Send, limit: int, state: dict) -> Send:
        """Wrap ``send`` so a request whose body was cut short answers 413, not whatever the
        truncated body happened to parse as.

        The application sees a body that stops early and will say something about it — usually
        a 422, sometimes a 400. That answer describes a request the client did not send, so it
        is discarded and replaced. Substitution is safe here because a request body is read
        before a handler produces anything: by the time the first response message arrives the
        flag is already set.
        """

        async def wrapped(message: Message) -> None:
            if not state["exceeded"]:
                await send(message)
                return
            if state["answered"]:
                # The application's own response messages, now superseded.
                return
            state["answered"] = True
            await self._send_refusal(
                scope, send, f"streamed body exceeded limit {limit}B and was cut"
            )

        return wrapped

    async def _refuse(self, scope: Scope, send: Send, detail: str, limit: int) -> None:
        """Answer 413 without invoking the application at all."""
        logger.warning(
            "Refused an oversized request body on %s (%s, limit %sB)",
            scope.get("path", "?"),
            detail,
            limit,
        )
        await self._send_refusal(scope, send, detail)

    async def _send_refusal(self, scope: Scope, send: Send, detail: str) -> None:
        """Write the 413 itself, in the same ``{code, message}`` shape as every other error."""
        error = RequestTooLargeError(detail=detail)
        response = JSONResponse(
            status_code=error.status_code,
            content={"code": error.code, "message": error.message},
        )
        apply_security_headers(response)
        await response(scope, receive_noop, send)


async def receive_noop() -> Message:
    """A receive channel for a response sent without reading the request.

    Starlette's ``Response.__call__`` takes a receive it does not use on a non-streaming body.
    It must still be awaitable, and it must never return: nothing will ask it for a message.
    """
    return {"type": "http.disconnect"}
