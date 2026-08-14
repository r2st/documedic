"""FastAPI dependency-injection wiring."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.rate_limit import RateLimit, SlidingWindowLimiter, retry_after_seconds
from app.core.security import decode_token
from app.db.session import get_db
from app.exceptions import RateLimitExceededError, TokenError
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


# --- Rate limiting ------------------------------------------------------------------------
#
# One process-wide limiter shared by every bucket; keys are namespaced by bucket name so two
# buckets never collide. See app/core/rate_limit.py for why this is in-process and for what is
# deliberately left unmetered.
limiter = SlidingWindowLimiter()

_SECONDS_PER_MINUTE = 60.0
_SECONDS_PER_HOUR = 3600.0

# Bucket name -> (settings attribute holding the ceiling, window length in seconds). A bucket
# may be shared by several routes, which is the point for `reasoning_run`: POST ../run and
# GET ../stream drive the same eight-agent panel.
_BUCKETS: dict[str, tuple[str, float]] = {
    "reasoning_run": ("rate_limit_reasoning_runs_per_minute", _SECONDS_PER_MINUTE),
    "reasoning_intake": ("rate_limit_intake_per_minute", _SECONDS_PER_MINUTE),
    "document_upload": ("rate_limit_uploads_per_minute", _SECONDS_PER_MINUTE),
    "guideline_search": ("rate_limit_searches_per_minute", _SECONDS_PER_MINUTE),
    "signup": ("rate_limit_signups_per_hour", _SECONDS_PER_HOUR),
    "validation_run": ("rate_limit_validation_runs_per_hour", _SECONDS_PER_HOUR),
}


def limit_for(bucket: str) -> RateLimit | None:
    """The ceiling configured for ``bucket``, or ``None`` when it is switched off.

    Read at request time, not at import time, so a deployment's environment and a test's
    monkeypatched setting both take effect without rebuilding the dependency tree.
    """
    if bucket not in _BUCKETS:
        raise KeyError(f"Unknown rate-limit bucket {bucket!r}; add it to _BUCKETS.")
    if not settings.rate_limit_enabled:
        return None
    setting_name, window_seconds = _BUCKETS[bucket]
    ceiling: int = getattr(settings, setting_name)
    if ceiling <= 0:  # explicitly disabled, per-bucket
        return None
    return RateLimit(max_requests=ceiling, window_seconds=window_seconds)


def enforce_rate_limit(bucket: str, key: str, *, subject: str) -> None:
    """Meter one request, raising :class:`RateLimitExceededError` when it is over the ceiling.

    ``subject`` names what the ceiling is per ("account", "address") and reaches the clinician —
    "you are asking too fast" and "this server is busy" call for different responses, and the
    message should not leave which one it is to guesswork.
    """
    limit = limit_for(bucket)
    if limit is None:
        return
    wait = limiter.check(f"{bucket}:{key}", limit)
    if wait is None:
        return
    seconds = retry_after_seconds(wait)
    raise RateLimitExceededError(
        seconds,
        f"This has been requested too many times in the last few minutes from this {subject}. "
        f"Wait about {seconds} second{'s' if seconds != 1 else ''} and try again — nothing was "
        "saved to the chart, and the drug-safety checks are unaffected.",
        detail=(
            f"bucket {bucket!r} exceeded {limit.max_requests} requests per "
            f"{limit.window_seconds:.0f}s; retry_after={seconds}s"
        ),
    )


def rate_limit(bucket: str) -> Callable[[Account], Awaitable[None]]:
    """A route dependency metering ``bucket`` per authenticated account.

    Keyed by account rather than by IP: a hospital's clinicians share one egress address, so an
    IP key would have them competing for a single budget and one busy clinic would throttle the
    rest. It resolves the account first, so an unauthenticated request 401s without spending
    anyone's quota, and FastAPI's per-request dependency cache means the token is decoded once
    even though the handler asks for the account too.

    Attach it in the decorator (``dependencies=[Depends(rate_limit("reasoning_run"))]``) so the
    handler signature stays about the clinical operation.
    """
    limit_for(bucket)  # fail at import time, not on the first request, if the name is wrong

    async def enforce(account: Account = Depends(get_current_account)) -> None:
        enforce_rate_limit(bucket, str(account.id), subject="account")

    return enforce


def rate_limit_by_ip(bucket: str) -> Callable[[Request], Awaitable[None]]:
    """A route dependency metering ``bucket`` per client address, for unauthenticated routes.

    Only for routes with no account to key on — signup being the one that matters, since
    unlimited account creation would otherwise walk straight around every per-account ceiling
    above.

    Caveat, shared with the login lockout in ``auth_service`` which keys the same way: uvicorn
    is not started with ``--proxy-headers``, so behind the nginx front end ``request.client``
    is the proxy and every caller collapses onto one key. That makes this a global ceiling
    rather than a per-address one, which is why it is set generously — a limit that is blunter
    than intended is still a limit, but it must not be tight enough to lock out a clinic
    onboarding its staff. ``X-Forwarded-For`` is not consulted: it is client-settable, so
    trusting it here would hand an attacker an unlimited supply of keys.
    """
    limit_for(bucket)

    async def enforce(request: Request) -> None:
        # No client on the connection happens for ASGI transports that do not report a peer
        # (in-process test clients, unix sockets). One shared key is the conservative reading.
        key = request.client.host if request.client else "unknown"
        enforce_rate_limit(bucket, key, subject="address")

    return enforce
