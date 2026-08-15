"""Which address a request is attributed to, behind zero or more reverse proxies.

Three things key on this: the address arm of the login lockout, the per-address signup
ceiling, and the ``ip_address`` written to ``sessions`` and to every auth audit entry. All
three read the peer on the TCP connection, which behind the reference nginx front end is
nginx — so they were, respectively, a deployment-wide lockout, a deployment-wide signup
ceiling, and an access trail that recorded the proxy on every row.

``X-Forwarded-For`` fixes that and is client-settable, which is the tension these tests pin:
it is honoured for exactly as many hops as the deployment declares it operates, and for none
at all by default.
"""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers

from app.config import settings
from app.core.client_address import client_address


class _Peer:
    def __init__(self, host: str) -> None:
        self.host = host


class _Request:
    """The two attributes ``client_address`` reads, without an ASGI scope to build."""

    def __init__(self, peer: str | None, forwarded_for: str | None = None) -> None:
        self.client = _Peer(peer) if peer is not None else None
        raw = [(b"x-forwarded-for", forwarded_for.encode())] if forwarded_for else []
        self.headers = Headers(raw=raw)


def _address(peer, forwarded_for=None):
    return client_address(_Request(peer, forwarded_for))  # type: ignore[arg-type]


@pytest.fixture
def hops(monkeypatch):
    def _set(count: int) -> None:
        monkeypatch.setattr(settings, "trusted_proxy_hops", count)

    return _set


# --- Default: trust nothing -------------------------------------------------------------


def test_the_header_is_ignored_when_no_proxy_is_declared(hops):
    """The safe default. A directly-exposed uvicorn sees the real peer, and a client that
    invents an ``X-Forwarded-For`` must not be able to pick its own rate-limit key."""
    hops(0)
    assert _address("203.0.113.7", "198.51.100.9") == "203.0.113.7"


def test_no_peer_and_no_trust_is_unknown(hops):
    """In-process ASGI clients and unix sockets report no peer at all."""
    hops(0)
    assert _address(None) is None


# --- One proxy (the reference nginx deployment) -----------------------------------------


def test_one_hop_takes_the_address_the_proxy_observed(hops):
    """nginx appends ``$remote_addr`` to whatever the client sent, so with one trusted hop the
    rightmost entry is the only one nginx vouched for."""
    hops(1)
    assert _address("10.0.0.2", "203.0.113.7") == "203.0.113.7"


def test_one_hop_discards_everything_the_client_prepended(hops):
    """The attack the hop count exists to stop: entries to the left of the trusted tail are
    whatever the caller chose to send, and must not become the key."""
    hops(1)
    assert _address("10.0.0.2", "1.2.3.4, 5.6.7.8, 203.0.113.7") == "203.0.113.7"


def test_two_hops_reach_past_the_inner_proxy(hops):
    """Load balancer then nginx: the LB appends the client, nginx appends the LB."""
    hops(2)
    assert _address("10.0.0.2", "203.0.113.7, 10.0.0.9") == "203.0.113.7"


def test_a_missing_header_falls_back_to_the_peer(hops):
    """A proxy that drops the header, or a direct hit on the container port."""
    hops(1)
    assert _address("10.0.0.2") == "10.0.0.2"


def test_a_chain_shorter_than_the_declared_hops_uses_its_leftmost_entry(hops):
    """The request did not arrive through the expected topology. The leftmost entry is the
    closest thing to a client address on offer and is no more forgeable than the header
    already is — the alternative, an index off the front of the list, would be a crash."""
    hops(3)
    assert _address("10.0.0.2", "203.0.113.7") == "203.0.113.7"


def test_whitespace_and_empty_entries_are_not_mistaken_for_addresses(hops):
    """``X-Forwarded-For: , 203.0.113.7`` is what a proxy chain with one empty hop emits."""
    hops(1)
    assert _address("10.0.0.2", " , 203.0.113.7 ") == "203.0.113.7"


def test_an_all_empty_header_falls_back_to_the_peer(hops):
    hops(1)
    assert _address("10.0.0.2", " , , ") == "10.0.0.2"


# --- Through the running application ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_session_records_the_forwarded_address_not_the_proxy(client, monkeypatch):
    """End to end: what lands in ``sessions.ip_address``, which is the DPDP access trail's
    answer to "where was this signed in from" and the list behind "sign out that device"."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    email = "forwarded@example.com"
    signup = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "password123"},
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    assert signup.status_code == 201, signup.text

    sessions = await client.get(
        "/api/v1/auth/sessions",
        headers={"Authorization": f"Bearer {signup.json()['access_token']}"},
    )
    assert sessions.status_code == 200
    assert [s["ip_address"] for s in sessions.json()] == ["203.0.113.7"]


@pytest.mark.asyncio
async def test_a_spoofed_header_cannot_move_a_caller_off_its_rate_limit_key(client, monkeypatch):
    """With no proxy declared, rotating ``X-Forwarded-For`` must not hand a caller a fresh
    signup budget — which is the whole reason the header is opt-in rather than assumed."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    monkeypatch.setattr(settings, "rate_limit_signups_per_hour", 2)

    statuses = []
    for index in range(3):
        resp = await client.post(
            "/api/v1/auth/signup",
            json={"email": f"spoof{index}@example.com", "password": "password123"},
            headers={"X-Forwarded-For": f"203.0.113.{index}"},
        )
        statuses.append(resp.status_code)
    assert statuses == [201, 201, 429]
