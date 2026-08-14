"""Health and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm import (
    available_providers,
    demo_fallback_enabled,
    using_simulated_llm,
)
from app.config import settings
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
    """
    db_ok = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {
        "database": "ok" if db_ok else "error",
        "llm_configured": settings.llm_configured,
        "llm_provider": settings.llm_provider,
        # Providers (in fallback order) that currently have an API key configured.
        "llm_available_providers": available_providers(),
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
