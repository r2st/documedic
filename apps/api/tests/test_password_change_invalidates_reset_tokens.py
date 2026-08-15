"""Changing a password must retire the reset links that were outstanding when it happened.

``change_password`` revoked every other session — the whole reason a clinician changes a
password after a suspected compromise — and left the *other* credential that opens the account
untouched. A password-reset token is a bearer credential equal to the password: whoever holds
one can set the password and sign in. So the flow that exists to end someone else's access left
them a live, unspent way back in:

* The ordinary case is not an attack at all. A clinician forgets the password, asks for a reset
  link, then remembers it and signs in and changes the password the normal way. The link is
  still live in a mailbox — for ``PASSWORD_RESET_TOKEN_TTL_MINUTES`` — and still works.
* The case the feature is *for* is worse. Someone reaches an unlocked workstation, requests a
  reset for the account (the endpoint is unauthenticated and answers 202 to anyone), and waits.
  The clinician notices, changes the password, is told every other device was signed out — and
  the attacker spends their token, which sets a password of their choosing and, by
  ``reset_password``'s own design, signs the clinician out everywhere.

The fix is one line of intent: the same call that retires a superseded token when a *newer*
reset is requested also runs when the password is set by any other route. Asserted here from
both directions — the token stops working, and the audit trail says so.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.user import Account, PasswordResetToken

PREFIX = "/api/v1/auth"

ORIGINAL = "correct-horse-battery"
CHANGED = "a-different-passphrase-1"
ATTACKER = "attacker-chosen-passphrase"


@pytest.fixture(autouse=True)
def _token_in_the_response(monkeypatch):
    """Deliver the reset token in the response body so the flow is exercisable over HTTP."""
    monkeypatch.setattr(settings, "password_reset_delivery", "response")


async def _signup(client, email: str, password: str = ORIGINAL) -> dict:
    resp = await client.post(
        f"{PREFIX}/signup", json={"email": email, "password": password, "display_name": "Dr X"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _outstanding_token(client, email: str) -> str:
    resp = await client.post(f"{PREFIX}/password-reset/request", json={"email": email})
    assert resp.status_code == 202, resp.text
    token = resp.json()["reset_token"]
    assert token, "the fixture above delivers the token in the response"
    return token


async def _change_password(client, access_token: str, *, current: str, new: str):
    return await client.post(
        f"{PREFIX}/password",
        json={"current_password": current, "new_password": new},
        headers={"Authorization": f"Bearer {access_token}"},
    )


async def _spend(client, token: str, new_password: str = ATTACKER):
    return await client.post(
        f"{PREFIX}/password-reset/confirm",
        json={"token": token, "new_password": new_password},
    )


@pytest.mark.asyncio
async def test_a_reset_link_outstanding_at_the_password_change_stops_working(client):
    """The finding itself. The link was issued before the change and must not survive it."""
    tokens = await _signup(client, "clinician@example.org")
    outstanding = await _outstanding_token(client, "clinician@example.org")

    changed = await _change_password(client, tokens["access_token"], current=ORIGINAL, new=CHANGED)
    assert changed.status_code == 200, changed.text

    spent = await _spend(client, outstanding)
    assert spent.status_code == 401, "the outstanding reset link survived the password change"
    assert spent.json()["code"] == "invalid_reset_token"


@pytest.mark.asyncio
async def test_the_password_the_clinician_set_is_the_one_that_still_signs_in(client):
    """The consequence, stated as the clinician experiences it: the attacker's chosen password
    never takes effect, and the one the clinician typed still works."""
    tokens = await _signup(client, "clinician@example.org")
    outstanding = await _outstanding_token(client, "clinician@example.org")
    await _change_password(client, tokens["access_token"], current=ORIGINAL, new=CHANGED)

    await _spend(client, outstanding, new_password=ATTACKER)

    attacker_login = await client.post(
        f"{PREFIX}/login", json={"email": "clinician@example.org", "password": ATTACKER}
    )
    assert attacker_login.status_code == 401, "the reset link set a password after the change"

    clinician_login = await client.post(
        f"{PREFIX}/login", json={"email": "clinician@example.org", "password": CHANGED}
    )
    assert clinician_login.status_code == 200, clinician_login.text


@pytest.mark.asyncio
async def test_the_retirement_is_recorded_as_invalidated_not_as_spent(client, db):
    """``invalidated_at`` and not ``used_at``: nobody presented the token, so the row must not
    read as a reset that happened. The distinction is what ``reset_password`` branches on to
    tell "superseded" from "already spent", and what an incident review reads."""
    tokens = await _signup(client, "clinician@example.org")
    await _outstanding_token(client, "clinician@example.org")

    await _change_password(client, tokens["access_token"], current=ORIGINAL, new=CHANGED)

    row = (await db.execute(select(PasswordResetToken))).scalars().one()
    assert row.used_at is None, "no one spent this token"
    assert row.invalidated_at is not None, "the token outlived the password change"
    assert row.invalidated_at.replace(tzinfo=row.invalidated_at.tzinfo or UTC) <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_the_password_change_audit_row_says_how_many_links_it_retired(client, db):
    """Counted on the existing ``auth_password_changed`` row rather than in a new action: it is
    the same event, and an incident review asking "was a reset link live when this happened?"
    should not have to join two rows to find out."""
    tokens = await _signup(client, "clinician@example.org")
    await _outstanding_token(client, "clinician@example.org")

    await _change_password(client, tokens["access_token"], current=ORIGINAL, new=CHANGED)

    row = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action == "auth_password_changed")
                .order_by(AuditLog.sequence.desc())
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.payload["invalidated_reset_tokens"] == 1


@pytest.mark.asyncio
async def test_a_failed_password_change_leaves_the_reset_link_alone(client):
    """Only a change that actually happened retires anything. Otherwise anyone at a borrowed
    keyboard could burn a clinician's reset link by guessing the current password wrongly —
    turning a rejected request into a denial of the one recovery route the product offers."""
    tokens = await _signup(client, "clinician@example.org")
    outstanding = await _outstanding_token(client, "clinician@example.org")

    rejected = await _change_password(
        client, tokens["access_token"], current="not-the-password", new=CHANGED
    )
    assert rejected.status_code == 401

    spent = await _spend(client, outstanding, new_password=ATTACKER)
    assert spent.status_code == 200, spent.text


@pytest.mark.asyncio
async def test_only_the_changing_account_s_links_are_retired(client, db):
    """Scoped by account. A clinician changing their own password must not invalidate the reset
    link a colleague is in the middle of using."""
    mine = await _signup(client, "mine@example.org")
    await _signup(client, "theirs@example.org")
    theirs = await _outstanding_token(client, "theirs@example.org")
    await _outstanding_token(client, "mine@example.org")

    await _change_password(client, mine["access_token"], current=ORIGINAL, new=CHANGED)

    spent = await _spend(client, theirs, new_password="colleague-new-passphrase")
    assert spent.status_code == 200, spent.text

    theirs_account = (
        (await db.execute(select(Account).where(Account.email == "theirs@example.org")))
        .scalars()
        .one()
    )
    rows = (
        (
            await db.execute(
                select(PasswordResetToken).where(PasswordResetToken.account_id == theirs_account.id)
            )
        )
        .scalars()
        .all()
    )
    assert [r.used_at is not None for r in rows] == [True]
