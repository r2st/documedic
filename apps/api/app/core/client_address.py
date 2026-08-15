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

from starlette.requests import Request

from app.config import settings

_FORWARDED_FOR = "x-forwarded-for"


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
        return peer

    chain = [part.strip() for part in request.headers.get(_FORWARDED_FOR, "").split(",")]
    chain = [part for part in chain if part]
    if not chain:
        return peer
    return chain[max(len(chain) - hops, 0)]
