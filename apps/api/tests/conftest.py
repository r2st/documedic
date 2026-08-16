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

from app.agents.circuit import breaker as llm_circuit_breaker
from app.agents.llm import close_provider_clients
from app.db import session as db_session
from app.db.seed import seed_all
from app.db.session import get_db
from app.dependencies import limiter
from app.main import create_app
from app.models import Base
from app.services.auth_service import reset_sweep_schedule
from app.services.guideline_service import reset_corpus_cache
from app.services.summary_service import reset_summary_cache


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Clear the process-wide rate-limit windows around every test.

    ``app.dependencies.limiter`` is a module-level singleton whose state is intentionally not
    per-request, so without this it accumulates across the whole session and tests start
    failing on each other's traffic: the per-address signup ceiling alone would be spent by the
    first handful of ``auth_client`` fixtures and every later test would 429 in setup.

    Both sides of the yield: clearing on the way in gives a test a known-empty window
    regardless of what ran before it, and clearing on the way out keeps a test that
    deliberately exhausts a bucket from leaking that into the next one.
    """
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _reset_session_sweep_schedule():
    """Forget when the session retention sweep last ran.

    ``auth_service._last_session_sweep`` is process-global and deliberately so — it is an
    hourly scheduling hint, not a lock. Left alone across tests, the first sign-in anywhere in
    the suite claims the hour and every later test that expects a sweep silently gets none.
    """
    reset_sweep_schedule()
    yield
    reset_sweep_schedule()


@pytest.fixture(autouse=True)
def _reset_llm_circuit_breaker():
    """Clear the per-provider circuit-breaker state around every test.

    ``app.agents.circuit.breaker`` is a process-wide singleton on purpose — an outage one caller
    discovers is one the next should not have to rediscover — which across a test session means
    a test that deliberately trips a provider would leave it tripped for every later test that
    expects a provider call to happen. Cleared on both sides for the same reason
    ``_reset_rate_limiter`` is.
    """
    llm_circuit_breaker.reset()
    yield
    llm_circuit_breaker.reset()


@pytest.fixture(autouse=True)
def _reset_llm_provider_clients():
    """Close and forget the cached provider SDK clients around every test.

    ``app.agents.llm`` keeps one client per (provider, key, endpoint, timeout) so connections
    are reused instead of re-handshaked per call. The key is read from ``settings``, and tests
    monkeypatch those settings per case — so without this a test that patched a key or a
    timeout could be served the client another test's configuration built, and a test asserting
    on how many clients were constructed would count the whole session's.
    """
    close_provider_clients()
    yield
    close_provider_clients()


@pytest.fixture(autouse=True)
def _reset_guideline_corpus_cache():
    """Clear the process-wide guideline corpus cache around every test.

    ``GuidelineService._load_corpus`` caches per ``corpus_version``, keyed on a freshness stamp
    of (row count, latest ``updated_at``). In production that is sound: the corpus is versioned
    reference data in one database, and an ingestion moves both halves of the stamp.

    Here it is not, and for a reason peculiar to the test environment. Every test builds a
    *fresh in-memory database* behind the same process, so two tests can seed the same version
    with different chunks; if they seed the same number of rows, the counts match, and SQLite's
    ``CURRENT_TIMESTAMP`` has one-second granularity, so the timestamps match too. The second
    test would then score against the first one's corpus. Nothing in production creates a new
    empty database underneath a running process, which is the assumption the stamp is allowed
    to make and this fixture restores.
    """
    reset_corpus_cache()
    yield
    reset_corpus_cache()


@pytest.fixture(autouse=True)
def _reset_clinical_summary_cache():
    """Clear the process-wide summary narrative cache around every test.

    Keyed on a hash of the prompt text, which is derived from chart content — and every test
    builds a fresh in-memory database behind the same process, so two tests that chart the same
    patient produce the same key. The second would read the first's prose without calling its
    own stubbed provider, and the assertion it makes about that provider would pass for the
    wrong reason. Same class of hazard as the guideline corpus cache above, and the same fix.
    """
    reset_summary_cache()
    yield
    reset_summary_cache()


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


@pytest_asyncio.fixture
async def second_auth_client(app):
    """A *different* account on the same app, for tenancy and concurrency tests.

    Deliberately its own AsyncClient rather than a header swap on ``auth_client``: several
    tests interleave requests from both clinicians, and sharing one client would silently
    make the last-set Authorization header win.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/auth/signup",
            json={
                "email": "doc2@example.com",
                "password": "password456",
                "display_name": "Dr Second",
            },
        )
        assert resp.status_code == 201, resp.text
        ac.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        yield ac


@pytest_asyncio.fixture
async def colleague_client(app, auth_client):
    """A second HTTP client authenticated as the **same** account as ``auth_client``.

    Models two clinicians sharing one practice login (or one clinician with the chart open
    in two tabs) — both see and may edit the same patients, which is the situation where
    lost updates and stale reads actually happen.
    """
    resp = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": "doc@example.com", "password": "password123"},
    )
    assert resp.status_code == 200, resp.text
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        yield ac


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
