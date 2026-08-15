"""Health and readiness probes."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm import (
    available_providers,
    demo_fallback_enabled,
    probe_all_providers,
    using_simulated_llm,
)
from app.agents.util import llm_executor
from app.config import settings
from app.core.client_address import proxy_configuration_report
from app.db.session import get_db
from app.dependencies import get_current_account
from app.openapi import errors

router = APIRouter(tags=["health"])


def _llm_mode() -> str:
    """How clinical reasoning is currently powered: live | demo | offline."""
    if available_providers():
        return "live"
    if using_simulated_llm():
        return "demo"
    return "offline"


@router.get("/health", summary="Service status and how reasoning is currently powered")
async def health() -> dict:
    """Unauthenticated status probe.

    `llm_mode` is the operationally interesting field: `live` (a provider key is configured),
    `demo` (clinical output is simulated `[DEMO MODE]` sample text, never real reasoning), or
    `offline` (no provider — the engine is paused). Deterministic drug-safety and lab checks
    run in all three, so `offline` does not mean unsafe, it means un-reasoned.
    """
    mode = _llm_mode()
    return {
        "status": "ok",
        "env": settings.app_env,
        "demo_mode": settings.demo_mode,
        "llm_mode": mode,
        # True when clinical output is built from simulated "[DEMO MODE]" sample data.
        "llm_simulated": mode == "demo",
    }


@router.get("/health/live", summary="Liveness probe")
async def live() -> dict:
    """Answers as long as the process is up. Touches nothing — a restart-me signal only."""
    return {"status": "alive"}


@router.get("/health/ready", summary="Readiness probe", responses=errors(500))
async def ready(db: AsyncSession = Depends(get_db)) -> dict:
    """Ready to serve traffic: the database answers a trivial query.

    Fails loudly (500) rather than reporting a degraded state, because a request that cannot
    reach the patient graph has nothing useful to fall back on.
    """
    await db.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get(
    "/health/dependencies",
    summary="Per-dependency detail for operators",
    responses=errors(401),
    dependencies=[Depends(get_current_account)],
)
async def dependencies(db: AsyncSession = Depends(get_db)) -> dict:
    """Which backing services and LLM providers are reachable and configured.

    Always 200, including when a dependency is down — the point is to report the state, not to
    gate traffic on it. Use `/health/ready` for that.

    Authenticated, unlike its three sibling probes. `nginx.conf` proxies `location /health` as
    a prefix, so this was answering anonymous callers on the public internet with the
    deployment's vendor inventory: which LLM providers hold keys, which is primary, the
    OpenRouter fallback topology, and the storage backend. None of that is PHI and none of it
    is secret on its own, but it is the shape of the infrastructure, published to whoever asks.

    `/health`, `/health/live` and `/health/ready` stay open — load balancers and container
    probes call those without credentials, and their bodies are deliberately thin. (`/health`
    does report `llm_mode`, so "reasoning is simulated" remains public by design: a clinician
    needs to know the engine is in demo mode, and it is a property of the deployment rather
    than of its wiring.)

    **`llm_providers` is the field to watch.** Every other LLM field here, and `llm_mode` on
    the public probe, answers "is a key configured" — which a revoked, expired or
    out-of-quota key passes. This one calls each configured provider and reports whether it
    answered, so the gap between "we hold a key" and "reasoning works" is visible here rather
    than to whichever clinician runs the next panel. Results are cached briefly; see
    `LLM_HEALTH_PROBE_TTL_SECONDS`.
    """
    db_ok = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    # Off the event loop, and onto the bounded LLM pool rather than the default executor.
    #
    # This said "same offload idiom as `agents.util.call_llm`" while doing the opposite of what
    # that function does. `call_llm` goes out of its way to run on `llm_executor()` precisely so
    # a provider that stops answering cannot occupy the default pool — which is where bcrypt
    # runs, so that pool filling up is what "logging in stopped working during an LLM outage"
    # looked like. `asyncio.to_thread` is the default pool.
    #
    # The bound alone is not the whole reason. There is no single-flight around the probe cache,
    # so a cold cache and N concurrent callers make N probe runs, and the callers are a
    # monitoring poller and whichever operators are refreshing the status page — which is to say
    # they arrive together, during the outage, at exactly the moment a clinician is trying to
    # sign in. Each run holds its thread for up to `llm_health_probe_timeout_seconds` per
    # configured provider, sequentially. Contained here, that queues status probes behind each
    # other, which is the correct thing for it to cost.
    providers = await asyncio.get_running_loop().run_in_executor(
        llm_executor(), probe_all_providers
    )
    reachable = [name for name, state in providers.items() if state["reachable"]]
    return {
        "database": "ok" if db_ok else "error",
        # What this process has observed about the proxy in front of it. Nothing can validate
        # TRUSTED_PROXY_HOPS at startup — whether a proxy is there is topology, not
        # configuration — and left wrong it fails silently, so the observation is reported
        # here instead. See app.core.client_address.
        "proxy_configuration": proxy_configuration_report(),
        # Pool occupancy, so connection-pool exhaustion is diagnosable while it is happening
        # rather than afterwards from a wall of timeouts. `checked_out` climbing to
        # `pool_size + overflow` is the signal; the long-running reasoning routes hold a
        # connection for the length of a panel, so this is the number that moves first.
        "database_pool": _pool_stats(db),
        "llm_configured": settings.llm_configured,
        "llm_provider": settings.llm_provider,
        # Providers (in fallback order) that currently have an API key configured.
        "llm_available_providers": available_providers(),
        # Per-provider {configured, reachable, error}. `reachable` is None when there is no
        # key to authenticate a probe with.
        "llm_providers": providers,
        # True when at least one configured provider actually answered. This, not `llm_mode`,
        # is what "reasoning will work" means.
        "llm_reachable": bool(reachable),
        "llm_mode": _llm_mode(),
        "llm_fallback_enabled": settings.llm_fallback_enabled,
        "llm_openrouter_fallback": settings.llm_openrouter_fallback,
        # True when an OpenRouter key is present and the OpenRouter tier is enabled.
        "llm_openrouter_configured": bool(
            settings.llm_openrouter_fallback and settings.openrouter_api_key
        ),
        "llm_demo_fallback": demo_fallback_enabled(),
        "llm_simulated": using_simulated_llm(),
        "storage_backend": settings.storage_backend,
    }


def _pool_stats(db: AsyncSession) -> dict:
    """Connection-pool occupancy, or an empty dict for a pool that does not report it.

    Read from the session's own bind rather than from ``get_engine()``: that is the pool the
    request in hand actually came out of, and asking for the process-global engine would
    *create* one in any context where it does not already exist.

    SQLite (tests) uses ``StaticPool``, which does not carry every counter, so this reports
    what it can rather than raising on a health probe.
    """
    try:
        # get_bind() is typed as Engine | Connection; only the engine carries a pool, and a
        # session bound to a bare Connection (never the case here) reports nothing.
        pool = getattr(db.get_bind(), "pool", None)
    except Exception:  # noqa: BLE001 — a probe never fails on its own diagnostics
        return {}
    if pool is None:
        return {}
    stats: dict = {}
    for name, attribute in (
        ("size", "size"),
        ("checked_out", "checkedout"),
        ("checked_in", "checkedin"),
        ("overflow", "overflow"),
    ):
        getter = getattr(pool, attribute, None)
        if callable(getter):
            try:
                stats[name] = getter()
            except Exception:  # noqa: BLE001 — a probe never fails on its own diagnostics
                continue
    return stats
