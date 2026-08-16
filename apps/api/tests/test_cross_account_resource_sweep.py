"""Cross-account access to resources that are not patients: documents, sessions, suggestions.

What the existing sweep proves, and what it cannot
--------------------------------------------------
``tests/test_route_authz.py`` enumerates the router table and calls every route carrying
``{patient_id}`` with another account's patient id. That covers the obvious IDOR and covers it
automatically for new routes — but it can only ever say something about the *patient* id, and
five of this API's path parameters are not patient ids::

    /patients/{patient_id}/documents/{doc_id}/file
    /reasoning/{session_id}/suggestions/{suggestion_id}/decision
    /auth/sessions/{session_id}
    /validation/runs/{run_id}

Where those appear next to a ``{patient_id}``, the sweep fills *both* from the victim's account,
so the patient check answers first and the second id is never actually tested. The attack that
leaves is the more natural one anyway: an attacker has an account, and therefore has a chart of
their own to hang a stolen id on. ``GET /patients/{my_own_patient}/documents/{your_doc}/file``
passes every patient-scoped check there is — the patient is mine — and asks the service to hand
over a scan by id. Whether that works depends on whether the document lookup is filtered by the
patient in the path or only by the document's own id, which is a property of each service and
not of the dependency that resolves the account.

So this module fills the id from the attacker's account wherever it can, and steals only the one
id under test. The routes are still enumerated from the live router table, so a new endpoint on
any of these resources is swept the moment it is registered; the meta-tests at the bottom fail if
a sweep stops matching any route, which is how an enumeration quietly covers nothing.

Every expectation here is "any 4xx". 404 is the documented answer — the routers state that
another account's id is a 404 rather than a 403, so the endpoint does not confirm the resource
exists — but a 403 would also be safe, and pinning the exact code would make this a test about
error taxonomy rather than about access.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient

from app.models.validation import ValidationRun
from tests.conftest import create_patient

# Same synthetic prescription the document tests use: the header satisfies magic-byte sniffing
# and the body is read by the deterministic text fallback, so no LLM is involved.
SCAN = b"%PDF-1.4\nMEDICATIONS:\nGlycomet 500mg BD\nCONDITIONS:\nType 2 Diabetes Mellitus\n"

# Bodies good enough to get past request validation on the routes that take one. A 422 would be
# a safe answer too, but it would mean the request never reached the authorization check, and
# this module is about the authorization check.
BODIES: dict[str, dict] = {
    "POST /api/v1/reasoning/{session_id}/intake/answers": {"answers": []},
    "POST /api/v1/reasoning/{session_id}/suggestions/{suggestion_id}/decision": {
        "decision": "accepted",
        "reason": "sweeping for cross-account writes",
    },
}


def _routes(app) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, route.path))
    return out


async def _call(client: AsyncClient, method: str, template: str, path: str):
    return await client.request(method, path, json=BODIES.get(f"{method} {template}", {}))


async def _reasoning_session(client: AsyncClient, patient_id: str) -> str:
    resp = await client.post(
        f"/api/v1/patients/{patient_id}/reasoning",
        json={"presenting_complaint": "fever and cough for three days"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["session"]["id"]


async def _answered_and_run(client: AsyncClient, session_id: str) -> list[dict]:
    """Take a session through intake to a completed run, and return its suggestions."""
    for _ in range(4):
        pending = (await client.get(f"/api/v1/reasoning/{session_id}/intake")).json()
        if not pending:
            break
        answered = await client.post(
            f"/api/v1/reasoning/{session_id}/intake/answers",
            json={
                "answers": [
                    {"question_id": q["id"], "answer_text": "since Monday"} for q in pending
                ]
            },
        )
        assert answered.status_code == 200, answered.text
        if answered.json()["intake_complete"]:
            break
    run = await client.post(f"/api/v1/reasoning/{session_id}/run")
    assert run.status_code == 200, run.text
    return run.json()["suggestions"]


# --- Documents ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_document_route_serves_another_accounts_document_through_your_own_chart(
    app, auth_client, second_auth_client
):
    """The whole point of this module, on the resource where it costs the most.

    A document is a scanned prescription or lab report: a PDF with a name, an address and a
    diagnosis on it. If the ``{doc_id}`` lookup is not filtered by the patient in the path, an
    account with any chart of its own can read any document on the deployment.
    """
    victim = await create_patient(auth_client)
    upload = await auth_client.post(
        f"/api/v1/patients/{victim['id']}/documents",
        files={"file": ("rx.pdf", SCAN, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    stolen_doc_id = upload.json()["id"]

    attacker = await create_patient(second_auth_client, full_name="Attacker's Own Patient")

    swept, leaked = 0, []
    for method, template in _routes(app):
        if "{doc_id}" not in template:
            continue
        swept += 1
        path = template.replace("{patient_id}", attacker["id"]).replace("{doc_id}", stolen_doc_id)
        resp = await _call(second_auth_client, method, template, path)
        if resp.status_code < 400:
            leaked.append(f"{method} {template} -> {resp.status_code}")

    assert swept, "the document sweep matched no routes; it is proving nothing"
    assert not leaked, "another account's document was reachable: " + "; ".join(leaked)


@pytest.mark.asyncio
async def test_the_stolen_document_id_is_not_merely_unroutable(auth_client):
    """Guard the guard. If the id above were rejected because it is malformed, or because the
    upload never happened, every assertion in that sweep would pass vacuously."""
    patient = await create_patient(auth_client)
    upload = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("rx.pdf", SCAN, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]

    owner_view = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents/{doc_id}/file")
    assert owner_view.status_code == 200, (
        "the owner cannot read the document the sweep tries to steal, so the sweep was asking "
        f"for something unreadable: {owner_view.text}"
    )


# --- Encounters --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_encounter_route_reaches_another_accounts_visit_through_your_own_chart(
    app, auth_client, second_auth_client
):
    """The document attack, on the resource that holds the consultation note itself.

    An encounter carries the presenting complaint and the clinician's notes. If the
    ``{encounter_id}`` lookup were filtered by the encounter's own id alone, an account with any
    chart of its own could read — and, worse, *sign or amend* — a visit on somebody else's.
    Writing is the sharper half here: a stolen encounter id on a POST would attest to another
    clinician's note under this account's name, and the signature is permanent.
    """
    victim = await create_patient(auth_client)
    opened = await auth_client.post(
        f"/api/v1/patients/{victim['id']}/encounters",
        json={"encounter_date": "2026-08-14", "clinician_notes": "Victim's consultation note"},
    )
    assert opened.status_code == 201, opened.text
    stolen_encounter_id = opened.json()["id"]

    attacker = await create_patient(second_auth_client, full_name="Attacker's Own Patient")

    swept, leaked = 0, []
    for method, template in _routes(app):
        if "{encounter_id}" not in template:
            continue
        swept += 1
        path = template.replace("{patient_id}", attacker["id"]).replace(
            "{encounter_id}", stolen_encounter_id
        )
        resp = await _call(second_auth_client, method, template, path)
        if resp.status_code < 400:
            leaked.append(f"{method} {template} -> {resp.status_code}")

    assert swept, "the encounter sweep matched no routes; it is proving nothing"
    assert not leaked, "another account's encounter was reachable: " + "; ".join(leaked)


@pytest.mark.asyncio
async def test_the_stolen_encounter_id_is_not_merely_unroutable(auth_client):
    """Guard the guard, as for the document sweep above."""
    patient = await create_patient(auth_client)
    opened = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/encounters",
        json={"encounter_date": "2026-08-14"},
    )
    assert opened.status_code == 201, opened.text

    owner_view = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/encounters/{opened.json()['id']}"
    )
    assert owner_view.status_code == 200, (
        "the owner cannot read the encounter the sweep tries to steal, so the sweep was asking "
        f"for something unreadable: {owner_view.text}"
    )


# --- Reasoning sessions ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_reasoning_route_accepts_another_accounts_session(
    app, auth_client, second_auth_client
):
    """A session carries the presenting complaint, the intake answers and the whole deliberation.

    Unlike the document routes there is no patient id in these paths to fall back on, so the
    account check on the session id is the only thing between the two accounts.
    """
    victim = await create_patient(auth_client)
    stolen_session = await _reasoning_session(auth_client, victim["id"])
    await create_patient(second_auth_client, full_name="Attacker's Own Patient")

    swept, leaked = 0, []
    for method, template in _routes(app):
        if "{session_id}" not in template or not template.startswith("/api/v1/reasoning"):
            continue
        if template.endswith("/stream"):
            # Authenticated by a query token rather than the bearer header; minting one for
            # another account's session is covered separately below.
            continue
        swept += 1
        path = template.replace("{session_id}", stolen_session).replace(
            "{suggestion_id}", str(uuid.uuid4())
        )
        resp = await _call(second_auth_client, method, template, path)
        if resp.status_code < 400:
            leaked.append(f"{method} {template} -> {resp.status_code}")

    assert swept, "the reasoning sweep matched no routes; it is proving nothing"
    assert not leaked, "another account's reasoning session was reachable: " + "; ".join(leaked)


@pytest.mark.asyncio
async def test_a_stream_token_cannot_be_minted_for_another_accounts_session(
    auth_client, second_auth_client
):
    """The one route the sweep above skips. A stream token is a bearer credential for one
    session that travels in a query string; minting one is the step that has to be scoped."""
    victim = await create_patient(auth_client)
    stolen_session = await _reasoning_session(auth_client, victim["id"])

    minted = await second_auth_client.post(f"/api/v1/reasoning/{stolen_session}/stream-token")
    assert minted.status_code >= 400, (
        f"another account minted a stream credential for this session: {minted.text}"
    )


# --- Suggestions -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_decision_cannot_be_recorded_against_another_sessions_suggestion(
    auth_client, second_auth_client
):
    """The suggestion id, stolen and hung on the attacker's own session.

    A decision is an audit-trail write attributing an accept or a dismissal to a clinician. If
    the suggestion is looked up by its own id alone, one account can write into the record of a
    suggestion made on a chart it cannot see — and Rule #7 means that write is permanent.
    """
    victim = await create_patient(auth_client)
    victim_session = await _reasoning_session(auth_client, victim["id"])
    suggestions = await _answered_and_run(auth_client, victim_session)
    assert suggestions, "the run produced nothing to record a decision against"
    stolen_suggestion = suggestions[0]["id"]

    attacker = await create_patient(second_auth_client, full_name="Attacker's Own Patient")
    attacker_session = await _reasoning_session(second_auth_client, attacker["id"])

    resp = await second_auth_client.post(
        f"/api/v1/reasoning/{attacker_session}/suggestions/{stolen_suggestion}/decision",
        json={"decision": "accepted", "reason": "recording against a chart I cannot see"},
    )
    assert resp.status_code >= 400, (
        f"a decision was recorded against another chart's suggestion: {resp.text}"
    )


@pytest.mark.asyncio
async def test_the_owner_can_record_that_same_decision(auth_client):
    """The other half: the refusal above must be about the account, not about the route being
    broken for everyone."""
    patient = await create_patient(auth_client)
    session = await _reasoning_session(auth_client, patient["id"])
    suggestions = await _answered_and_run(auth_client, session)

    resp = await auth_client.post(
        f"/api/v1/reasoning/{session}/suggestions/{suggestions[0]['id']}/decision",
        json={"decision": "accepted", "reason": "reviewed alongside the chart"},
    )
    assert resp.status_code == 201, resp.text


# --- Sign-in sessions --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_account_cannot_sign_another_account_out(auth_client, second_auth_client):
    """``DELETE /auth/sessions/{session_id}`` is the only route whose ``{session_id}`` is a
    sign-in session rather than a reasoning session, so the reasoning sweep never reaches it.

    Revoking someone else's session is a denial of service against a clinician mid-consultation,
    and it needs no read access to be worth doing.
    """
    listed = await auth_client.get("/api/v1/auth/sessions")
    assert listed.status_code == 200, listed.text
    sessions = listed.json()
    assert sessions, "the victim has no sign-in session to revoke"
    victim_session_id = sessions[0]["id"]

    revoked = await second_auth_client.delete(f"/api/v1/auth/sessions/{victim_session_id}")
    assert revoked.status_code >= 400, f"another account revoked this session: {revoked.text}"

    still_listed = await auth_client.get("/api/v1/auth/sessions")
    assert still_listed.status_code == 200, "the victim was signed out by the attempt"
    assert victim_session_id in {s["id"] for s in still_listed.json()}, (
        "the session survived the request but was revoked anyway"
    )


# --- Validation runs ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_accounts_validation_run_is_not_readable(auth_client, second_auth_client, db):
    """A validation run holds the vignettes and the engine's answers on them. It is the one
    account-scoped resource with no patient id anywhere near it."""
    me = await auth_client.get("/api/v1/auth/me")
    assert me.status_code == 200, me.text

    # Running the real case set replays every vignette through a full panel, so the row is
    # inserted directly: what is under test is the read's scoping, not the engine. The ``db``
    # fixture shares the app's sessionmaker, so this lands in the database the request reads.
    run = ValidationRun(account_id=uuid.UUID(me.json()["id"]), vignette_count=0)
    db.add(run)
    await db.commit()
    run_id = run.id

    stolen = await second_auth_client.get(f"/api/v1/validation/runs/{run_id}")
    assert stolen.status_code >= 400, f"another account read this validation run: {stolen.text}"

    owned = await auth_client.get(f"/api/v1/validation/runs/{run_id}")
    assert owned.status_code == 200, (
        f"the owner cannot read it either, so the refusal above proves nothing: {owned.text}"
    )


# --- Meta: the sweeps have to keep matching the router table -----------------------------------


@pytest.mark.asyncio
async def test_every_non_patient_path_parameter_is_covered_by_a_sweep(app):
    """The failure this module is most likely to have is silent: a new path parameter appears,
    no sweep matches it, and every test above still passes.

    So the parameters are enumerated from the routes and checked against what is claimed here.
    A new one fails this test, and the fix is a sweep for it rather than an entry in the list.
    """
    swept = {"patient_id", "doc_id", "encounter_id", "session_id", "suggestion_id", "run_id"}
    # Not resource ids, and neither is scoped to an account:
    #   condition_name — the pathway routes take a condition *name*, a lookup into the shared
    #     guideline corpus;
    #   resource_type — the per-type export takes a FHIR type name ("Condition",
    #     "MedicationRequest"), a fixed vocabulary shared by every deployment. The chart it reads
    #     is chosen by the {patient_id} beside it, which the sweep above does cover.
    not_a_resource = {"condition_name", "resource_type"}

    found = {
        part[1:-1]
        for _method, template in _routes(app)
        for part in template.split("/")
        if part.startswith("{") and part.endswith("}")
    }
    unswept = found - swept - not_a_resource
    assert not unswept, (
        f"path parameters no cross-account sweep covers: {sorted(unswept)}. Add a sweep for "
        "each, or record here why it is not an account-scoped resource id."
    )
