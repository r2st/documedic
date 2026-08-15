"""Which address a request actually came from, behind a reverse proxy.

``request.client.host`` is the peer on the TCP connection. In this product's reference
deployment that peer is nginx (``nginx/nginx.conf`` proxies ``/api/`` to the API container),
so every clinician in a hospital arrives at the application under one address — the proxy's.

Three controls key on that address and all three were quietly wrong because of it:

* the login lockout in :mod:`app.services.auth_service`, whose IP arm then counted the whole
  deployment's failed sign-ins into one budget;
* the per-address signup ceiling in :func:`app.dependencies.rate_limit_by_ip`, which became a
  global ceiling rather than a per-caller one;
* ``sessions.ip_address`` and the ``ip_address`` recorded on every auth audit entry, which are
  part of the DPDP access trail and were recording the proxy on every row — so the trail could
  not say where an access came from, and "sign out that device" listed identical addresses.

``X-Forwarded-For`` is the header that carries the real address, and it is client-settable, so
it can only be trusted for as many hops as the deployment actually operates. That count is
configuration (``TRUSTED_PROXY_HOPS``), never a guess: with it left at 0 the header is ignored
entirely and the peer address is used, which is correct for a directly-exposed uvicorn and is
the safe default for anything else.
"""

from __future__ import annotations

import logging

from starlette.requests import Request

from app.config import settings

logger = logging.getLogger(__name__)

_FORWARDED_FOR = "x-forwarded-for"

# Set once, when a request carries ``X-Forwarded-For`` while ``trusted_proxy_hops`` is 0.
# Reported by :func:`proxy_configuration_report`; see :func:`_note_untrusted_forwarded_for`
# for why this is a signal worth surfacing and not a conclusion.
_forwarded_for_seen_while_untrusted = False


def _note_untrusted_forwarded_for(request: Request) -> None:
    """Record that a forwarded-for header arrived on a deployment configured to ignore it.

    ``TRUSTED_PROXY_HOPS`` is the one setting in this area that nothing can validate at
    startup: whether a proxy sits in front of the API is a property of the deployment's
    topology, not of its configuration, so the process cannot know at boot that its default of
    0 is wrong. Left wrong it is *silent* — every per-address control keeps working, keyed on
    an address that is the same for everyone (see this module's docstring). That is the
    failure this whole module exists to fix, and shipping ``nginx/nginx.conf`` while leaving
    the setting at 0 puts it straight back.

    So the deployment is asked at runtime instead. A request carrying ``X-Forwarded-For`` on a
    deployment that trusts no hops is evidence that *something* in front is setting it.

    Evidence, not proof: the header is client-settable, so on a directly-exposed uvicorn — the
    configuration for which 0 is correct — any caller can set it and raise this flag. It is
    therefore reported, never acted on, and never used to change how an address is resolved. A
    false positive costs one log line and one true field on an authenticated health endpoint;
    the operator reading it knows their own topology and can tell instantly which it is.

    Logged once rather than per request: this fires on every request once a proxy is in front,
    and a warning per request is how an operator learns to filter the warning out.
    """
    global _forwarded_for_seen_while_untrusted
    if _forwarded_for_seen_while_untrusted or _FORWARDED_FOR not in request.headers:
        return
    _forwarded_for_seen_while_untrusted = True
    logger.warning(
        "Received X-Forwarded-For while TRUSTED_PROXY_HOPS=0, so it is being ignored and the "
        "peer address is used instead. If this API runs behind a reverse proxy you operate "
        "(the reference nginx/nginx.conf is one hop), set TRUSTED_PROXY_HOPS to the number of "
        "hops: until then every caller shares the proxy's address, which makes the login "
        "lockout a deployment-wide sign-in block, the signup ceiling a global one, and the "
        "address on sessions and auth audit entries the proxy's rather than the clinician's. "
        "If uvicorn is exposed directly then 0 is correct and a caller simply sent the header."
    )


def proxy_configuration_report() -> dict:
    """What this process has observed about the proxy in front of it, for health output."""
    return {
        "trusted_proxy_hops": settings.trusted_proxy_hops,
        # True once a request arrived carrying X-Forwarded-For while trusting no hops. A
        # deployment behind a proxy should read this as "TRUSTED_PROXY_HOPS is unset"; one
        # with uvicorn exposed directly should read it as "a caller sent a header we ignored".
        "forwarded_for_seen_while_untrusted": _forwarded_for_seen_while_untrusted,
    }


def reset_proxy_observations() -> None:
    """Forget what has been observed about the proxy. For tests, and for nothing else."""
    global _forwarded_for_seen_while_untrusted
    _forwarded_for_seen_while_untrusted = False


def client_address(request: Request) -> str | None:
    """The caller's address, honouring ``X-Forwarded-For`` for trusted proxy hops only.

    Returns ``None`` when the transport reports no peer at all (in-process ASGI clients, unix
    sockets) and the header gives nothing usable — callers treat that as "unknown", not as an
    address that can be compared.

    Each proxy in the chain *appends* the address it saw to ``X-Forwarded-For`` (nginx's
    ``$proxy_add_x_forwarded_for`` does exactly this), so the rightmost entries are the
    trustworthy ones and everything to their left is whatever the client chose to send. With
    ``TRUSTED_PROXY_HOPS = n`` the entry ``n`` places from the right is the address the
    outermost proxy we operate observed; anything further left is discarded.

    A chain shorter than the configured hop count means the request did not arrive through the
    expected topology (a direct hit on the container port, or a proxy that drops the header).
    The leftmost entry is then the closest thing to a client address on offer, and it is no
    more forgeable than the header already is.
    """
    peer = request.client.host if request.client else None
    hops = settings.trusted_proxy_hops
    if hops <= 0:
        _note_untrusted_forwarded_for(request)
        return peer

    chain = [part.strip() for part in request.headers.get(_FORWARDED_FOR, "").split(",")]
    chain = [part for part in chain if part]
    if not chain:
        return peer
    return chain[max(len(chain) - hops, 0)]
