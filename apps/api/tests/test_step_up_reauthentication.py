"""The step-up password prompt in front of chart deletion, bulk import and record export.

The control being asserted is narrow and worth stating plainly: an access token proves that a
sign-in happened, not that the person now holding the keyboard is the clinician it happened for.
On this product's deployment — one practice login, several machines, signed in through a shift —
those two facts come apart routinely, and every other session control (idle timeout, absolute
expiry, revocation, reuse detection) measures the token rather than the person.

So the tests below are mostly about *what does not satisfy the gate*: a valid token does not, a
refreshed token does not, and another device's confirmation does not. Each of those is a way the
control could be technically present and practically absent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.user import Session as AuthSession
from tests.conftest import create_patient

pytestmark = pytest.mark.asyncio

_PASSWORD = "password123"
_EMAIL = "doc@example.com"


async def _age_the_sign_in(db, *, minutes: int) -> None:
    """Backdate every live sign-in's password proof, as the passage of time would.

    Every other way of producing an expired step-up window in a test is worse: sleeping for
    fifteen minutes is not a test, and monkeypatching ``datetime.now`` inside the dependency
    would assert that the mock was installed rather than that the column is read.
    """
    rows = (await db.execute(select(AuthSession))).scalars().all()
    for row in rows:
        row.last_authenticated_at = datetime.now(UTC) - timedelta(minutes=minutes)
    await db.commit()


async def test_a_fresh_sign_in_satisfies_the_gate(auth_client):
    """The prompt must not fire on the request straight after signing in — a control that asks
    twice in a row for the same proof is one clinicians learn to click through."""
    patient = await create_patient(auth_client)
    resp = await auth_client.delete(f"/api/v1/patients/{patient['id']}")
    assert resp.status_code == 200, resp.text


async def test_an_aged_sign_in_is_refused_with_a_code_a_client_can_act_on(auth_client, db):
    patient = await create_patient(auth_client)
    await _age_the_sign_in(db, minutes=settings.reauthentication_max_age_minutes + 1)

    resp = await auth_client.delete(f"/api/v1/patients/{patient['id']}")

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "reauthentication_required"
    # The chart is untouched: a refused step-up is not a half-performed action.
    assert (await auth_client.get(f"/api/v1/patients/{patient['id']}")).status_code == 200


async def test_the_refusal_is_403_and_never_401(auth_client, db):
    """A 401 would be answered by the frontend's refresh-then-sign-out path, so a clinician who
    simply had not typed their password recently would be signed out of a session that was never
    in question. The status code is the whole contract here."""
    patient = await create_patient(auth_client)
    await _age_the_sign_in(db, minutes=999)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/export")

    assert resp.status_code == 403
    assert resp.json()["code"] == "reauthentication_required"


async def test_confirming_the_password_reopens_the_window(auth_client, db):
    patient = await create_patient(auth_client)
    await _age_the_sign_in(db, minutes=999)
    assert (await auth_client.delete(f"/api/v1/patients/{patient['id']}")).status_code == 403

    confirm = await auth_client.post("/api/v1/auth/reauthenticate", json={"password": _PASSWORD})
    assert confirm.status_code == 200, confirm.text
    body = confirm.json()
    assert body["valid_for_seconds"] == settings.reauthentication_max_age_minutes * 60
    assert body["valid_until"] > body["authenticated_at"]

    assert (await auth_client.delete(f"/api/v1/patients/{patient['id']}")).status_code == 200


async def test_a_wrong_password_does_not_reopen_the_window(auth_client, db):
    patient = await create_patient(auth_client)
    await _age_the_sign_in(db, minutes=999)

    resp = await auth_client.post("/api/v1/auth/reauthenticate", json={"password": "not-it"})

    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"
    assert (await auth_client.delete(f"/api/v1/patients/{patient['id']}")).status_code == 403


async def test_a_wrong_password_here_does_not_lock_the_account_out(auth_client, db, client):
    """Deliberate, and the opposite of what the sign-in route does.

    Only someone already holding a valid access token can reach this route. If wrong guesses fed
    the login lockout, anyone passing an unattended signed-in screen could type rubbish a few
    times and shut the clinician out of their own records for the cooldown — turning a control
    meant to protect the account into a way to deny it.
    """
    await _age_the_sign_in(db, minutes=999)
    for _ in range(settings.login_max_failed_attempts + 2):
        resp = await auth_client.post("/api/v1/auth/reauthenticate", json={"password": "nope"})
        assert resp.status_code == 401

    login = await client.post("/api/v1/auth/login", json={"email": _EMAIL, "password": _PASSWORD})
    assert login.status_code == 200, login.text


async def test_both_outcomes_reach_the_audit_trail(auth_client, db):
    await _age_the_sign_in(db, minutes=999)
    await auth_client.post("/api/v1/auth/reauthenticate", json={"password": "nope"})
    await auth_client.post("/api/v1/auth/reauthenticate", json={"password": _PASSWORD})

    actions = (
        (await db.execute(select(AuditLog.action).order_by(AuditLog.sequence))).scalars().all()
    )
    assert "auth_reauthentication_failed" in actions
    assert "auth_reauthenticated" in actions


async def test_refreshing_a_token_is_not_a_password_proof(auth_client, client, db):
    """The sharpest case. Rotation moves ``last_used_at``, which is what an open browser tab
    does on a timer with nobody at it. If rotation also re-stamped the password proof, a tab left
    open on a ward workstation would hold the step-up window open indefinitely and this gate
    would be decorative."""
    login = await client.post("/api/v1/auth/login", json={"email": _EMAIL, "password": _PASSWORD})
    refresh_token = login.json()["refresh_token"]
    patient = await create_patient(auth_client)
    await _age_the_sign_in(db, minutes=999)

    rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert rotated.status_code == 200, rotated.text
    auth_client.headers["Authorization"] = f"Bearer {rotated.json()['access_token']}"

    resp = await auth_client.delete(f"/api/v1/patients/{patient['id']}")
    assert resp.status_code == 403
    assert resp.json()["code"] == "reauthentication_required"


async def test_a_fresh_sign_in_carries_its_proof_through_rotation(auth_client, client):
    """The other half of the rule above: rotation must not *lose* the proof either, or a
    clinician would be prompted every fifteen minutes for no reason."""
    login = await client.post("/api/v1/auth/login", json={"email": _EMAIL, "password": _PASSWORD})
    patient = await create_patient(auth_client)

    rotated = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    auth_client.headers["Authorization"] = f"Bearer {rotated.json()['access_token']}"

    assert (await auth_client.delete(f"/api/v1/patients/{patient['id']}")).status_code == 200


async def test_confirming_on_one_device_does_not_unlock_another(app, auth_client, db):
    """The gate asks about the person at *this* keyboard, so the answer is scoped to this
    sign-in. Stamping the account would mean a clinician confirming on their phone also cleared
    the prompt on the consulting-room screen someone else is standing at."""
    second = await auth_client.post(
        "/api/v1/auth/login", json={"email": _EMAIL, "password": _PASSWORD}
    )
    patient = await create_patient(auth_client)
    await _age_the_sign_in(db, minutes=999)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as other:
        other.headers["Authorization"] = f"Bearer {second.json()['access_token']}"
        confirmed = await other.post("/api/v1/auth/reauthenticate", json={"password": _PASSWORD})
        assert confirmed.status_code == 200, confirmed.text

    # The *first* sign-in is still cold.
    resp = await auth_client.delete(f"/api/v1/patients/{patient['id']}")
    assert resp.status_code == 403


async def test_changing_the_password_counts_as_a_proof(auth_client, client, db):
    """Changing a password re-verifies the current one, which is the strongest evidence of
    identity this product ever collects. Asking for it again a minute later would be absurd."""
    login = await client.post("/api/v1/auth/login", json={"email": _EMAIL, "password": _PASSWORD})
    refresh_token = login.json()["refresh_token"]
    auth_client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
    patient = await create_patient(auth_client)
    await _age_the_sign_in(db, minutes=999)

    changed = await auth_client.post(
        "/api/v1/auth/password",
        json={
            "current_password": _PASSWORD,
            "new_password": "a-new-password-1",
            "keep_current_refresh_token": refresh_token,
        },
    )
    assert changed.status_code == 200, changed.text

    assert (await auth_client.delete(f"/api/v1/patients/{patient['id']}")).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/patients/{pid}/export",
        "/api/v1/patients/{pid}/export/pdf",
        "/api/v1/patients/{pid}/export/Condition",
    ],
)
async def test_every_bulk_export_route_is_behind_the_gate(auth_client, db, path):
    """All three, and the per-type one especially: asking for six resource types one at a time
    must not be a way around the prompt that guards asking for all of them at once."""
    patient = await create_patient(auth_client)
    await _age_the_sign_in(db, minutes=999)

    resp = await auth_client.get(path.format(pid=patient["id"]))

    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "reauthentication_required"


async def test_ordinary_clinical_reads_and_writes_are_not_behind_the_gate(auth_client, db):
    """A password prompt on the paths a clinician uses dozens of times a shift is a prompt that
    gets clicked through without being read, which makes the gate worse than useless where it
    actually matters. This pins the boundary."""
    patient = await create_patient(auth_client)
    await _age_the_sign_in(db, minutes=999)

    assert (await auth_client.get(f"/api/v1/patients/{patient['id']}")).status_code == 200
    assert (await auth_client.get(f"/api/v1/patients/{patient['id']}/record")).status_code == 200
    updated = await auth_client.patch(
        f"/api/v1/patients/{patient['id']}", json={"phone": "9876543210"}
    )
    assert updated.status_code == 200, updated.text


async def test_the_gate_can_be_switched_off_for_development(auth_client, db, monkeypatch):
    """Zero disables it, and ``production_config_errors`` refuses zero in production — the pair
    is what lets a local stack skip the prompt without letting a real deployment do it."""
    patient = await create_patient(auth_client)
    await _age_the_sign_in(db, minutes=999)
    monkeypatch.setattr(settings, "reauthentication_max_age_minutes", 0)

    assert (await auth_client.delete(f"/api/v1/patients/{patient['id']}")).status_code == 200


async def test_the_session_list_shows_when_the_password_was_last_typed(auth_client):
    """On the "where am I signed in" screen because that is where an unattended workstation is
    identifiable: a row refreshed a minute ago whose password was typed nine hours back looks,
    by ``last_used_at`` alone, like the liveliest session on the account."""
    resp = await auth_client.get("/api/v1/auth/sessions")

    assert resp.status_code == 200, resp.text
    assert all("last_authenticated_at" in row for row in resp.json())


async def test_another_account_cannot_confirm_its_way_into_this_one(second_auth_client):
    """Trivially true through the route (the token names the account) and worth pinning anyway:
    the confirmation writes an authorisation fact, and one written against the wrong sign-in
    would be a hole no other test would notice."""
    resp = await second_auth_client.post(
        "/api/v1/auth/reauthenticate", json={"password": "password123"}
    )
    # Wrong password for *this* account — doc2 signed up with password456.
    assert resp.status_code == 401
