"""The login lockout must stop credential guessing without locking out a hospital.

Two dimensions are counted, and the whole point of this file is that they are counted
*separately*:

* **email** — a tight budget, protecting one account from having its password guessed.
* **address** — a loose budget, catching guessing spread across many accounts from one source.

Pooling them, which is what the code did, is unsafe in the availability direction rather than
the confidentiality one. Every clinician on the reference deployment reaches the API through
one nginx front end (see :mod:`app.core.client_address`), so under a pooled counter eight
mistyped passwords anywhere in the hospital denied sign-in to everyone, holding the correct
password, for fifteen minutes. On a system a clinician opens mid-consultation that is the more
damaging failure of the two.

The other half is the *read* the budget is counted from. Scoped in SQL to one email or one
address, a busy deployment cannot age a guessing run out of the window simply by being busy —
and neither can an attacker doing it on purpose.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.audit_log import AuditLog

PASSWORD = "password123"


@pytest.fixture(autouse=True)
def tight_budgets(monkeypatch):
    """Small budgets so a test makes a handful of requests rather than dozens."""
    monkeypatch.setattr(settings, "login_max_failed_attempts", 3)
    monkeypatch.setattr(settings, "login_max_failed_attempts_per_ip", 8)
    monkeypatch.setattr(settings, "login_attempt_window_minutes", 15)
    monkeypatch.setattr(settings, "login_lockout_minutes", 15)


async def _signup(client, email):
    resp = await client.post("/api/v1/auth/signup", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 201, resp.text


async def _login(client, email, password=PASSWORD, **kwargs):
    return await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, **kwargs
    )


# --- The two budgets are separate -------------------------------------------------------


@pytest.mark.asyncio
async def test_one_accounts_failures_do_not_lock_out_a_colleague(client):
    """The regression this file exists for: a bystander with the right password still gets in.

    Every request from the test client reports the same address, exactly as every clinician
    behind the reference nginx front end does.
    """
    await _signup(client, "victim@example.com")
    await _signup(client, "bystander@example.com")

    for _ in range(settings.login_max_failed_attempts):
        assert (await _login(client, "victim@example.com", "wrong")).status_code == 401

    assert (await _login(client, "victim@example.com")).status_code == 429
    assert (await _login(client, "bystander@example.com")).status_code == 200


@pytest.mark.asyncio
async def test_a_shared_address_still_stops_guessing_spread_across_accounts(client):
    """The address budget is loose, not absent: enough failures from one source still lock it.

    An attacker who rotates the email to dodge the per-account budget spends this one instead,
    and it catches them without any single account having exhausted its own.
    """
    await _signup(client, "target@example.com")
    for index in range(settings.login_max_failed_attempts_per_ip):
        # A different email every time, so no email ever reaches its own budget of 3.
        resp = await _login(client, f"guess{index}@example.com", "wrong")
        assert resp.status_code == 401

    assert (await _login(client, "target@example.com")).status_code == 429


@pytest.mark.asyncio
async def test_the_address_budget_is_far_looser_than_the_account_budget(client):
    """Failures well past the per-account budget still leave a colleague signing in.

    Pins the ordering that makes the control safe on a shared egress address: honest typos
    from several clinicians accumulate on one key, and must not reach a lockout anywhere near
    as fast as a guessing run against one account does.
    """
    await _signup(client, "colleague@example.com")
    for index in range(settings.login_max_failed_attempts_per_ip - 1):
        assert (await _login(client, f"typo{index}@example.com", "wrong")).status_code == 401

    assert (await _login(client, "colleague@example.com")).status_code == 200


@pytest.mark.asyncio
async def test_the_address_arm_can_be_disabled_on_its_own(client, monkeypatch):
    """A deployment that cannot resolve real client addresses can switch the arm off entirely
    and keep the per-account control, which is the one that protects a password."""
    monkeypatch.setattr(settings, "login_max_failed_attempts_per_ip", 0)
    await _signup(client, "kept@example.com")
    for index in range(20):
        assert (await _login(client, f"noise{index}@example.com", "wrong")).status_code == 401
    assert (await _login(client, "kept@example.com")).status_code == 200

    # The per-account budget is untouched by that.
    for _ in range(settings.login_max_failed_attempts):
        await _login(client, "kept@example.com", "wrong")
    assert (await _login(client, "kept@example.com")).status_code == 429


@pytest.mark.asyncio
async def test_the_lockout_entry_names_which_budget_tripped(client, db):
    """An email lockout and an address lockout call for different operator responses, so the
    trail has to distinguish them rather than recording one undifferentiated event."""
    await _signup(client, "scoped@example.com")
    for _ in range(settings.login_max_failed_attempts + 1):
        await _login(client, "scoped@example.com", "wrong")

    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "auth_login_locked_out")))
        .scalars()
        .all()
    )
    assert rows, "the lockout was not audited"
    assert rows[-1].payload["scope"] == "email"


# --- The budget is counted from a read that cannot be flooded out of scope ---------------


@pytest.mark.asyncio
async def test_unrelated_sign_in_traffic_cannot_flush_a_guessing_run_out_of_the_window(
    client, monkeypatch
):
    """A deployment busier than the row cap must not silently stop locking anyone out.

    The read used to take the newest N auth rows across every account and filter them in
    Python, so once a window held more than N events the oldest — including the failures that
    should have tripped the lockout — fell off the end. That is both a capacity bug on a real
    hospital and a deliberate evasion: interleave junk attempts against other addresses and
    the budget resets. Scoping the read to one email in SQL makes the cap per-dimension, so
    only that email's own failures can consume it.
    """
    from app.services import auth_service

    # A cap of exactly the per-account budget: one unrelated failure would have been enough to
    # push a real one out of a pooled read.
    monkeypatch.setattr(auth_service, "_MAX_ATTEMPTS_SCANNED", 3)
    monkeypatch.setattr(settings, "login_max_failed_attempts_per_ip", 0)

    await _signup(client, "quiet@example.com")
    for _ in range(settings.login_max_failed_attempts):
        assert (await _login(client, "quiet@example.com", "wrong")).status_code == 401

    # Traffic from everyone else on the deployment, newer than the failures above.
    for index in range(10):
        await _login(client, f"busy{index}@example.com", "wrong")

    assert (await _login(client, "quiet@example.com")).status_code == 429


@pytest.mark.asyncio
async def test_a_successful_sign_in_still_clears_that_accounts_budget(client):
    """Unchanged behaviour, re-pinned because the read that implements it was rewritten."""
    await _signup(client, "reset@example.com")
    for _ in range(settings.login_max_failed_attempts - 1):
        await _login(client, "reset@example.com", "wrong")

    assert (await _login(client, "reset@example.com")).status_code == 200
    for _ in range(settings.login_max_failed_attempts - 1):
        assert (await _login(client, "reset@example.com", "wrong")).status_code == 401


@pytest.mark.asyncio
async def test_one_persons_success_does_not_clear_a_shared_addresss_budget(client):
    """The address arm deliberately does not stop at a success.

    Behind a shared egress address anyone signing in successfully would otherwise erase the
    record of a guessing run against everyone else there — which is trivially arranged by an
    attacker who holds one valid account on the same network.
    """
    await _signup(client, "insider@example.com")
    await _signup(client, "watched@example.com")

    half = settings.login_max_failed_attempts_per_ip // 2
    for index in range(half):
        await _login(client, f"a{index}@example.com", "wrong")
    assert (await _login(client, "insider@example.com")).status_code == 200
    for index in range(settings.login_max_failed_attempts_per_ip - half):
        await _login(client, f"b{index}@example.com", "wrong")

    assert (await _login(client, "watched@example.com")).status_code == 429
