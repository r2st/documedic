"""Shared test fixtures: isolated in-memory DB, seeded drug data, and an authed client."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key")
os.environ.setdefault("BCRYPT_ROUNDS", "4")  # fast hashing in tests
# Keep the default test suite on the DETERMINISTIC offline fallback (not the simulated demo
# net), so assertions are reproducible. The demo path has its own dedicated tests that opt in.
os.environ.setdefault("LLM_DEMO_FALLBACK", "false")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import session as db_session
from app.db.seed import seed_all
from app.db.session import get_db
from app.main import create_app
from app.models import Base


@pytest.fixture(autouse=True)
def _no_leaked_global_engine():
    """Fail loudly if a test leaves ``app.db.session._engine`` set.

    The module-global engine is created lazily by ``get_sessionmaker()``. An aiosqlite/asyncpg
    engine is bound to the event loop it was created on, so one surviving into a later test --
    which pytest-asyncio runs on a *fresh* loop -- deadlocks the whole suite rather than
    failing. This turns that silent hang into a named test failure at the point of the leak.
    """
    yield
    leaked = db_session._engine is not None
    db_session._engine = None
    db_session._sessionmaker = None
    assert not leaked, (
        "test leaked the process-global engine (app.db.session._engine). Patch "
        "'app.main.get_sessionmaker' -- not 'app.db.session.get_sessionmaker' -- and let the "
        "real dispose_engine() run."
    )


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Seed reference drug data once per test DB.
    sm = async_sessionmaker(eng, expire_on_commit=False, autoflush=False)
    async with sm() as db:
        await seed_all(db)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker(engine):
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def db(sessionmaker) -> AsyncSession:
    async with sessionmaker() as session:
        yield session


@pytest_asyncio.fixture
async def app(sessionmaker):
    application = create_app()

    async def _override_get_db():
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_db] = _override_get_db
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_client(client):
    """Client with a signed-up account and Authorization header set."""
    resp = await client.post(
        "/api/v1/auth/signup",
        json={"email": "doc@example.com", "password": "password123", "display_name": "Dr Test"},
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def create_patient(client: AsyncClient, **overrides) -> dict:
    payload = {
        "full_name": "Ramesh Kumar",
        "sex": "male",
        "date_of_birth": "1968-05-10",
        "consent_given": True,
    }
    payload.update(overrides)
    resp = await client.post("/api/v1/patients", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()
