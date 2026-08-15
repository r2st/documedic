"""Completing a password reset must let the clinician back in.

The login lockout counts failed attempts against an email and refuses the sign-in *before* the
password is verified, and only an ``auth_login_success`` row clears the budget. Nothing else
does — so the one remedy the product offers for a forgotten password did not restore access.

The loop that produces is not an edge case, it is the ordinary path: forgetting a password is
what generates failed attempts, so the clinician most likely to reach the reset flow is
precisely the one already locked out. They complete the reset, are told "Sign in with your new
password", and are refused with a message about failed attempts they cannot act on — the new
password is correct and being rejected without being looked at. Fifteen minutes into a
consultation, with no way to tell that waiting is the answer.

Proving possession of a reset token is a stronger claim on an account than knowing its
password, so it clears the email budget for at least everything a successful sign-in clears.

The address budget is deliberately *not* cleared, for the reason it is counted separately in
the first place: it is shared. Behind the reference nginx front end every clinician reports one
address, so an attacker who owns any account there — their own is enough — could reset its
password on demand and wipe the guessing record for every other account behind it. A control
that anyone in range can clear at will is not a control.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.audit_log import AuditLog

PREFIX = "/api/v1/auth"
OLD_PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "a-different-password-9"


@pytest.fixture(autouse=True)
def _tight_budgets(monkeypatch):
    """Small budgets, and a token in the response so the flow runs end to end over HTTP."""
    monkeypatch.setattr(settings, "login_max_failed_attempts", 3)
    monkeypatch.setattr(settings, "login_max_failed_attempts_per_ip", 50)
    monkeypatch.setattr(settings, "login_attempt_window_minutes", 15)
    monkeypatch.setattr(settings, "login_lockout_minutes", 15)
    monkeypatch.setattr(settings, "password_reset_delivery", "response")


async def _signup(client, email: str) -> None:
    resp = await client.post(
        f"{PREFIX}/signup", json={"email": email, "password": OLD_PASSWORD, "display_name": "Dr X"}
    )
    assert resp.status_code == 201, resp.text


async def _login(client, email: str, password: str):
    return await client.post(f"{PREFIX}/login", json={"email": email, "password": password})


async def _lock_out(client, email: str) -> None:
    """Burn the email budget, and confirm the account is actually refused."""
    for _ in range(settings.login_max_failed_attempts):
        assert (await _login(client, email, "wrong-password")).status_code == 401
    assert (await _login(client, email, OLD_PASSWORD)).status_code == 429


async def _reset(client, email: str, new_password: str = NEW_PASSWORD) -> None:
    """Request a token and spend it."""
    requested = await client.post(f"{PREFIX}/password-reset/request", json={"email": email})
    assert requested.status_code == 202, requested.text
    token = requested.json()["reset_token"]
    assert token, "the reset flow must hand back a token in this configuration"
    confirmed = await client.post(
        f"{PREFIX}/password-reset/confirm", json={"token": token, "new_password": new_password}
    )
    assert confirmed.status_code == 200, confirmed.text


# --- The regression ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_locked_out_clinician_can_sign_in_after_resetting_their_password(client):
    """The whole point: the remedy for a forgotten password restores access to the account."""
    email = "locked-out@example.com"
    await _signup(client, email)
    await _lock_out(client, email)

    await _reset(client, email)

    resp = await _login(client, email, NEW_PASSWORD)
    assert resp.status_code == 200, (
        "a completed reset must clear the email lockout budget — otherwise the only remedy the "
        f"product offers for a forgotten password does not restore access (got {resp.status_code})"
    )


@pytest.mark.asyncio
async def test_the_lockout_is_lifted_rather_than_the_password_check_skipped(client):
    """Clearing the budget must not also stop the new password from being verified.

    A lockout lifted by turning the sign-in into a no-op would be a far worse bug than the one
    being fixed, so the wrong password must still be refused — and refused as a *credential*
    failure (401), not as a lockout (429).
    """
    email = "still-checked@example.com"
    await _signup(client, email)
    await _lock_out(client, email)

    await _reset(client, email)

    assert (await _login(client, email, "not-the-new-password")).status_code == 401
    assert (await _login(client, email, OLD_PASSWORD)).status_code == 401
    assert (await _login(client, email, NEW_PASSWORD)).status_code == 200


@pytest.mark.asyncio
async def test_failures_after_the_reset_lock_the_account_again(client):
    """The budget is cleared, not disabled: guessing at the *new* password still locks out.

    Otherwise one reset would buy an attacker an unlimited guessing run, which trades the
    availability bug for a confidentiality one.
    """
    email = "relockable@example.com"
    await _signup(client, email)
    await _lock_out(client, email)
    await _reset(client, email)
    assert (await _login(client, email, NEW_PASSWORD)).status_code == 200

    for _ in range(settings.login_max_failed_attempts):
        assert (await _login(client, email, "wrong-again")).status_code == 401
    assert (await _login(client, email, NEW_PASSWORD)).status_code == 429


# --- What must not be cleared -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reset_does_not_clear_the_shared_address_budget(client):
    """One account's reset must not wipe the guessing record for everyone behind the address.

    Every request from the test client reports one address, exactly as every clinician behind
    the reference nginx front end does. If a reset cleared that budget, an attacker holding any
    account on the deployment could lift the distributed-guessing control at will.
    """
    settings.login_max_failed_attempts_per_ip = 6
    victims = [f"bystander-{i}@example.com" for i in range(3)]
    for email in victims:
        await _signup(client, email)
    attacker = "attacker@example.com"
    await _signup(client, attacker)

    # Spread failures across accounts so the address budget is what trips, not any one email's.
    for email in victims:
        for _ in range(2):
            assert (await _login(client, email, "wrong-password")).status_code == 401

    await _reset(client, attacker)

    resp = await _login(client, victims[0], OLD_PASSWORD)
    assert resp.status_code == 429, (
        "the address budget is shared, so a reset on one account must not clear it "
        f"(got {resp.status_code})"
    )


@pytest.mark.asyncio
async def test_requesting_a_reset_does_not_clear_the_lockout(client):
    """Only *completing* a reset counts — the request is unauthenticated.

    Anyone can POST an address to the request endpoint; it proves nothing about who sent it. If
    the request cleared the budget, the lockout could be lifted by the same person guessing at
    the password, which is the entire population the control is aimed at.
    """
    email = "request-only@example.com"
    await _signup(client, email)
    await _lock_out(client, email)

    requested = await client.post(f"{PREFIX}/password-reset/request", json={"email": email})
    assert requested.status_code == 202

    resp = await _login(client, email, OLD_PASSWORD)
    assert resp.status_code == 429, (
        "an unauthenticated request must not lift the lockout — only spending the token does "
        f"(got {resp.status_code})"
    )


@pytest.mark.asyncio
async def test_the_lockout_lifts_however_the_clinician_capitalised_their_address(client):
    """The barrier is matched on an exact string, so case must not decide whether it is found.

    Three separate points take the address as typed — the failed sign-ins, the reset request,
    and the sign-in afterwards — and a clinician has no reason to capitalise the same way at
    each. Accounts are stored lowercased and every one of those paths normalises to that, which
    is what makes the match hold; a change to any of them would silently restore the lockout
    for anyone who typed a capital letter, and only for them.
    """
    email = "Mixed.Case@Example.com"
    await _signup(client, email)
    for _ in range(settings.login_max_failed_attempts):
        assert (await _login(client, email.upper(), "wrong-password")).status_code == 401
    assert (await _login(client, email, OLD_PASSWORD)).status_code == 429

    await _reset(client, email.lower())

    assert (await _login(client, email.upper(), NEW_PASSWORD)).status_code == 200


# --- The audit trail --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_completed_reset_is_still_recorded_against_the_account(db, client):
    """The row that clears the budget is the one the security trail already keeps.

    Nothing here adds a way to lift a lockout that is invisible afterwards: the clearing event
    is ``auth_password_reset_completed``, which is written for every reset and never pruned.
    """
    email = "audited@example.com"
    await _signup(client, email)
    await _lock_out(client, email)
    await _reset(client, email)

    rows = (
        (
            await db.execute(
                select(AuditLog).where(AuditLog.action == "auth_password_reset_completed")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload.get("email") == email, (
        "the email is what scopes this row to the account whose budget it clears"
    )
