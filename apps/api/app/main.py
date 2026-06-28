"""FastAPI application factory, router registration, exception handlers, lifespan."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
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

    return app


app = create_app()
