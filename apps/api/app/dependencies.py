"""FastAPI dependency-injection wiring."""

from __future__ import annotations

import uuid

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_db
from app.exceptions import TokenError
from app.models.user import Account

# Declared purely so the OpenAPI schema records that these routes take a bearer token: without
# a security scheme in the dependency tree, the generated spec claimed every patient route was
# open, /docs offered no Authorize button, and a generated client had no idea to send the
# header. ``auto_error=False`` keeps it documentation-only -- it returns None instead of
# raising its own bare 403, leaving the parsing and the clinician-facing 401 below untouched.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="AccessToken",
    description=(
        "Short-lived access token from `POST /api/v1/auth/login`, sent as `Bearer <token>`."
    ),
)


async def get_current_account(
    request: Request,
    _credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Account:
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
