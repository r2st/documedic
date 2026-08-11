"""FastAPI dependency-injection wiring."""

from __future__ import annotations

import uuid

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_db
from app.exceptions import TokenError
from app.models.user import Account


async def get_current_account(request: Request, db: AsyncSession = Depends(get_db)) -> Account:
    """Resolve the authenticated account from the Bearer access token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise TokenError(
            "You are not signed in. Sign in to open patient records.",
            detail="request carried no Bearer authorization header",
        )
    token = auth.split(" ", 1)[1].strip()
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise TokenError(detail=f"token type {payload.get('type')!r}, expected 'access'")
    try:
        account_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise TokenError(detail="token subject is missing or not a uuid") from exc

    account = await db.get(Account, account_id)
    if account is None or account.is_deleted:
        raise TokenError(
            "This account is no longer active. Contact your administrator to restore access.",
            detail=f"account {account_id} absent or soft-deleted",
        )
    return account


# Convenience alias used across routers.
CurrentAccount = Depends(get_current_account)
DBSession = Depends(get_db)
