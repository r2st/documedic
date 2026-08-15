"""The password-reset routes must not answer in latency what they refuse to answer in words.

Both routes are written to be unreadable from the outside. ``/password-reset/request`` is
always 202 with the same body, whether the address has an account, has none, or has asked too
often; ``/password-reset/confirm`` returns one 401 and one message for all four ways a token
can be bad, and :class:`~app.exceptions.InvalidResetTokenError` says why in as many words —
"already used" in particular would confirm the address is a real account someone is actively
resetting.

Every one of those promises was made in the response body. None was made in the clock, and the
branches are not close: issuing a token costs a count, an invalidation sweep, an insert, an
audit row and a commit, against one indexed SELECT for an address with no account. Measured on
SQLite in-process before the fix, ``request`` split 2.1x and ``confirm`` 1.9x — and every extra
step is a network round-trip against the PostgreSQL this actually deploys against, so the gap
in the deployment is wider than the one on this bench, not narrower.

``login`` gets this right a different way (a bcrypt verification against
``_DUMMY_PASSWORD_HASH``, so the unknown-email branch burns the same CPU), and there is a test
here holding it to that, because it is the same property and it should fail in the same file if
it ever regresses.
"""

from __future__ import annotations

import asyncio
import statistics
import time

import pytest

from app.config import settings
from app.core.timing import with_minimum_duration

REQUEST = "/api/v1/auth/password-reset/request"
CONFIRM = "/api/v1/auth/password-reset/confirm"


async def _median_ms(call, samples: int = 9) -> float:
    """Median wall-clock of ``call``, after a warm-up that is not measured.

    Median rather than mean: the first call through a route pays for imports and statement
    compilation the rest do not, and one such outlier moves a mean by more than the effect
    being measured.
    """
    for _ in range(2):
        await call()
    timings = []
    for _ in range(samples):
        start = time.perf_counter()
        await call()
        timings.append((time.perf_counter() - start) * 1000)
    return statistics.median(timings)


# How far apart two branches of the same route may be, as a ratio of the faster to the slower.
#
# A ratio and not a millisecond difference, which is the whole reason these tests are worth
# having. The real gap between the branches is about a millisecond on SQLite — smaller than the
# noise on a loaded CI box — so an absolute threshold wide enough not to flake is also wide
# enough to pass on the unfixed code, which is what a first draft of this file did. What the
# fix actually produces is two branches both dominated by the same 250ms floor, so their *ratio*
# collapses to ~1.0 while noise stays a low single-digit percent of it. Unfixed, the same ratios
# are 2.1x, 1.9x and 6.4x.
_MAX_BRANCH_RATIO = 1.25


def _assert_indistinguishable(slower_label: str, slower: float, faster_label: str, faster: float):
    ratio = max(slower, faster) / max(min(slower, faster), 0.001)
    assert ratio < _MAX_BRANCH_RATIO, (
        f"{slower_label} answered in {slower:.1f}ms and {faster_label} in {faster:.1f}ms "
        f"({ratio:.2f}x) — the route reports which branch it took by how long it takes"
    )


# --- The floor itself ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_floor_pads_a_fast_call_up_to_it():
    async def quick() -> str:
        return "done"

    start = time.perf_counter()
    result = await with_minimum_duration(0.2, quick, label="test")
    elapsed = time.perf_counter() - start

    assert result == "done"
    assert elapsed >= 0.2, f"returned after {elapsed:.3f}s, inside its own floor"


@pytest.mark.asyncio
async def test_the_floor_pads_the_branch_that_raises_too():
    """The point of padding in a ``finally``, not a nicety.

    On both routes this protects, the *fast* branch is the one that raises: an unknown reset
    token is refused after a single SELECT while a valid one goes on to hash a password and
    revoke every session. Padding only the returning branch would leave the cheap branch
    unpadded and the endpoint exactly as readable as before.
    """

    async def refuse() -> None:
        raise ValueError("nope")

    start = time.perf_counter()
    with pytest.raises(ValueError):
        await with_minimum_duration(0.2, refuse, label="test")
    elapsed = time.perf_counter() - start

    assert elapsed >= 0.2, f"the raising branch returned after {elapsed:.3f}s, unpadded"


@pytest.mark.asyncio
async def test_the_floor_never_shortens_work_that_outran_it():
    """Padding is only ever added. A floor that truncated would be a correctness bug."""

    async def slow() -> str:
        await asyncio.sleep(0.15)
        return "done"

    start = time.perf_counter()
    assert await with_minimum_duration(0.05, slow, label="test") == "done"
    assert (time.perf_counter() - start) >= 0.15


@pytest.mark.asyncio
async def test_a_floor_that_stopped_covering_its_route_says_so(caplog):
    """The floor equalises the branches only while it exceeds both of them.

    When real work outgrows it the anti-enumeration property is gone and the responses are
    still perfectly correct, so nothing else in the system would ever mention it.
    """

    async def slow() -> None:
        await asyncio.sleep(0.05)

    with caplog.at_level("WARNING"):
        await with_minimum_duration(0.01, slow, label="POST /somewhere")

    assert any("constant-time floor" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_a_zero_floor_is_off_not_instant():
    """Zero opts a test out; it must not turn into a floor of "no time at all"."""

    async def quick() -> str:
        return "done"

    assert await with_minimum_duration(0, quick, label="test") == "done"


# --- The routes ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requesting_a_reset_takes_the_same_time_for_a_stranger(client):
    """The enumeration oracle itself: "does this address have an account?", asked in latency."""
    signed_up = await client.post(
        "/api/v1/auth/signup", json={"email": "known@example.com", "password": "password123"}
    )
    assert signed_up.status_code == 201, signed_up.text

    async def ask(email: str):
        async def call():
            resp = await client.post(REQUEST, json={"email": email})
            assert resp.status_code == 202, resp.text

        return await _median_ms(call)

    known = await ask("known@example.com")
    unknown = await ask("nobody-here@example.com")

    _assert_indistinguishable("an address with an account", known, "a stranger", unknown)


@pytest.mark.asyncio
async def test_requesting_a_reset_takes_the_same_time_once_throttled(client, monkeypatch):
    """The third branch, and the one that is easiest to forget.

    Past ``password_reset_max_requests_per_hour`` the known-address path returns early — after
    the count, before the insert — which is a distinct duration from both of the others. It
    would tell an attacker not only that the address is real but that someone is resetting it.
    """
    monkeypatch.setattr(settings, "password_reset_max_requests_per_hour", 2)
    signed_up = await client.post(
        "/api/v1/auth/signup", json={"email": "throttled@example.com", "password": "password123"}
    )
    assert signed_up.status_code == 201, signed_up.text

    async def call_known():
        resp = await client.post(REQUEST, json={"email": "throttled@example.com"})
        assert resp.status_code == 202, resp.text

    async def call_unknown():
        resp = await client.post(REQUEST, json={"email": "nobody-here@example.com"})
        assert resp.status_code == 202, resp.text

    # Burn the budget, so every sample below takes the throttled branch.
    for _ in range(4):
        await call_known()

    throttled = await _median_ms(call_known)
    unknown = await _median_ms(call_unknown)

    _assert_indistinguishable("an address over its ceiling", throttled, "a stranger", unknown)


@pytest.mark.asyncio
async def test_confirming_takes_the_same_time_for_a_spent_token_and_an_invented_one(
    client, monkeypatch
):
    """The distinction :class:`InvalidResetTokenError` is written to withhold.

    Its docstring is explicit that "already used" must not be distinguishable from "no such
    token", because it confirms the address is a live account mid-reset. A spent token costs an
    audit row and a commit that an invented one does not.
    """
    monkeypatch.setattr(settings, "password_reset_delivery", "response")
    signed_up = await client.post(
        "/api/v1/auth/signup", json={"email": "spender@example.com", "password": "password123"}
    )
    assert signed_up.status_code == 201, signed_up.text
    asked = await client.post(REQUEST, json={"email": "spender@example.com"})
    token = asked.json()["reset_token"]
    assert token, "demo delivery returns the token in the body; without it this proves nothing"

    spent = await client.post(
        CONFIRM, json={"token": token, "new_password": "a-much-better-password"}
    )
    assert spent.status_code == 200, spent.text

    async def confirm(candidate: str):
        async def call():
            resp = await client.post(
                CONFIRM, json={"token": candidate, "new_password": "another-password-again"}
            )
            assert resp.status_code == 401, resp.text

        return await _median_ms(call)

    real_but_spent = await confirm(token)
    never_existed = await confirm("x" * len(token))

    _assert_indistinguishable(
        "a real but already-spent token", real_but_spent, "an invented one", never_existed
    )


@pytest.mark.asyncio
async def test_a_successful_reset_is_not_the_fast_one_either(client, monkeypatch):
    """Confirm's branches run the other way round: success is the expensive path.

    A token that works hashes a password and revokes every session for the account, so without
    the floor the *200* is the slow response and every failure is the fast one — which reads,
    from the outside, as "that token was real".
    """
    monkeypatch.setattr(settings, "password_reset_delivery", "response")

    async def one_reset(email: str) -> float:
        signed_up = await client.post(
            "/api/v1/auth/signup", json={"email": email, "password": "password123"}
        )
        assert signed_up.status_code == 201, signed_up.text
        asked = await client.post(REQUEST, json={"email": email})
        token = asked.json()["reset_token"]
        start = time.perf_counter()
        done = await client.post(
            CONFIRM, json={"token": token, "new_password": "a-much-better-password"}
        )
        elapsed = (time.perf_counter() - start) * 1000
        assert done.status_code == 200, done.text
        return elapsed

    # Each token is single-use, so a successful confirm cannot be sampled in a loop against one
    # account — every measurement needs a fresh account and a fresh token.
    successes = [await one_reset(f"resetter{n}@example.com") for n in range(3)]

    async def call_failure():
        resp = await client.post(
            CONFIRM, json={"token": "y" * 43, "new_password": "another-password-again"}
        )
        assert resp.status_code == 401, resp.text

    failure = await _median_ms(call_failure)
    success = statistics.median(successes)

    _assert_indistinguishable("a token that worked", success, "one that did not", failure)


@pytest.mark.asyncio
async def test_the_floor_is_actually_applied_to_both_routes(client, monkeypatch):
    """Guards the wiring, not the arithmetic.

    The two assertions above compare the routes against each other, so a floor removed from
    *both* would leave them agreeing with each other at 1ms and passing. This one holds each
    route to the configured floor in absolute terms.
    """
    monkeypatch.setattr(settings, "password_reset_min_response_seconds", 0.3)

    start = time.perf_counter()
    resp = await client.post(REQUEST, json={"email": "anyone@example.com"})
    assert resp.status_code == 202
    assert (time.perf_counter() - start) >= 0.3, "the request route answered inside its floor"

    start = time.perf_counter()
    resp = await client.post(CONFIRM, json={"token": "z" * 43, "new_password": "password1234"})
    assert resp.status_code == 401
    assert (time.perf_counter() - start) >= 0.3, "the confirm route answered inside its floor"


@pytest.mark.asyncio
async def test_login_still_burns_bcrypt_on_an_unknown_email(client, monkeypatch):
    """``login`` solves the same problem the other way; pinned here so both stay solved.

    It runs a bcrypt verification against ``_DUMMY_PASSWORD_HASH`` when there is no account, so
    the unknown-email branch costs what the wrong-password branch costs. That works there
    because the asymmetry is one expensive call, which is exactly why it does not generalise to
    the reset routes.
    """
    # The lockout would otherwise refuse the later samples and measure a different branch.
    monkeypatch.setattr(settings, "login_max_failed_attempts", 10_000)
    monkeypatch.setattr(settings, "login_max_failed_attempts_per_ip", 10_000)
    signed_up = await client.post(
        "/api/v1/auth/signup", json={"email": "signin@example.com", "password": "password123"}
    )
    assert signed_up.status_code == 201, signed_up.text

    async def attempt(email: str):
        async def call():
            resp = await client.post(
                "/api/v1/auth/login", json={"email": email, "password": "not-the-password"}
            )
            assert resp.status_code == 401, resp.text

        return await _median_ms(call)

    known = await attempt("signin@example.com")
    unknown = await attempt("nobody-here@example.com")

    # Relative, because bcrypt's cost is what both branches are paying and it varies with the
    # configured rounds. Anything under 2x means the dummy hash is doing its job; a branch that
    # skipped bcrypt entirely would come back an order of magnitude faster.
    ratio = max(known, unknown) / max(min(known, unknown), 0.001)
    assert ratio < 2.0, (
        f"an unknown email answered in {unknown:.1f}ms and a known one in {known:.1f}ms — "
        "the unknown-email branch is no longer paying for a bcrypt verification"
    )
