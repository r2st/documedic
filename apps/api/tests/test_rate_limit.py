"""Rate limiting: the sliding window itself, the wiring, and what is deliberately unmetered.

Two layers, tested separately because they fail differently. The window is pure logic with an
injected clock, so it is tested exactly (no sleeps, no flakiness). The wiring is tested through
the HTTP surface, where what matters is which routes are metered, which are not, and that the
429 carries what a client and a clinician each need.
"""

from __future__ import annotations

import io

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.rate_limit import (
    RateLimit,
    SlidingWindowLimiter,
    retry_after_seconds,
)
from app.dependencies import limit_for, limiter
from tests.conftest import create_patient


class FakeClock:
    """A hand-advanced monotonic clock, so window expiry is asserted rather than waited for."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- The sliding window ---------------------------------------------------------------------


def test_allows_up_to_the_ceiling_then_refuses():
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    limit = RateLimit(max_requests=3, window_seconds=60)

    assert [lim.check("k", limit) for _ in range(3)] == [None, None, None]
    assert lim.check("k", limit) is not None


def test_refused_request_is_not_counted():
    """A rejection must not extend its own window.

    If a blocked request recorded itself, a client polling at the limit would push its retry
    further away with every attempt and never recover — it would be a permanent lockout dressed
    up as a rate limit. So the 4th call below is refused, and once the window has passed the
    caller gets its full budget back rather than a queue of its own rejections.
    """
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    limit = RateLimit(max_requests=2, window_seconds=60)

    lim.check("k", limit)
    lim.check("k", limit)
    for _ in range(20):  # hammering while blocked
        clock.advance(1)
        assert lim.check("k", limit) is not None

    # The two real hits are now 20s+ old; the window is 60s, so still blocked...
    assert lim.check("k", limit) is not None
    # ...and clears exactly when the *first* real hit ages out, not later.
    clock.advance(60)
    assert lim.check("k", limit) is None


def test_window_slides_rather_than_resetting_in_fixed_buckets():
    """The ceiling holds across any 60s span, not just aligned ones.

    A fixed-bucket counter would let a caller spend its whole budget at the end of one bucket
    and again at the start of the next — double the intended rate, at exactly the moment a retry
    storm produces it.
    """
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    limit = RateLimit(max_requests=2, window_seconds=60)

    assert lim.check("k", limit) is None  # t=0
    clock.advance(59)
    assert lim.check("k", limit) is None  # t=59
    clock.advance(2)  # t=61: the t=0 hit has expired, the t=59 hit has not
    assert lim.check("k", limit) is None
    assert lim.check("k", limit) is not None  # two live hits (t=59, t=61)


def test_retry_after_counts_down_to_the_oldest_hit_expiring():
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    limit = RateLimit(max_requests=1, window_seconds=60)

    lim.check("k", limit)
    clock.advance(10)
    assert lim.check("k", limit) == pytest.approx(50.0)
    clock.advance(45)
    assert lim.check("k", limit) == pytest.approx(5.0)


def test_keys_are_independent():
    lim = SlidingWindowLimiter(clock=FakeClock())
    limit = RateLimit(max_requests=1, window_seconds=60)

    assert lim.check("a", limit) is None
    assert lim.check("b", limit) is None  # b is unaffected by a's spent budget
    assert lim.check("a", limit) is not None


def test_key_table_is_bounded_and_evicts_least_recently_seen(caplog):
    """Memory is capped, and lifting a live ceiling to stay capped is logged, not silent."""
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock, max_tracked_keys=3)
    limit = RateLimit(max_requests=1, window_seconds=60)

    for key in ("a", "b", "c"):
        lim.check(key, limit)
    assert lim.tracked_keys == 3

    with caplog.at_level("WARNING"):
        lim.check("d", limit)

    assert lim.tracked_keys == 3
    # "a" was the least recently touched, so it lost its window and gets a fresh budget.
    assert lim.check("a", limit) is None
    # "c" is still tracked and still spent.
    assert lim.check("c", limit) is not None
    assert "cap" in caplog.text


def test_eviction_prefers_the_key_that_has_not_been_seen(caplog):
    """Touching a key makes it recently-used, so it is not the one dropped."""
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock, max_tracked_keys=2)
    limit = RateLimit(max_requests=2, window_seconds=60)

    lim.check("a", limit)
    lim.check("b", limit)
    lim.check("a", limit)  # a is now the most recent, b the least
    lim.check("c", limit)  # evicts b

    assert lim.check("a", limit) is not None  # a kept its two spent hits
    assert lim.check("b", limit) is None  # b was forgotten


def test_reset_clears_every_window():
    lim = SlidingWindowLimiter(clock=FakeClock())
    limit = RateLimit(max_requests=1, window_seconds=60)
    lim.check("k", limit)
    lim.reset()
    assert lim.tracked_keys == 0
    assert lim.check("k", limit) is None


def test_clock_default_is_monotonic_not_wall_clock():
    """A wall-clock source would break on an NTP step; assert the default is immune.

    Constructed with no clock argument, two successive reads must never go backwards — which
    ``time.time()`` can do and ``time.monotonic()`` cannot.
    """
    lim = SlidingWindowLimiter()
    limit = RateLimit(max_requests=5, window_seconds=60)
    for _ in range(5):
        assert lim.check("k", limit) is None
    assert lim.check("k", limit) is not None


@pytest.mark.parametrize("bad", [0, -1])
def test_zero_or_negative_ceiling_is_rejected_at_construction(bad):
    """A limit of 0 would reject every request; 'off' is expressed as no RateLimit at all."""
    with pytest.raises(ValueError, match="max_requests"):
        RateLimit(max_requests=bad, window_seconds=60)


@pytest.mark.parametrize("bad", [0, -5.0])
def test_non_positive_window_is_rejected(bad):
    with pytest.raises(ValueError, match="window_seconds"):
        RateLimit(max_requests=1, window_seconds=bad)


@pytest.mark.parametrize(
    ("wait", "expected"),
    [
        (0.0, 1),  # never advertise "retry now" -- that is another immediate 429
        (0.1, 1),
        (1.0, 1),
        (1.2, 2),  # rounds up: a truncated wait retries too early
        (59.9, 60),
    ],
)
def test_retry_after_rounds_up_and_never_reaches_zero(wait, expected):
    assert retry_after_seconds(wait) == expected


# --- Bucket configuration -------------------------------------------------------------------


def test_every_declared_bucket_resolves_to_a_limit():
    """Guards against a bucket name that is wired to a route but has no setting behind it."""
    from app.dependencies import _BUCKETS

    for bucket in _BUCKETS:
        limit = limit_for(bucket)
        assert limit is not None, f"{bucket} has a non-positive default ceiling"
        assert limit.max_requests >= 1


def test_unknown_bucket_fails_loudly():
    with pytest.raises(KeyError, match="Unknown rate-limit bucket"):
        limit_for("no_such_bucket")


def test_rate_limit_enabled_false_disables_every_bucket(monkeypatch):
    monkeypatch.setattr("app.config.settings.rate_limit_enabled", False)
    assert limit_for("reasoning_run") is None
    assert limit_for("signup") is None


def test_a_single_bucket_can_be_disabled_without_the_others(monkeypatch):
    monkeypatch.setattr("app.config.settings.rate_limit_uploads_per_minute", 0)
    assert limit_for("document_upload") is None
    assert limit_for("reasoning_run") is not None


# --- HTTP wiring ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_start_is_metered_and_the_429_is_actionable(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.settings.rate_limit_intake_per_minute", 2)
    patient = await create_patient(auth_client)
    url = f"/api/v1/patients/{patient['id']}/reasoning"
    body = {"presenting_complaint": "chest pain for two days"}

    for _ in range(2):
        assert (await auth_client.post(url, json=body)).status_code == 201

    resp = await auth_client.post(url, json=body)
    assert resp.status_code == 429
    payload = resp.json()
    # A distinct code from the sign-in lockout: a client must be able to tell "back off" from
    # "this account is locked, sign in again".
    assert payload["code"] == "rate_limited"
    # Retry-After is the machine-readable contract.
    assert int(resp.headers["Retry-After"]) >= 1
    # ...and the prose answers the two questions a clinician actually has.
    assert "nothing was saved" in payload["message"]
    assert "drug-safety checks are unaffected" in payload["message"]


@pytest.mark.asyncio
async def test_the_limit_is_per_account_not_global(auth_client, second_auth_client, monkeypatch):
    """One clinician exhausting their budget must not throttle a colleague.

    The reason the key is the account and not the client address: a hospital's clinicians share
    one egress IP, so an address key would put a whole department on one budget.
    """
    monkeypatch.setattr("app.config.settings.rate_limit_intake_per_minute", 1)
    body = {"presenting_complaint": "fever and cough"}

    mine = await create_patient(auth_client)
    assert (
        await auth_client.post(f"/api/v1/patients/{mine['id']}/reasoning", json=body)
    ).status_code == 201
    assert (
        await auth_client.post(f"/api/v1/patients/{mine['id']}/reasoning", json=body)
    ).status_code == 429

    theirs = await create_patient(second_auth_client)
    assert (
        await second_auth_client.post(f"/api/v1/patients/{theirs['id']}/reasoning", json=body)
    ).status_code == 201


@pytest.mark.asyncio
async def test_run_and_stream_share_one_budget(auth_client, monkeypatch):
    """The two routes drive the same eight-agent panel, so they must not each get a full budget.

    Otherwise a client halves its effective cost per call simply by alternating them.
    """
    monkeypatch.setattr("app.config.settings.rate_limit_reasoning_runs_per_minute", 1)
    patient = await create_patient(auth_client)
    start = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "breathless on exertion"},
    )
    session_id = start.json()["session"]["id"]

    assert (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).status_code == 200
    # The single run consumed the shared budget, so the stream is refused too.
    resp = await auth_client.get(f"/api/v1/reasoning/{session_id}/stream")
    assert resp.status_code == 429
    assert resp.json()["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_stream_is_metered_even_when_authenticated_by_stream_token(auth_client, monkeypatch):
    """The SSE route accepts a `?token=` stream token, and that path is metered too.

    This is the route the ceiling exists for: a browser's EventSource reconnects on every
    transport error, so an abandoned tab re-runs the whole panel on a loop. If only the bearer
    path were metered, the reconnect loop — which is exactly the query-token path — would be the
    one hole left open.
    """
    monkeypatch.setattr("app.config.settings.rate_limit_reasoning_runs_per_minute", 1)
    patient = await create_patient(auth_client)
    start = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "palpitations"},
    )
    session_id = start.json()["session"]["id"]
    minted = await auth_client.post(f"/api/v1/reasoning/{session_id}/stream-token")
    token = minted.json()["token"]

    stream_url = f"/api/v1/reasoning/{session_id}/stream?token={token}"
    # A client with no Authorization header at all, as EventSource would be.
    no_header = {"Authorization": ""}
    first = await auth_client.get(stream_url, headers=no_header)
    assert first.status_code == 200
    second = await auth_client.get(stream_url, headers=no_header)
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_unauthenticated_requests_do_not_spend_an_account_budget(
    app, auth_client, monkeypatch
):
    """Auth is resolved before metering, so an anonymous flood cannot exhaust a real clinician.

    The `rate_limit` dependency asks for the account, so FastAPI resolves the token first and an
    anonymous request 401s before touching a bucket. Were the order reversed, anyone who could
    reach the API could deny a named clinician their reasoning budget for the rest of the window
    without ever presenting a credential — a denial of service on the clinical path, from
    outside the fence.

    Its own client, deliberately: ``auth_client`` *is* the ``client`` fixture with a header set
    on it, so reusing it here would send the bearer token and test nothing.
    """
    monkeypatch.setattr("app.config.settings.rate_limit_intake_per_minute", 1)
    patient = await create_patient(auth_client)
    url = f"/api/v1/patients/{patient['id']}/reasoning"
    body = {"presenting_complaint": "abdominal pain"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        for _ in range(5):
            resp = await anon.post(url, json=body)
            assert resp.status_code == 401, resp.text

    # The account's own budget is untouched by the flood aimed at its chart.
    assert (await auth_client.post(url, json=body)).status_code == 201


@pytest.mark.asyncio
async def test_document_upload_is_metered(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.settings.rate_limit_uploads_per_minute", 1)
    patient = await create_patient(auth_client)
    url = f"/api/v1/patients/{patient['id']}/documents"

    def png() -> dict:
        # Minimal PNG magic bytes -- the upload path decides the type from these, not the name.
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        return {"file": ("scan.png", io.BytesIO(data), "image/png")}

    assert (await auth_client.post(url, files=png())).status_code == 201
    resp = await auth_client.post(url, files=png())
    assert resp.status_code == 429
    assert resp.json()["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_guideline_search_is_metered(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.settings.rate_limit_searches_per_minute", 1)
    url = "/api/v1/guidelines/search?q=metformin+in+renal+impairment"

    assert (await auth_client.get(url)).status_code == 200
    assert (await auth_client.get(url)).status_code == 429


@pytest.mark.asyncio
async def test_signup_is_metered_per_address(client, monkeypatch):
    """Unlimited account creation would walk straight around every per-account ceiling.

    Keyed by client address rather than account, because there is no account yet.
    """
    monkeypatch.setattr("app.config.settings.rate_limit_signups_per_hour", 2)
    for i in range(2):
        resp = await client.post(
            "/api/v1/auth/signup",
            json={
                "email": f"new{i}@example.com",
                "password": "password123",
                "display_name": f"Dr {i}",
            },
        )
        assert resp.status_code == 201, resp.text

    blocked = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": "new2@example.com",
            "password": "password123",
            "display_name": "Dr Three",
        },
    )
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "rate_limited"


# --- What must stay unmetered ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_deterministic_drug_safety_is_never_rate_limited(auth_client, monkeypatch):
    """A 429 on an allergy cross-check reads as "no conflict found" to a hurried clinician.

    These checks are local, rule-based and cheap — there is no upstream spend to protect — and
    CLAUDE.md rule 8 requires them to answer whenever asked, including offline. So they are
    deliberately absent from every bucket, and that absence is asserted rather than assumed:
    metering them later would be an easy, quiet, and dangerous change to make.

    Driven with every ceiling pinned to 1 so any accidental wiring would show up immediately.
    """
    for setting in (
        "rate_limit_reasoning_runs_per_minute",
        "rate_limit_intake_per_minute",
        "rate_limit_uploads_per_minute",
        "rate_limit_searches_per_minute",
    ):
        monkeypatch.setattr(f"app.config.settings.{setting}", 1)

    patient = await create_patient(auth_client)
    pid = patient["id"]

    for _ in range(12):
        check = await auth_client.post(
            f"/api/v1/patients/{pid}/drug-safety/check",
            json={"drug_name": "Crocin"},
        )
        assert check.status_code == 200, check.text
        flags = await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/flags")
        assert flags.status_code == 200, flags.text


@pytest.mark.asyncio
async def test_chart_reads_are_never_rate_limited(auth_client, monkeypatch):
    """Reading the record a clinician is treating from must not be throttled.

    It costs a query, not an LLM call, and a clinician mid-consultation refreshing a timeline is
    not abuse.
    """
    monkeypatch.setattr("app.config.settings.rate_limit_reasoning_runs_per_minute", 1)
    patient = await create_patient(auth_client)
    pid = patient["id"]

    for _ in range(15):
        assert (await auth_client.get(f"/api/v1/patients/{pid}")).status_code == 200
        assert (await auth_client.get(f"/api/v1/patients/{pid}/record")).status_code == 200
        assert (await auth_client.get(f"/api/v1/patients/{pid}/documents")).status_code == 200


@pytest.mark.asyncio
async def test_login_keeps_its_own_429_code_distinct_from_rate_limiting(client, monkeypatch):
    """The sign-in lockout and the capacity limiter share a status code, not a meaning.

    `too_many_attempts` is a security control that clears after minutes and is not fixed by
    retrying; `rate_limited` clears in seconds and is. A client that conflated them would either
    hammer a locked account or ask a merely-throttled clinician to sign in again.
    """
    monkeypatch.setattr("app.config.settings.login_max_failed_attempts", 2)
    await client.post(
        "/api/v1/auth/signup",
        json={"email": "lock@example.com", "password": "password123", "display_name": "Dr L"},
    )
    for _ in range(2):
        bad = await client.post(
            "/api/v1/auth/login",
            json={"email": "lock@example.com", "password": "wrong-password"},
        )
        assert bad.status_code == 401

    locked = await client.post(
        "/api/v1/auth/login",
        json={"email": "lock@example.com", "password": "password123"},
    )
    assert locked.status_code == 429
    assert locked.json()["code"] == "too_many_attempts"
    # No Retry-After: the wait is minutes and is not what the client should do next.
    assert "Retry-After" not in locked.headers


@pytest.mark.asyncio
async def test_disabling_rate_limiting_restores_unmetered_behaviour(auth_client, monkeypatch):
    """The kill switch works, for a deployment that terminates limits at its front end."""
    monkeypatch.setattr("app.config.settings.rate_limit_enabled", False)
    monkeypatch.setattr("app.config.settings.rate_limit_intake_per_minute", 1)
    patient = await create_patient(auth_client)
    url = f"/api/v1/patients/{patient['id']}/reasoning"

    for _ in range(4):
        resp = await auth_client.post(url, json={"presenting_complaint": "headache"})
        assert resp.status_code == 201


@pytest.mark.asyncio
async def test_limiter_state_does_not_leak_between_tests(auth_client, monkeypatch):
    """Companion to the autouse reset fixture in conftest.

    If this ever fails, the fixture stopped running and every rate-limit assertion in the suite
    became order-dependent — the failures would land in unrelated tests, in an order-dependent
    way, which is the worst possible place to debug them from.

    The only key that may exist on entry is the signup bucket, spent once by the ``auth_client``
    fixture itself. Anything more means state survived from an earlier test.
    """
    assert limiter.tracked_keys <= 1, "rate-limit state leaked in from a previous test"
    monkeypatch.setattr("app.config.settings.rate_limit_intake_per_minute", 1)
    patient = await create_patient(auth_client)
    url = f"/api/v1/patients/{patient['id']}/reasoning"
    assert (await auth_client.post(url, json={"presenting_complaint": "rash"})).status_code == 201
    assert (await auth_client.post(url, json={"presenting_complaint": "rash"})).status_code == 429
