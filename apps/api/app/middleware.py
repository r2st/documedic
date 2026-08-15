"""Cross-cutting middleware: request-id correlation and security headers (P1-11b)."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.core.request_context import bind_request_id, reset_request_id
from app.exceptions import AetherError, RequestTooDeeplyNestedError, RequestTooLargeError

logger = logging.getLogger(__name__)

# The Content-Security-Policy for an API that serves JSON and clinician-uploaded scans, and
# never serves a page of its own. Every directive is set to the most restrictive value that is
# still true of this application, because "no page here needs it" is exactly the argument that
# stops being available once someone adds one.
#
# `default-src 'none'` rather than `'self'`: this origin has no scripts, styles, fonts or
# frames to load, and `'self'` silently permitted all of them. What it must still allow is the
# one thing it does serve — an uploaded image, rendered inline by the browser — so `img-src`
# opts that back in on its own.
#
# `script-src 'none'` and `object-src 'none'` are the two that matter for the download route.
# A response body from this origin that a browser is willing to treat as a document runs with
# this origin's privileges, and this origin is where a bearer token is presented; the PDF path
# is already forced to `attachment` for that reason (see routers/documents.py) and these say
# the same thing declaratively, for the cases nobody thought of.
#
# `base-uri` and `form-action` close the two redirect-ish sinks a CSP that only sets
# `default-src` leaves open. `frame-ancestors 'none'` duplicates X-Frame-Options for browsers
# that honour both; the older header stays because some proxies and scanners only read it.
_CSP = (
    "default-src 'none'; "
    "img-src 'self' data:; "
    "script-src 'none'; "
    "object-src 'none'; "
    "style-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

# Every powerful browser feature this API could ever be asked for, turned off. An API response
# is not a page and will not ask for a camera — which is the point: if a response from this
# origin is ever rendered as a document (an inline scan, an error page, a future export), it
# starts with no access to the microphone, the camera, geolocation or the clipboard rather
# than with whatever the browser's defaults happen to be that year.
#
# `interest-cohort=()` is retained deliberately even though FLoC is gone: it costs nothing and
# is the only opt-out some intermediary caches still recognise. Under the DPDP Act this API's
# responses carry patient data and must not become an input to anyone's ad profiling.
_PERMISSIONS_POLICY = (
    "accelerometer=(), ambient-light-sensor=(), autoplay=(), battery=(), camera=(), "
    "display-capture=(), document-domain=(), encrypted-media=(), fullscreen=(), "
    "geolocation=(), gyroscope=(), hid=(), idle-detection=(), local-fonts=(), "
    "magnetometer=(), microphone=(), midi=(), payment=(), picture-in-picture=(), "
    "publickey-credentials-get=(), screen-wake-lock=(), serial=(), usb=(), "
    "xr-spatial-tracking=(), interest-cohort=()"
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": _CSP,
    "Permissions-Policy": _PERMISSIONS_POLICY,
    # Blocks this API's responses from being loaded as a cross-origin *subresource* — an
    # `<img src>` on an attacker's page pointed at a scan, a `<script src>` pointed at a JSON
    # body. It deliberately does not affect the frontend, which reaches every endpoint through
    # `fetch` in CORS mode: Cross-Origin-Resource-Policy is only consulted for `no-cors`
    # requests, and a CORS request is governed by the Access-Control headers instead. So this
    # closes the embedding paths without touching the one path that is meant to work.
    "Cross-Origin-Resource-Policy": "same-origin",
    # Severs the `window.opener` link if a response from this origin is ever opened in a new
    # browsing context, so the opener cannot reach into it or be navigated by it.
    "Cross-Origin-Opener-Policy": "same-origin",
}

# Two years, subdomains included. Deliberately *without* `preload`: that directive is a
# request to be baked into browsers' shipped preload lists, which is close to irreversible and
# commits every present and future subdomain of the deployment to HTTPS. That is an operator's
# decision about a domain, not the application's to make on their behalf.
#
# Set only in production, so it is never applied to a developer's `http://localhost` — a
# browser that pins HSTS for localhost pins it for every other project on that machine, and
# there is no way to un-send it.
HSTS_VALUE = "max-age=63072000; includeSubDomains"

# What an inbound X-Request-Id may look like to be adopted as this request's correlation id.
#
# The header is client-controlled and lands in three places at once: echoed back in the response
# header, interpolated into the 500 body's clinician-facing prose, and written into every log
# line the request produces. Unfiltered, all three took whatever was sent — 4 KB of it, or an
# ANSI escape. The last is the one that bites: the request id is written into logs an operator
# reads in a terminal while an incident is running, and `\x1b[2J` clears their screen. Nothing
# needed guarding against because "the framework strips it" — h11 rejects CR and LF in a header
# value and nothing else, so escapes, control characters and kilobyte values all arrive intact.
#
# The character set is every punctuation mark correlation ids are actually built out of — UUIDs
# and W3C `traceparent` (hex and `-`), base64url (`-`, `_`, `=`), and the separators gateways
# join segments with. It excludes whitespace, quotes, angle brackets and anything below 0x20,
# none of which appear in a real id and all of which exist here only to be interpreted by
# whatever reads the log.
_SAFE_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._~:@+/=-]{1,128}\Z")


def resolve_request_id(supplied: str | None) -> str:
    """This request's correlation id: the client's, if it is one, else a fresh one.

    A rejected value is replaced rather than refused with a 400. The id is a diagnostic aid, not
    a credential or an input the request means anything without, so a client that sends a
    malformed one should still get its answer — it simply does not get to choose how it is
    filed. Substitution is logged, without the value: writing the rejected id into the log to
    explain that it was unsafe to write into the log defeats the purpose.

    Adopting the client's id at all is deliberate and unchanged — it is how a request is
    followed across a proxy and the frontend — and it does mean a caller can make two requests
    share an id. That is inherent to client-supplied correlation and is not what this guards.
    """
    if supplied is not None and _SAFE_REQUEST_ID.match(supplied):
        return supplied
    if supplied:
        logger.info(
            "Ignored a malformed X-Request-Id (%d chars) and generated one instead.",
            len(supplied),
        )
    return uuid.uuid4().hex


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
        request_id = resolve_request_id(request.headers.get("X-Request-Id"))
        request.state.request_id = request_id
        # Also into the context, which is what carries it anywhere a ``Request`` object does
        # not reach: the services, the agent nodes, the extraction pipeline, the SSE worker
        # task, and the threads ``asyncio.to_thread`` and ``call_llm`` hand work to. Without
        # this the id existed only on ``request.state``, so the only log lines carrying it were
        # the three in ``app.main`` that interpolate it by hand — never the line describing the
        # failure itself. See app.core.request_context.
        token = bind_request_id(request_id)
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
            logger.exception("Unhandled exception")
            response = internal_error_response(request_id)
        finally:
            # Unbound before this coroutine returns, so the id cannot outlive its request in a
            # context that gets reused. Safe for the streaming routes: ``BaseHTTPMiddleware``
            # runs the application in a *child* task, which took its own copy of the context
            # at the moment it was started — after the bind above — so the SSE generator keeps
            # the id for as long as it is producing events, whatever happens here.
            reset_request_id(token)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
        apply_security_headers(response)
        return response


# The routes that legitimately carry a body measured in megabytes. In practice this is the one
# document-upload route; it is written as a pattern rather than a literal because the path
# carries the patient id, and as a set rather than a flag because a second upload route (a
# batch ingest, a bulk guideline import) must be an explicit decision to widen the ceiling
# rather than something that inherits it by being new.
#
# Deliberately matched on the raw path, before routing. This middleware runs outside the
# router — that is what lets it refuse a body without the body being read — so there is no
# matched route to consult, and a path that resolves to nothing gets the small ceiling, which
# is the safe direction: an unmatched address answers 404 and never needed a large body.
_LARGE_BODY_ROUTES = (re.compile(r"\A/api/v1/patients/[^/]+/documents/?\Z"),)

# Content types whose body FastAPI will hand to a recursive JSON parser. `+json` catches the
# structured-suffix types (`application/merge-patch+json` and friends) that Starlette's
# `Request.json()` is equally willing to parse.
_JSON_CONTENT_TYPE = re.compile(r"\Aapplication/(?:[\w.+-]+\+)?json\b", re.IGNORECASE)


def _is_large_body_route(path: str) -> bool:
    return any(pattern.match(path) for pattern in _LARGE_BODY_ROUTES)


class _DepthScanner:
    """Streaming nesting-depth counter for a JSON body, over raw bytes.

    Counts ``[`` and ``{`` against ``]`` and ``}``, skipping anything inside a string literal
    so that a drug name of ``"{{{{"`` is text and not depth. It is not a parser and does not
    try to be: it never allocates the document, it reads each byte once, and it is wrong only
    for input that is not valid JSON — which the real parser rejects anyway.

    Feeding it a chunk at a time is the point. The depth of a body is known while the body is
    still arriving, so an abusive one is cut off part-way rather than measured after the whole
    thing has been buffered and handed to ``json.loads``.
    """

    __slots__ = ("depth", "exceeded", "_in_string", "_escaped", "_limit")

    _OPENERS = frozenset(b"[{")
    _CLOSERS = frozenset(b"]}")

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self.depth = 0
        self.exceeded = False
        self._in_string = False
        self._escaped = False

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif byte == 0x5C:  # backslash
                    self._escaped = True
                elif byte == 0x22:  # closing quote
                    self._in_string = False
                continue
            if byte == 0x22:
                self._in_string = True
            elif byte in self._OPENERS:
                self.depth += 1
                if self.depth > self._limit:
                    self.exceeded = True
                    return
            elif byte in self._CLOSERS:
                # Clamped at zero so a body with unbalanced closers cannot drive the counter
                # negative and buy itself extra depth further on.
                self.depth = max(0, self.depth - 1)


class RequestBodyLimitMiddleware:
    """Bound what a request body may cost before the application reads a byte of it.

    Three ceilings, all refused up here rather than after parsing:

    **Size, on an upload route** — ``settings.max_request_bytes`` (24 MB), sized to fit a 20 MB
    scan plus its multipart framing. The upload route also streams and aborts one chunk past
    20 MB on its own.

    **Size, on every other route** — ``settings.max_json_request_bytes`` (1 MB). The upload
    ceiling was previously applied to all of them, which is not a ceiling at all for
    ``POST /auth/login``: 24 MB of JSON was buffered whole and parsed before the first Pydantic
    rule could answer 422, unauthenticated, with no per-account limit in front of it and
    nothing to stop it being repeated. Nothing but the upload route has a legitimate body
    within two orders of magnitude of a megabyte.

    **Nesting depth** — ``settings.max_json_depth`` (32), on JSON bodies. JSON is parsed
    recursively, and the only thing bounding ``[[[[…`` was CPython's recursion limit. That is
    an implementation detail of the interpreter rather than a statement about what this API
    accepts, and it surfaces as FastAPI's opaque flat 400 ("There was an error parsing the
    body") that a client cannot match on and an operator cannot distinguish from a syntax
    error. The deepest body this API has a use for nests three levels.

    The only thing that stood in front of any of this was ``client_max_body_size 25m`` in the
    reference ``nginx.conf``, which protects exactly the deployments that put nginx in front of
    the API and leave the container port unreachable. That is a property of a deployment, not
    of this application — the same gap as ``TRUSTED_PROXY_HOPS`` — so the ceilings belong here.

    Pure ASGI rather than ``BaseHTTPMiddleware`` because the useful check happens *before* the
    body is read: ``BaseHTTPMiddleware`` gives no way to reject one without consuming it first,
    which is the cost being avoided.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def limit_for(self, scope: Scope) -> int:
        """The size ceiling for this request. Read per request rather than captured at
        construction, like every other ceiling in this application (see
        ``dependencies.limit_for``): the app is built once per process, so a captured value
        could not be changed without rebuilding it."""
        if _is_large_body_route(str(scope.get("path", ""))):
            return settings.max_request_bytes
        return settings.max_json_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(scope)
        headers = Headers(scope=scope)
        # Declared length: the common case, and the only one that can be refused for free —
        # nothing has been read yet, so an oversized body costs one response.
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._refuse(scope, send, f"declared {declared}B", limit)
                    return
            except ValueError:
                # A malformed Content-Length is not this middleware's error to answer; the
                # server rejects it on its own terms. Fall through to the counting path.
                pass

        # The wrappers are installed either way, not only for a chunked body. A declared length
        # that fits still says nothing about nesting depth, and a client that sends no
        # Content-Length has declared nothing at all — without the counting path the size check
        # is sidestepped by simply omitting the header.
        scanner = (
            _DepthScanner(settings.max_json_depth)
            if _JSON_CONTENT_TYPE.match(headers.get("content-type", ""))
            else None
        )
        state: dict = {"refusal": None, "answered": False}
        await self.app(
            scope,
            self._counted(scope, receive, limit, scanner, state),
            self._guarded(scope, send, state),
        )

    def _counted(
        self,
        scope: Scope,
        receive: Receive,
        limit: int,
        scanner: _DepthScanner | None,
        state: dict,
    ) -> Receive:
        """Wrap ``receive`` so an abusive body stops accumulating at the ceiling.

        Past a limit the stream is cut — the application is handed an end-of-body rather than
        the rest of the bytes — and the reason is recorded on ``state``. That bounds the
        memory and the parse, which is the whole point; :meth:`_guarded` is what turns the
        recorded reason into the response.

        Cutting rather than raising because an exception thrown here does not survive the trip
        out. FastAPI wraps body parsing in ``except Exception`` and re-raises it as a flat 400
        ("There was an error parsing the body"), so a :class:`RequestTooLargeError` raised from
        a receive channel reached the client as a 400 with no code a client could match on.
        """
        total = 0

        async def wrapped() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] != "http.request" or state["refusal"] is not None:
                return message
            body = message.get("body", b"")
            total += len(body)
            if total > limit:
                logger.warning(
                    "Refused an oversized request body on %s (streamed past %sB, limit %sB)",
                    scope.get("path", "?"),
                    total,
                    limit,
                )
                state["refusal"] = RequestTooLargeError(
                    detail=f"streamed body exceeded limit {limit}B and was cut"
                )
                return _END_OF_BODY
            if scanner is not None:
                scanner.feed(body)
                if scanner.exceeded:
                    logger.warning(
                        "Refused a deeply nested JSON body on %s (past %s levels)",
                        scope.get("path", "?"),
                        settings.max_json_depth,
                    )
                    state["refusal"] = RequestTooDeeplyNestedError(
                        detail=f"JSON nesting exceeded {settings.max_json_depth} levels"
                    )
                    return _END_OF_BODY
            return message

        return wrapped

    def _guarded(self, scope: Scope, send: Send, state: dict) -> Send:
        """Wrap ``send`` so a request whose body was cut short answers the refusal, not
        whatever the truncated body happened to parse as.

        The application sees a body that stops early and will say something about it — usually
        a 422, sometimes a 400. That answer describes a request the client did not send, so it
        is discarded and replaced. Substitution is safe here because a request body is read
        before a handler produces anything: by the time the first response message arrives the
        refusal is already recorded.
        """

        async def wrapped(message: Message) -> None:
            refusal = state["refusal"]
            if refusal is None:
                await send(message)
                return
            if state["answered"]:
                # The application's own response messages, now superseded.
                return
            state["answered"] = True
            await self._send_refusal(scope, send, refusal)

        return wrapped

    async def _refuse(self, scope: Scope, send: Send, detail: str, limit: int) -> None:
        """Answer 413 without invoking the application at all."""
        logger.warning(
            "Refused an oversized request body on %s (%s, limit %sB)",
            scope.get("path", "?"),
            detail,
            limit,
        )
        await self._send_refusal(scope, send, RequestTooLargeError(detail=detail))

    async def _send_refusal(self, scope: Scope, send: Send, error: AetherError) -> None:
        """Write the refusal itself, in the same ``{code, message}`` shape as every other
        error."""
        response = JSONResponse(
            status_code=error.status_code,
            content={"code": error.code, "message": error.message},
        )
        apply_security_headers(response)
        await response(scope, receive_noop, send)


# The message handed to the application in place of the rest of a body that was cut. Shared
# rather than rebuilt per refusal: it is read, never mutated.
_END_OF_BODY: Message = {"type": "http.request", "body": b"", "more_body": False}


async def receive_noop() -> Message:
    """A receive channel for a response sent without reading the request.

    Starlette's ``Response.__call__`` takes a receive it does not use on a non-streaming body.
    It must still be awaitable, and it must never return: nothing will ask it for a message.
    """
    return {"type": "http.disconnect"}
