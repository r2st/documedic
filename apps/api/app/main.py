"""FastAPI application factory, router registration, exception handlers, lifespan."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db.session import dispose_engine, get_sessionmaker
from app.exceptions import AetherError
from app.middleware import RequestContextMiddleware
from app.routers import (
    audit,
    auth,
    documents,
    guidelines,
    health,
    patients,
    reasoning,
    records,
    safety,
    validation,
)

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


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
async def lifespan(app: FastAPI):
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
        # Default 422 for non-UUID validation errors (body, query, etc.). A custom
        # field_validator that raises a bare ValueError (the standard Pydantic v2 idiom) puts
        # the raw exception instance in error["ctx"]["error"], which plain json.dumps (what
        # JSONResponse uses) cannot serialize -- jsonable_encoder coerces it to a string first.
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

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
        validation,
    ):
        app.include_router(module.router, prefix=API_PREFIX)
    app.include_router(records.labs_router, prefix=API_PREFIX)

    return app


app = create_app()
