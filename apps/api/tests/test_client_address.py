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

import logging

import pytest
from starlette.datastructures import Headers

from app.config import settings
from app.core.client_address import (
    client_address,
    proxy_configuration_report,
    reset_proxy_observations,
)


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


# --- Noticing that TRUSTED_PROXY_HOPS was never set --------------------------------------
#
# Nothing can validate this setting at startup: whether a proxy sits in front of the API is a
# property of the deployment's topology, not of its configuration. Left at the default of 0
# behind a proxy it fails silently — every control above keeps working, on an address that is
# the same for everyone — so the process reports what it observes instead.


@pytest.fixture(autouse=True)
def _forget_observations():
    """Module-level observation, so it has to be cleared around every test that reads it."""
    reset_proxy_observations()
    yield
    reset_proxy_observations()


def test_a_forwarded_header_on_an_untrusting_deployment_is_reported(hops):
    """The signal. Something in front is setting the header and the API is ignoring it."""
    hops(0)
    assert proxy_configuration_report()["forwarded_for_seen_while_untrusted"] is False
    _address("10.0.0.2", "203.0.113.7")
    report = proxy_configuration_report()
    assert report["forwarded_for_seen_while_untrusted"] is True
    assert report["trusted_proxy_hops"] == 0


def test_no_header_leaves_the_report_clean(hops):
    """A directly-exposed uvicorn with no proxy in front says nothing at all."""
    hops(0)
    _address("203.0.113.7")
    assert proxy_configuration_report()["forwarded_for_seen_while_untrusted"] is False


def test_a_header_that_is_being_honoured_is_not_a_misconfiguration(hops):
    """With hops declared the header is used, not ignored, so there is nothing to report."""
    hops(1)
    assert _address("10.0.0.2", "203.0.113.7") == "203.0.113.7"
    assert proxy_configuration_report()["forwarded_for_seen_while_untrusted"] is False


def test_the_observation_never_changes_the_address_that_is_resolved(hops):
    """Evidence, never acted on: the header is client-settable, so raising the flag must not
    also start trusting it. This is the property that keeps a false positive harmless."""
    hops(0)
    assert _address("10.0.0.2", "198.51.100.9") == "10.0.0.2"
    assert proxy_configuration_report()["forwarded_for_seen_while_untrusted"] is True
    # Still the peer on the next request, too — the flag is a report, not a mode switch.
    assert _address("10.0.0.3", "198.51.100.9") == "10.0.0.3"


def test_it_warns_once_and_not_per_request(hops, caplog):
    """This fires on every single request once a proxy is in front. A warning per request is
    how an operator learns to filter the warning out."""
    hops(0)
    with caplog.at_level(logging.WARNING, logger="app.core.client_address"):
        for _ in range(5):
            _address("10.0.0.2", "203.0.113.7")
    warnings = [r for r in caplog.records if "TRUSTED_PROXY_HOPS" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_the_health_endpoint_reports_it(auth_client, monkeypatch):
    """Where an operator actually finds it: the same probe that reports the database and the
    LLM providers, since this is a deployment fault of exactly that kind."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    await auth_client.post(
        "/api/v1/auth/signup",
        json={"email": "proxyreport@example.com", "password": "password123"},
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    body = (await auth_client.get("/health/dependencies")).json()
    assert body["proxy_configuration"] == {
        "trusted_proxy_hops": 0,
        "forwarded_for_seen_while_untrusted": True,
    }
