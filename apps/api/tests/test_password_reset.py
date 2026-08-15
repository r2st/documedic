"""Password change and reset — the flow that did not exist.

``accounts.password_hash`` was written once, at signup, and by nothing else. A clinician who
forgot a password had no route back to their own audit identity, and one whose password was
*compromised* had no route at all: `POST /auth/logout-all` revokes sessions, and the attacker
who knows the password simply signs in again. Revoking sessions without being able to change
the credential is theatre.

What is asserted here is the shape of the flow rather than its happy path alone:

* the reset request is indistinguishable for an address with an account and one without, in
  status, body and headers — including when the account is over its ceiling, because a ceiling
  that shows through is itself the enumeration oracle;
* tokens are single-use, time-limited, superseded by the next request, and stored as hashes;
* completing a reset signs the account out everywhere, and changing a password signs out
  everything except the device doing it;
* changing a password re-proves the current one, so a borrowed unlocked workstation is not
  enough to take an account over.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config import Settings, production_config_errors, settings
from app.models.audit_log import AuditLog
from app.models.user import Account, PasswordResetToken, Session
from app.routers.auth import deliver_reset_token
from app.services.auth_service import AuthService

PREFIX = "/api/v1/auth"


@pytest.fixture(autouse=True)
def _token_in_the_response(monkeypatch):
    """Exercise the flow end to end over HTTP.

    The shipped default is ``log`` — a token in the response body is an account takeover for
    anyone who can POST an address, which is why production refuses it. Here it is what lets
    these tests be about the reset flow rather than about scraping a log line. The two tests
    that are *about* the channel override this themselves.
    """
    monkeypatch.setattr(settings, "password_reset_delivery", "response")


async def _signup(client, email: str, password: str = "correct-horse-battery") -> dict:
    resp = await client.post(
        f"{PREFIX}/signup", json={"email": email, "password": password, "display_name": "Dr X"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _request_reset(client, email: str):
    return await client.post(f"{PREFIX}/password-reset/request", json={"email": email})


async def _token_for(client, email: str) -> str:
    body = (await _request_reset(client, email)).json()
    assert body["reset_token"], "the test settings deliver the token in the response"
    return body["reset_token"]


# --- No user enumeration -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reset_request_is_identical_for_a_known_and_an_unknown_address(client):
    """The whole point of the endpoint's design. A different status, a different message, or a
    populated `reset_token` for one and not the other turns "start a reset" into "does this
    person work here?" — the question `POST /auth/login` goes to some length not to answer."""
    await _signup(client, "known@example.org")

    known = await _request_reset(client, "known@example.org")
    unknown = await _request_reset(client, "nobody@example.org")

    assert known.status_code == unknown.status_code == 202
    assert known.json()["message"] == unknown.json()["message"]
    assert unknown.json()["reset_token"] is None


@pytest.mark.asyncio
async def test_exceeding_the_per_account_ceiling_is_silent(client, monkeypatch):
    """A ceiling that shows through is the oracle it was meant to close: "this address started
    refusing after five" is a positive answer about that address."""
    monkeypatch.setattr(settings, "password_reset_max_requests_per_hour", 2)
    await _signup(client, "busy@example.org")

    responses = [await _request_reset(client, "busy@example.org") for _ in range(4)]
    unknown = await _request_reset(client, "nobody@example.org")

    assert {r.status_code for r in responses} == {202}
    assert {r.json()["message"] for r in responses} == {unknown.json()["message"]}
    # The ceiling did apply — it just did not announce itself.
    assert [r.json()["reset_token"] is None for r in responses] == [False, False, True, True]


@pytest.mark.asyncio
async def test_a_throttled_request_is_recorded_even_though_the_caller_is_not_told(
    client, db, monkeypatch
):
    """Invisible to the caller, visible to the security trail — which is where a burst of reset
    requests against one clinician needs to be legible."""
    monkeypatch.setattr(settings, "password_reset_max_requests_per_hour", 1)
    await _signup(client, "watched@example.org")
    await _request_reset(client, "watched@example.org")
    await _request_reset(client, "watched@example.org")

    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "auth_password_reset_throttled" in actions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail",
    ["unknown", "expired", "spent", "superseded"],
)
async def test_every_reset_failure_is_byte_identical(client, db, detail):
    """Four ways to fail, one response. "Already used" in particular would confirm that the
    address is a live account someone is mid-reset on."""
    await _signup(client, "target@example.org")
    token = await _token_for(client, "target@example.org")

    if detail == "unknown":
        token = "0" * 64
    elif detail == "expired":
        row = (await db.execute(select(PasswordResetToken))).scalars().one()
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()
    elif detail == "spent":
        first = await client.post(
            f"{PREFIX}/password-reset/confirm", json={"token": token, "new_password": "n3wpassw0rd"}
        )
        assert first.status_code == 200
    elif detail == "superseded":
        await _token_for(client, "target@example.org")

    resp = await client.post(
        f"{PREFIX}/password-reset/confirm",
        json={"token": token, "new_password": "an0therpassword"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"code": "invalid_reset_token", "message": _EXPECTED_FAILURE_MESSAGE}


_EXPECTED_FAILURE_MESSAGE = (
    "This password-reset link is no longer valid. Request a new one from the sign-in screen."
)


# --- Token properties ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reset_token_is_stored_only_as_a_hash(client, db):
    """Same rule as refresh tokens: reading this table must not make you able to take an
    account over."""
    await _signup(client, "hash@example.org")
    token = await _token_for(client, "hash@example.org")
    rows = (await db.execute(select(PasswordResetToken.token_hash))).scalars().all()
    assert rows and token not in rows
    assert all(len(value) == 64 for value in rows)


@pytest.mark.asyncio
async def test_requesting_again_invalidates_the_outstanding_token(client):
    """ "The first link never arrived, send another" must not leave two live capabilities over
    one account — otherwise a token seen in a log can be held in reserve behind a legitimate
    reset."""
    await _signup(client, "twice@example.org")
    first = await _token_for(client, "twice@example.org")
    second = await _token_for(client, "twice@example.org")

    stale = await client.post(
        f"{PREFIX}/password-reset/confirm", json={"token": first, "new_password": "n3wpassw0rd"}
    )
    assert stale.status_code == 401

    fresh = await client.post(
        f"{PREFIX}/password-reset/confirm", json={"token": second, "new_password": "n3wpassw0rd"}
    )
    assert fresh.status_code == 200


@pytest.mark.asyncio
async def test_a_reset_token_expires(client, db, monkeypatch):
    monkeypatch.setattr(settings, "password_reset_token_ttl_minutes", 30)
    await _signup(client, "slow@example.org")
    token = await _token_for(client, "slow@example.org")

    row = (await db.execute(select(PasswordResetToken))).scalars().one()
    assert row.expires_at is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    resp = await client.post(
        f"{PREFIX}/password-reset/confirm", json={"token": token, "new_password": "n3wpassw0rd"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_replaying_a_spent_token_is_audited(client, db):
    """The legitimate holder has no reason to present a spent token twice, so the second
    presentation is a signal rather than a retry."""
    await _signup(client, "replay@example.org")
    token = await _token_for(client, "replay@example.org")
    for _ in range(2):
        await client.post(
            f"{PREFIX}/password-reset/confirm",
            json={"token": token, "new_password": "n3wpassw0rd"},
        )

    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "auth_password_reset_token_reused" in actions


# --- What a reset actually does --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_completed_reset_changes_the_password_the_account_signs_in_with(client):
    await _signup(client, "reset@example.org", password="old-password-1")
    token = await _token_for(client, "reset@example.org")
    await client.post(
        f"{PREFIX}/password-reset/confirm", json={"token": token, "new_password": "new-password-2"}
    )

    old = await client.post(
        f"{PREFIX}/login", json={"email": "reset@example.org", "password": "old-password-1"}
    )
    new = await client.post(
        f"{PREFIX}/login", json={"email": "reset@example.org", "password": "new-password-2"}
    )
    assert old.status_code == 401
    assert new.status_code == 200


@pytest.mark.asyncio
async def test_a_reset_signs_the_account_out_everywhere(client, db):
    """No equivalent of change_password's "keep this one": possession of a reset token proves
    nothing about which of the live sessions were the clinician's."""
    tokens = await _signup(client, "everywhere@example.org", password="old-password-1")
    reset = await _token_for(client, "everywhere@example.org")
    await client.post(
        f"{PREFIX}/password-reset/confirm", json={"token": reset, "new_password": "new-password-2"}
    )

    refreshed = await client.post(
        f"{PREFIX}/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 401

    live = await db.scalar(
        select(func.count()).select_from(Session).where(Session.is_revoked.is_(False))
    )
    assert live == 0


@pytest.mark.asyncio
async def test_a_reset_for_a_deactivated_account_is_refused(client, db):
    await _signup(client, "gone@example.org")
    token = await _token_for(client, "gone@example.org")
    account = (
        await db.execute(select(Account).where(Account.email == "gone@example.org"))
    ).scalar_one()
    account.is_deleted = True
    await db.commit()

    resp = await client.post(
        f"{PREFIX}/password-reset/confirm", json={"token": token, "new_password": "n3wpassw0rd"}
    )
    assert resp.status_code == 401


# --- Changing a password while signed in -----------------------------------------------------


@pytest.mark.asyncio
async def test_changing_a_password_requires_the_current_one(client):
    """A valid access token means "reached the keyboard", not "knows the password", and an
    unattended workstation is the commonest way one is borrowed."""
    tokens = await _signup(client, "change@example.org", password="old-password-1")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    resp = await client.post(
        f"{PREFIX}/password",
        json={"current_password": "not-it", "new_password": "new-password-2"},
        headers=headers,
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"

    still_old = await client.post(
        f"{PREFIX}/login", json={"email": "change@example.org", "password": "old-password-1"}
    )
    assert still_old.status_code == 200


@pytest.mark.asyncio
async def test_a_failed_password_change_is_audited(client, db):
    tokens = await _signup(client, "audited@example.org", password="old-password-1")
    await client.post(
        f"{PREFIX}/password",
        json={"current_password": "not-it", "new_password": "new-password-2"},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "auth_password_change_failed" in actions


@pytest.mark.asyncio
async def test_changing_a_password_signs_out_other_devices_but_can_keep_this_one(client):
    """The point of changing a password after a suspected compromise is that the other
    device's refresh token stops working; the point of the exception is that the clinician
    doing it is not thrown out of the tab they are consulting in."""
    here = await _signup(client, "devices@example.org", password="old-password-1")
    there = await client.post(
        f"{PREFIX}/login", json={"email": "devices@example.org", "password": "old-password-1"}
    )
    other_refresh = there.json()["refresh_token"]

    resp = await client.post(
        f"{PREFIX}/password",
        json={
            "current_password": "old-password-1",
            "new_password": "new-password-2",
            "keep_current_refresh_token": here["refresh_token"],
        },
        headers={"Authorization": f"Bearer {here['access_token']}"},
    )
    assert resp.status_code == 200

    # The kept session is probed *first*, and deliberately: presenting the revoked token is
    # token reuse, and reuse detection revokes the whole family — including this one. Probing
    # in the other order tests the reuse defence, not this one.
    assert (
        await client.post(f"{PREFIX}/refresh", json={"refresh_token": here["refresh_token"]})
    ).status_code == 200
    assert (
        await client.post(f"{PREFIX}/refresh", json={"refresh_token": other_refresh})
    ).status_code == 401


@pytest.mark.asyncio
async def test_a_changed_password_is_the_one_that_signs_in(client):
    tokens = await _signup(client, "swapped@example.org", password="old-password-1")
    await client.post(
        f"{PREFIX}/password",
        json={"current_password": "old-password-1", "new_password": "new-password-2"},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    old = await client.post(
        f"{PREFIX}/login", json={"email": "swapped@example.org", "password": "old-password-1"}
    )
    new = await client.post(
        f"{PREFIX}/login", json={"email": "swapped@example.org", "password": "new-password-2"}
    )
    assert old.status_code == 401 and new.status_code == 200


# --- Delivery channel ------------------------------------------------------------------------


def test_returning_the_token_in_the_response_is_refused_in_production():
    """`response` delivery hands the reset token to whoever POSTed the address. It makes the
    flow usable against a bare API, which is worth having in development and is an account
    takeover in production."""
    problems = production_config_errors(
        Settings(
            app_env="production",
            app_secret_key="x" * 40,
            app_debug=False,
            field_encryption_key="k" * 32,
            password_reset_delivery="response",
        )
    )
    assert any("PASSWORD_RESET_DELIVERY" in problem for problem in problems)

    allowed = production_config_errors(
        Settings(
            app_env="production",
            app_secret_key="x" * 40,
            app_debug=False,
            field_encryption_key="k" * 32,
            password_reset_delivery="log",
        )
    )
    assert not any("PASSWORD_RESET_DELIVERY" in problem for problem in allowed)


def test_log_delivery_withholds_the_token_from_the_response(monkeypatch, caplog):
    monkeypatch.setattr(settings, "password_reset_delivery", "log")
    with caplog.at_level("WARNING"):
        assert deliver_reset_token("s3cr3t-token") is None
    assert any("s3cr3t-token" in record.getMessage() for record in caplog.records)


def test_no_channel_can_distinguish_an_address_without_an_account(monkeypatch, caplog):
    """``None`` in is ``None`` out through every branch — including the logging one, which must
    not emit a line for an address that has no account."""
    for channel in ("response", "log"):
        monkeypatch.setattr(settings, "password_reset_delivery", channel)
        caplog.clear()
        with caplog.at_level("WARNING"):
            assert deliver_reset_token(None) is None
        assert not caplog.records


# --- Service-level ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_password_reset_returns_none_for_an_unknown_address(db):
    assert await AuthService(db).request_password_reset("nobody@example.org") is None
