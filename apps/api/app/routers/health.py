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

router = APIRouter(tags=["health"])


def _llm_mode() -> str:
    """How clinical reasoning is currently powered: live | demo | offline."""
    if available_providers():
        return "live"
    if using_simulated_llm():
        return "demo"
    return "offline"


@router.get("/health")
async def health() -> dict:
    mode = _llm_mode()
    return {
        "status": "ok",
        "env": settings.app_env,
        "demo_mode": settings.demo_mode,
        "llm_mode": mode,
        # True when clinical output is built from simulated "[DEMO MODE]" sample data.
        "llm_simulated": mode == "demo",
    }


@router.get("/health/live")
async def live() -> dict:
    return {"status": "alive"}


@router.get("/health/ready")
async def ready(db: AsyncSession = Depends(get_db)) -> dict:
    await db.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/health/dependencies")
async def dependencies(db: AsyncSession = Depends(get_db)) -> dict:
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
