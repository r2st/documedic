"""FastAPI application factory, router registration, exception handlers, lifespan."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agents.llm import close_provider_clients
from app.agents.util import shutdown_llm_executor
from app.config import assert_production_config, settings
from app.core.logging_config import configure_logging
from app.db.session import dispose_engine, get_sessionmaker
from app.exceptions import AetherError
from app.middleware import (
    RequestBodyLimitMiddleware,
    RequestContextMiddleware,
    apply_security_headers,
    internal_error_response,
)
from app.openapi import TAGS_METADATA
from app.routers import (
    appointments,
    audit,
    auth,
    dashboard,
    discharge,
    documents,
    encounter_participants,
    encounters,
    guidelines,
    handoffs,
    health,
    note_search,
    pathways,
    patient_portal,
    patients,
    protocols,
    reasoning,
    records,
    safety,
    summary,
    validation,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

# Keys of a Pydantic error dict that are safe to return to the client. Everything outside this
# set is dropped by _safe_validation_errors.
_SAFE_ERROR_KEYS = ("type", "loc", "msg")

# Copy for the HTTP errors Starlette's *router* raises, before any application code runs: an
# address that matches no route, and a method a route does not accept. No application code
# raises HTTPException — domain failures are AetherError — so this table is the whole set in
# practice, with a generic fallback for anything a future dependency introduces.
#
# The message is written here rather than taken from ``exc.detail`` because that is Starlette's
# developer prose ("Not Found", "Method Not Allowed"), which tells the clinician reading a toast
# mid-consultation nothing about what to do next.
_ROUTER_ERROR_COPY: dict[int, tuple[str, str]] = {
    404: (
        "not_found",
        "That address is not part of this application. The link may be out of date — open the "
        "record from the patient list instead.",
    ),
    405: (
        "method_not_allowed",
        "That request could not be carried out as sent, so nothing was changed. Reload the page "
        "and try again.",
    ),
}
_ROUTER_ERROR_FALLBACK = (
    "error",
    "That request could not be completed, so nothing was changed. Reload the page and try again.",
)


def _safe_validation_errors(exc: RequestValidationError) -> list[dict]:
    """Project Pydantic's validation errors down to type/loc/msg, dropping the rejected input.

    Pydantic puts the value it rejected in ``error["input"]``, and FastAPI's default handler
    returns that verbatim. For a *field-level* failure that is one field's value; but for a
    ``missing`` error -- the most common kind -- the input is the **entire submitted body**.
    A patient create that forgets ``full_name`` therefore echoed back phone, address_text and
    the free-text ``notes`` field (clinical history) in the 422 response, where it reaches the
    client, any frontend error reporting, and every proxy access log on the way.

    Those columns are encrypted at rest precisely because they are DPDP-sensitive, so handing
    them back in plaintext over an error path undoes that. Nothing needs them: ``loc`` names
    the offending field and ``msg`` states the constraint, which is all a client can act on.

    ``ctx`` goes too. It carries constraint metadata (``ctx["expected"]``, already repeated in
    ``msg``) but for a validator that raises a bare ValueError it also holds the raw exception
    instance, whose string form is author-controlled and could quote the value.
    """
    return [{key: error[key] for key in _SAFE_ERROR_KEYS if key in error} for error in exc.errors()]


async def _seed_drug_data() -> None:
    """Reconcile drug vocabulary, interactions, and contraindications against the curated files.

    Runs only the deterministic drug-safety data (not guidelines, which require Qdrant and can
    be slow). Re-running changes nothing on a database already in step; where it differs, the
    file wins — including corrections to existing rows, which is what makes a curated fix
    reachable without dropping tables. See ``app.db.seed``.

    Failures are logged but never crash the app, and that is the safe direction *here* even
    though the seeder refuses an empty or dangling corpus outright: this path runs against a
    database that already holds the previous corpus, so declining to apply a bad one leaves the
    old rules serving. The deploy path (``Dockerfile``) chains the seeder before uvicorn with
    ``&&``, which is where a refusal is meant to stop the release.
    """
    from app.db.seed import (
        seed_contraindications,
        seed_drug_vocabulary,
        seed_interactions,
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db:
        try:
            counts = {
                "drug_vocabulary": await seed_drug_vocabulary(db),
                "drug_interactions": await seed_interactions(db),
                "contraindications": await seed_contraindications(db),
            }
            await db.commit()
            if any(counts.values()):
                logger.info("Drug data seeded on startup: %s", counts)
            else:
                logger.debug("Drug data already present — nothing to seed.")
        except Exception:
            await db.rollback()
            logger.warning(
                "Drug data seeding failed on startup — safety checks may be "
                "incomplete until seed data is loaded manually.",
                exc_info=True,
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Before anything touches patient data: refuse to serve production traffic with a
    # development-grade secret, debug mode on, or a wildcard CORS policy.
    assert_production_config()
    await _seed_drug_data()
    yield
    # Shutdown, in the order the dependencies run: the LLM side first, then the database.
    #
    # Uvicorn has already let the in-flight requests finish by the time this runs, so what is
    # left in the pool is work nothing is waiting on any more. Cancelling it stops a worker on
    # its way out from opening new provider sockets, and stops the interpreter being held at
    # the atexit join by calls that were only ever queued. See ``shutdown_llm_executor`` for
    # what this can and cannot interrupt.
    shutdown_llm_executor()
    # After the executor, so no worker thread is part-way through a request on a client while
    # it is being closed. Releases the pooled keep-alive connections to each provider.
    close_provider_clients()
    await dispose_engine()


def create_app() -> FastAPI:
    # First, before anything here can want to log. Nothing else in this process configures
    # logging: uvicorn sets up its own three loggers and leaves the root one at WARNING with no
    # handler, so without this every `logger.info` under `app.*` is discarded and everything
    # louder falls through to `logging.lastResort` — a bare message with no timestamp, no level
    # and no correlation id. See app.core.logging_config.
    configure_logging()
    app = FastAPI(
        title="Aether Clinician API",
        version="0.4.0",
        description=(
            "Clinician-facing diagnostic & management decision-support: patient graph + "
            "deterministic drug safety (P1), 8-agent reasoning engine + Reasoning Theatre (P2), "
            "guideline RAG with cited management (P3), validation/regulatory/pilot (P4).\n\n"
            "**The clinician decides.** Nothing here prescribes. Every clinical output is a "
            "suggestion carrying an autonomy tier, traceable to patient data or a cited "
            "guideline, and it has passed the Verifier agent — there is no route around that "
            "and no flag that skips it.\n\n"
            "**Errors** are `{code, message}` (see the `ErrorResponse` schema). Match on "
            "`code`; `message` is clinician-facing prose and gets rewritten. Request-validation "
            "failures are the one exception and return FastAPI's `{detail: [...]}` — with the "
            "rejected input stripped out, since for a missing-field error that input is the "
            "whole submitted body.\n\n"
            "**Correlation.** Every response carries `X-Request-Id`, echoing the one you sent "
            "if you sent one. A 500 repeats it in the body as `request_id`, because that is the "
            "reference a clinician reads back to an administrator without opening devtools.\n\n"
            "**Auth** is a bearer access token from `POST /api/v1/auth/login`. A record "
            "belonging to another account returns 404, not 403, so the API never confirms that "
            "a chart it will not show you exists."
        ),
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )

    # Added first, so it ends up *innermost* of the three (Starlette builds the stack so the
    # most recently added is outermost). That is deliberate: refusing an oversized body still
    # happens before the application reads a byte of it, but the 413 travels back out through
    # RequestContextMiddleware and CORSMiddleware, so it carries the request id and the
    # security and CORS headers that every other error response carries.
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
    )

    @app.exception_handler(AetherError)
    async def aether_error_handler(_request: Request, exc: AetherError) -> JSONResponse:
        """Serialize a domain error, keeping its internal cause in the log only.

        ``exc.detail`` exists so the clinician-facing ``message`` can stay actionable ("sign in
        again") while operators still get the mechanism ("token type 'refresh', expected
        'access'"), correlated by request id. It is deliberately never serialized: it names
        internals, and for a 401 storm the distinction between causes is exactly what an
        attacker would like echoed back.
        """
        if exc.detail:
            # INFO, which until app.core.logging_config existed meant "not emitted at all" —
            # the root logger sat at WARNING with no handler, so the entire reason ``detail``
            # is separated from ``message`` produced no output in the deployed process.
            # The request id is no longer interpolated here: every record carries it now.
            logger.info("%s (%s): %s", type(exc).__name__, exc.code, exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
            # Almost always None. A 429 carries Retry-After here, because the status code alone
            # does not tell a client how long to back off. See AetherError.headers.
            headers=exc.headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def router_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Render Starlette's own HTTP errors in the same ``{code, message}`` shape.

        Without this, the two errors the router raises before reaching any handler — 404 for an
        unmatched address, 405 for a method a route does not accept — were the only responses in
        the API answering ``{"detail": "Not Found"}``. A client that matches on ``code``, which
        is what the OpenAPI description tells it to do, had nothing to match, and the clinician
        got Starlette's developer prose.

        ``exc.headers`` is carried through because the 405 arrives with ``Allow`` on it, and a
        405 without ``Allow`` is not a 405.
        """
        code, message = _ROUTER_ERROR_COPY.get(exc.status_code, _ROUTER_ERROR_FALLBACK)
        logger.info("HTTP %s (%s): %s", exc.status_code, code, exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": code, "message": message},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Convert UUID path-parameter validation errors to 404 Not Found.

        FastAPI returns 422 when a path parameter like ``patient_id`` cannot be
        parsed as ``uuid.UUID`` (e.g. the client sends ``/patients/3``).  For
        resource-lookup endpoints this is confusing — the user expects a 404.
        All other validation errors (query params, request body) keep the
        standard 422 response.
        """
        for error in exc.errors():
            loc = error.get("loc", ())
            err_type = error.get("type", "")
            if "path" in loc and "uuid" in err_type:
                return JSONResponse(
                    status_code=404,
                    content={
                        "code": "not_found",
                        # The identifier in the URL is not even the shape of a record id, so this
                        # is a truncated or hand-edited link rather than a deleted record —
                        # saying so stops the clinician hunting for a chart that never existed.
                        "message": (
                            "That link does not point at a valid record — it may have been "
                            "truncated when it was copied. Open the record from the patient "
                            "list instead."
                        ),
                    },
                )
        # Default 422 for non-UUID validation errors (body, query, etc.), with the rejected
        # input stripped out -- see _safe_validation_errors.
        return JSONResponse(
            status_code=422, content={"detail": jsonable_encoder(_safe_validation_errors(exc))}
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Backstop for a failure that ``RequestContextMiddleware`` could not catch.

        Almost every unhandled exception is turned into this same 500 by the middleware,
        which sits inside CORSMiddleware and so produces a response the browser can actually
        read. What is left for here is the narrow band the middleware cannot see: a failure
        raised by CORSMiddleware itself, or by the middleware's own dispatch. Those still
        have to answer in the ``{code, message}`` JSON shape rather than Starlette's default
        plain text, and still have to carry the security headers — nothing about a server
        error makes ``nosniff`` optional — so both are applied here by hand.

        ``request.state.request_id`` is usually set even on this path, because the middleware
        assigns it before doing anything that can fail.
        """
        request_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled exception")
        response = internal_error_response(request_id)
        if request_id:
            response.headers["X-Request-Id"] = request_id
        apply_security_headers(response)
        return response

    app.include_router(health.router)
    for module in (
        auth,
        patients,
        documents,
        encounters,
        records,
        safety,
        summary,
        audit,
        reasoning,
        guidelines,
        pathways,
        handoffs,
        discharge,
        appointments,
        protocols,
        note_search,
        encounter_participants,
        patient_portal,
        dashboard,
        validation,
    ):
        app.include_router(module.router, prefix=API_PREFIX)
    app.include_router(records.labs_router, prefix=API_PREFIX)
    app.include_router(records.export_router, prefix=API_PREFIX)
    app.include_router(records.medications_router, prefix=API_PREFIX)
    app.include_router(records.critical_labs_router, prefix=API_PREFIX)
    app.include_router(appointments.diary_router, prefix=API_PREFIX)
    app.include_router(protocols.catalogue_router, prefix=API_PREFIX)
    app.include_router(encounter_participants.shared_router, prefix=API_PREFIX)
    app.include_router(patient_portal.portal_router, prefix=API_PREFIX)

    return app


app = create_app()
