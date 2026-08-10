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
            "guideline RAG with cited management (P3), validation/regulatory/pilot (P4)."
        ),
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
    async def aether_error_handler(_request: Request, exc: AetherError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
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
                        "message": "The requested resource was not found.",
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
                "message": "An unexpected error occurred. Please try again.",
            },
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
