"""Cross-cutting middleware: request-id correlation and security headers (P1-11b)."""

from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings

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
