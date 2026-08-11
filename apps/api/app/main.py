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

from app.config import assert_production_config, settings
from app.db.session import dispose_engine, get_sessionmaker
from app.exceptions import AetherError
from app.middleware import RequestContextMiddleware
from app.openapi import TAGS_METADATA
from app.routers import (
    audit,
    auth,
    documents,
    guidelines,
    health,
    pathways,
    patients,
    reasoning,
    records,
    safety,
    validation,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

# Keys of a Pydantic error dict that are safe to return to the client. Everything outside this
# set is dropped by _safe_validation_errors.
_SAFE_ERROR_KEYS = ("type", "loc", "msg")


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
    """Seed drug vocabulary, interactions, and contraindications if empty.

    Idempotent — the underlying seed functions skip rows that already exist.
    Runs only the deterministic drug-safety data (not guidelines, which require
    Qdrant and can be slow).  Failures are logged but never crash the app.
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
    await dispose_engine()


def create_app() -> FastAPI:
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
            "**Auth** is a bearer access token from `POST /api/v1/auth/login`. A record "
            "belonging to another account returns 404, not 403, so the API never confirms that "
            "a chart it will not show you exists."
        ),
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )

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
    async def aether_error_handler(request: Request, exc: AetherError) -> JSONResponse:
        """Serialize a domain error, keeping its internal cause in the log only.

        ``exc.detail`` exists so the clinician-facing ``message`` can stay actionable ("sign in
        again") while operators still get the mechanism ("token type 'refresh', expected
        'access'"), correlated by request id. It is deliberately never serialized: it names
        internals, and for a 401 storm the distinction between causes is exactly what an
        attacker would like echoed back.
        """
        if exc.detail:
            logger.info(
                "%s (%s): %s [request_id=%s]",
                type(exc).__name__,
                exc.code,
                exc.detail,
                getattr(request.state, "request_id", None),
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
            # Almost always None. A 429 carries Retry-After here, because the status code alone
            # does not tell a client how long to back off. See AetherError.headers.
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
        """Last-resort handler for anything not already an AetherError/RequestValidationError.

        Without this, Starlette's default 500 path is plain text (inconsistent with every
        other error response's {code, message} JSON shape) and the exception is only ever
        visible in process stderr with no request correlation. This logs it against the
        request id (already on every response via RequestContextMiddleware) and never leaks
        internal exception text to the client -- an unhandled exception can easily be
        carrying patient data in its message/args (e.g. a DB constraint error), which must
        never reach the response body.
        """
        request_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled exception (request_id=%s)", request_id)
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                # The request id is the only thing that connects the clinician's report to the
                # log line above, so it goes in the body and not just the X-Request-Id header —
                # nobody opens devtools mid-consultation. "Try again" is kept but qualified:
                # get_db rolls the request's transaction back, so a retry is safe and no
                # half-written clinical record is left behind, which is the clinician's real
                # question here.
                "message": (
                    "This action did not complete and nothing was saved to the chart. Retrying "
                    "is safe. If it keeps happening, report reference "
                    f"{request_id or 'unknown'} to your administrator."
                ),
                "request_id": request_id,
            },
            # Set explicitly: RequestContextMiddleware adds this header on the way out, but an
            # unhandled exception is caught by Starlette's ServerErrorMiddleware, which sits
            # *outside* it — so the one response where correlation matters most was the only
            # one shipping without the header.
            headers={"X-Request-Id": request_id} if request_id else None,
        )

    app.include_router(health.router)
    for module in (
        auth,
        patients,
        documents,
        records,
        safety,
        audit,
        reasoning,
        guidelines,
        pathways,
        validation,
    ):
        app.include_router(module.router, prefix=API_PREFIX)
    app.include_router(records.labs_router, prefix=API_PREFIX)

    return app


app = create_app()
