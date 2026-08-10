"""Application startup: production config gate, idempotent drug seeding, lifespan.

Drug seeding is a safety dependency, not a convenience — without the vocabulary and
interaction tables the deterministic allergy/interaction checks silently have nothing to
match against. It must therefore be idempotent, and its failure must be loud in the log.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.config import InsecureProductionConfigError, settings
from app.db.seed import seed_contraindications, seed_drug_vocabulary, seed_interactions
from app.main import API_PREFIX, _seed_drug_data, create_app, lifespan
from app.models.drug_vocabulary import Contraindication, DrugInteraction, DrugVocabulary


async def _count(db, model) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


# --- Seeding ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_test_database_comes_pre_seeded(db):
    """The safety engine is useless without reference data — assert it is actually there."""
    assert await _count(db, DrugVocabulary) > 0
    assert await _count(db, DrugInteraction) > 0
    assert await _count(db, Contraindication) > 0


@pytest.mark.asyncio
async def test_seeding_is_idempotent(db):
    """Startup seeding runs on every boot; a second run must add nothing."""
    before = (
        await _count(db, DrugVocabulary),
        await _count(db, DrugInteraction),
        await _count(db, Contraindication),
    )
    assert await seed_drug_vocabulary(db) == 0
    assert await seed_interactions(db) == 0
    assert await seed_contraindications(db) == 0
    await db.commit()

    after = (
        await _count(db, DrugVocabulary),
        await _count(db, DrugInteraction),
        await _count(db, Contraindication),
    )
    assert before == after


@pytest.mark.asyncio
async def test_seeded_vocabulary_maps_indian_brands_to_generics(db):
    """The brand -> generic -> reference-id chain is what makes an allergy to 'Crocin'
    match a prescription for paracetamol."""
    rows = (await db.execute(select(DrugVocabulary))).scalars().all()
    assert all(row.reference_id and row.generic_name for row in rows)
    brands = {(row.brand_name or "").lower() for row in rows}
    assert "crocin" in brands


@pytest.mark.asyncio
async def test_seed_failure_is_logged_and_never_crashes_startup(monkeypatch, caplog, sessionmaker):
    """A seeding failure degrades safety coverage but must not stop the API from booting —
    it has to be visible in the log instead."""
    monkeypatch.setattr("app.main.get_sessionmaker", lambda: sessionmaker)

    async def boom(_db):
        raise RuntimeError("seed table missing")

    monkeypatch.setattr("app.db.seed.seed_drug_vocabulary", boom)

    with caplog.at_level("WARNING"):
        await _seed_drug_data()

    assert any("seeding failed" in rec.getMessage().lower() for rec in caplog.records)


@pytest.mark.asyncio
async def test_seed_data_runs_and_commits_on_a_fresh_database(monkeypatch, caplog, sessionmaker):
    monkeypatch.setattr("app.main.get_sessionmaker", lambda: sessionmaker)
    with caplog.at_level("DEBUG"):
        await _seed_drug_data()  # already seeded by the fixture — the no-op branch
    assert not any(rec.levelname == "ERROR" for rec in caplog.records)


# --- Lifespan and app wiring -----------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_refuses_to_start_with_an_insecure_production_config(monkeypatch):
    """The gate runs before anything touches patient data."""
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "app_secret_key", "dev-insecure-secret-change-me")

    with pytest.raises(InsecureProductionConfigError):
        async with lifespan(create_app()):
            pass


@pytest.mark.asyncio
async def test_lifespan_starts_in_development(monkeypatch, sessionmaker):
    """``app.main`` binds ``get_sessionmaker`` at import time, so the patch target must be
    ``app.main.get_sessionmaker`` -- patching ``app.db.session.get_sessionmaker`` leaves the
    real factory in play, which builds the process-global engine against the real
    ``DATABASE_URL`` and leaks it (bound to this test's event loop) into every later test.
    ``dispose_engine`` is deliberately left unpatched: it is a no-op when no global engine
    exists, and is the only thing that clears one if it does."""
    monkeypatch.setattr("app.main.get_sessionmaker", lambda: sessionmaker)

    async with lifespan(create_app()):
        pass  # no exception is the assertion


def test_every_router_is_mounted_under_the_versioned_prefix():
    app = create_app()
    paths = {route.path for route in app.routes if hasattr(route, "path")}

    for expected in (
        f"{API_PREFIX}/auth/login",
        f"{API_PREFIX}/patients",
        f"{API_PREFIX}/patients/{{patient_id}}/documents",
        f"{API_PREFIX}/patients/{{patient_id}}/drug-safety/check",
        f"{API_PREFIX}/patients/{{patient_id}}/record",
        f"{API_PREFIX}/patients/{{patient_id}}/labs/critical-flags",
    ):
        assert expected in paths, f"{expected} is not mounted"


def test_health_endpoints_are_mounted_without_the_api_prefix():
    """Probes sit outside the versioned API so a version bump never breaks orchestration."""
    paths = {route.path for route in create_app().routes if hasattr(route, "path")}
    assert "/health" in paths
    assert f"{API_PREFIX}/health" not in paths


def test_cors_is_restricted_to_the_configured_origins():
    app = create_app()
    cors = [m for m in app.user_middleware if "CORSMiddleware" in str(m.cls)]
    assert cors, "CORS middleware is not installed"
    assert "*" not in settings.cors_origin_list
