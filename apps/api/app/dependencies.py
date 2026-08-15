"""FastAPI dependency-injection wiring."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.client_address import client_address
from app.core.rate_limit import RateLimit, SlidingWindowLimiter, retry_after_seconds
from app.core.security import decode_token
from app.db.session import get_db
from app.exceptions import RateLimitExceededError, TokenError
from app.models.user import Account
from app.models.user import Session as AuthSession

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
    await assert_auth_session_live(db, payload, account_id=account_id)
    # Stashed so a handler that needs to mint a token scoped to this same sign-in (see
    # `routers.reasoning.mint_stream_token`) does not have to re-parse the header to find it.
    request.state.auth_session_id = uuid.UUID(str(payload["asid"]))
    return account


async def assert_auth_session_live(
    db: AsyncSession, payload: dict, *, account_id: uuid.UUID
) -> None:
    """Reject a token whose sign-in has since been revoked or expired.

    A JWT is valid until it expires, so on its own an access token is a credential that cannot
    be withdrawn. Every revocation this service offers acted only on the refresh token, which
    meant each of them was a promise it kept fifteen minutes late:

    * "Sign out everywhere" after losing a laptop left the laptop reading charts.
    * "Sign out this other device" did not sign out that device.
    * Changing the password — which ``AuthService.change_password`` performs *because* someone
      may have reached an unlocked workstation — revoked the intruder's refresh token and left
      their access token working.
    * Refresh-token reuse detection revokes the whole family precisely because one of the two
      holders is an attacker; the attacker's access token survived that too.

    The check is one indexed lookup on a session id the token already carries, on a request
    that has just fetched the account row anyway. Both conditions matter and neither implies
    the other: ``is_revoked`` is the deliberate withdrawal, and ``expires_at`` is the absolute
    ceiling on a sign-in that ``_issue_tokens`` anchors to when the *family* started, so a
    long-lived family's last access token must not outlive it either.

    A token with no ``asid`` names no sign-in, so there is nothing that could revoke it — it is
    refused rather than waved through, which is what keeps the claim from being optional in
    practice. Tokens minted before this claim existed are refused the same way; the client's
    refresh flow answers a 401 by rotating, so the cost is one extra round-trip per signed-in
    browser at deploy, not a re-login.

    ``account_id`` asserts the two identities in the token agree: the sign-in named by ``asid``
    must belong to the account named by ``sub``. Nothing in the service mints a mismatched pair
    — both claims are written in one place, from one account, by ``_issue_tokens`` — so this is
    not a hole being closed but an invariant being *stated*. It was true only by the good
    behaviour of every current call site, which is the kind of truth that survives until someone
    adds an impersonation route or a token-minting helper and pairs the wrong session with the
    wrong subject. Checked here because both rows are already in hand and the comparison is
    free.

    Required rather than defaulted to ``None``, for the same reason ``asid`` itself is not
    optional: a security check a caller can silently skip is one a caller will eventually skip.
    Every call site already resolves the account before reaching here, so passing it costs
    nothing and a new one that forgets fails to type-check rather than failing open.
    """
    raw = payload.get("asid")
    if raw is None:
        raise TokenError(detail="token carries no asid claim, so no sign-in backs it")
    try:
        auth_session_id = uuid.UUID(str(raw))
    except ValueError as exc:
        raise TokenError(detail=f"token asid {raw!r} is not a uuid") from exc

    session = await db.get(AuthSession, auth_session_id)
    if session is None:
        raise TokenError(detail=f"no session row for asid {auth_session_id}")
    if session.account_id != account_id:
        raise TokenError(
            detail=(
                f"session {auth_session_id} belongs to account {session.account_id}, "
                f"not to the token subject {account_id}"
            )
        )
    if session.is_revoked:
        raise TokenError(
            "You have been signed out of this device. Sign in again to continue.",
            detail=f"session {auth_session_id} is revoked",
        )
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise TokenError(
            "Your sign-in session has expired. Sign in again to continue.",
            detail=f"session {auth_session_id} past its absolute expiry",
        )


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
    "password_reset": ("rate_limit_password_resets_per_hour", _SECONDS_PER_HOUR),
    "validation_run": ("rate_limit_validation_runs_per_hour", _SECONDS_PER_HOUR),
    "record_export": ("rate_limit_exports_per_hour", _SECONDS_PER_HOUR),
    # Shared by the per-patient chain walk and the dossier's full-table one, for the same
    # reason `reasoning_run` is shared by POST ../run and GET ../stream: they are the same
    # work, and a caller must not be able to double its spend by alternating them.
    "chain_verification": ("rate_limit_chain_verifications_per_hour", _SECONDS_PER_HOUR),
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

    The key comes from ``client_address``, which consults ``X-Forwarded-For`` for as many hops
    as ``TRUSTED_PROXY_HOPS`` says this deployment operates and ignores it entirely otherwise.
    Read straight off ``request.client`` — as this did — every caller behind the reference
    nginx front end collapses onto the proxy's address, turning a per-address ceiling into a
    global one: ten signups an hour for an entire hospital, so a clinic onboarding its staff
    locks itself out. The header is client-settable, which is exactly why the hop count is
    configuration and defaults to not trusting it at all.
    """
    limit_for(bucket)

    async def enforce(request: Request) -> None:
        # No address happens for ASGI transports that do not report a peer (in-process test
        # clients, unix sockets). One shared key is the conservative reading.
        key = client_address(request) or "unknown"
        enforce_rate_limit(bucket, key, subject="address")

    return enforce
