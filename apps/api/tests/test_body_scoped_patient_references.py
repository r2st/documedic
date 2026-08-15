"""A patient id in a request *body* is as much a tenancy boundary as one in the path.

``test_route_authz`` enumerates every route carrying ``{patient_id}`` in its path template and
proves account B cannot touch account A's chart through it. Nothing enumerated the other door,
and one endpoint had walked through it: ``POST /safety-reports`` takes ``patient_id`` and
``session_id`` in its JSON body and wrote both onto the row without asking whether the caller
held either.

It is a write, not a read — no chart content comes back — which is why it survived a read-shaped
audit. What it wrote is the problem. ``file_report`` audits as ``safety_report_filed`` *against
the named patient*, and ``audit_logs`` is append-only, hash-chained and never pruned. Any account
could therefore append a permanent, unremovable entry to any chart's trail, attributed to a
clinician with no connection to the patient, and its owner reads that trail at
``GET /patients/{id}/audit``. A malformed id failed duller and louder: both columns are foreign
keys, so it reached ``flush`` as an IntegrityError and 500ed.

The sweep at the bottom is the general form, driven off the live router table and each route's
Pydantic body model, so the next endpoint to accept a patient id in a body is covered the moment
it is registered rather than the next time someone thinks to look.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient

from tests.conftest import create_patient

REPORT = {
    "category": "wrong_suggestion",
    "severity": "near_miss",
    "description": "The panel proposed a drug the chart contraindicates.",
}


async def _start_session(client: AsyncClient, patient_id: str) -> str:
    resp = await client.post(
        f"/api/v1/patients/{patient_id}/reasoning",
        json={"presenting_complaint": "fever and cough for three days"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session"]["id"]


# --- the hole -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_safety_report_cannot_name_another_accounts_patient(
    auth_client, second_auth_client
) -> None:
    """The bug. Account B files a report against account A's chart and it is accepted."""
    patient = await create_patient(auth_client)

    resp = await second_auth_client.post(
        "/api/v1/safety-reports", json={**REPORT, "patient_id": patient["id"]}
    )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_the_rejected_report_is_not_filed_at_all(auth_client, second_auth_client) -> None:
    """A 404 that still wrote the row would leave the register holding the linkage anyway."""
    patient = await create_patient(auth_client)

    await second_auth_client.post(
        "/api/v1/safety-reports", json={**REPORT, "patient_id": patient["id"]}
    )

    listed = await second_auth_client.get("/api/v1/safety-reports")
    assert listed.status_code == 200, listed.text
    assert listed.json() == []


@pytest.mark.asyncio
async def test_the_rejected_report_writes_nothing_to_the_victims_audit_trail(
    auth_client, second_auth_client
) -> None:
    """What the hole actually cost: an unremovable entry on someone else's chart.

    The trail is append-only and hash-chained, so an entry written here could never be taken
    back out — and its owner reads it as a record of who touched their patient.
    """
    patient = await create_patient(auth_client)

    await second_auth_client.post(
        "/api/v1/safety-reports", json={**REPORT, "patient_id": patient["id"]}
    )

    trail = await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")
    assert trail.status_code == 200, trail.text
    actions = [entry["action"] for entry in trail.json()["items"]]
    assert "safety_report_filed" not in actions


@pytest.mark.asyncio
async def test_a_safety_report_naming_a_patient_that_does_not_exist_is_a_404_not_a_500(
    auth_client,
) -> None:
    """``patient_id`` is a foreign key, so an unknown id used to surface as an IntegrityError."""
    resp = await auth_client.post(
        "/api/v1/safety-reports", json={**REPORT, "patient_id": str(uuid.uuid4())}
    )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_a_safety_report_naming_an_unknown_session_is_a_404_not_a_500(auth_client) -> None:
    """The same for the other foreign key."""
    resp = await auth_client.post(
        "/api/v1/safety-reports", json={**REPORT, "session_id": str(uuid.uuid4())}
    )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_a_safety_report_cannot_name_another_accounts_reasoning_session(
    auth_client, second_auth_client
) -> None:
    """A session id links the report to a run, and through it to that run's patient."""
    patient = await create_patient(auth_client)
    session_id = await _start_session(auth_client, patient["id"])

    resp = await second_auth_client.post(
        "/api/v1/safety-reports", json={**REPORT, "session_id": session_id}
    )

    assert resp.status_code == 404, resp.text


# --- what must keep working -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_report_on_the_accounts_own_chart_is_filed(auth_client) -> None:
    """The ordinary case the register exists for."""
    patient = await create_patient(auth_client)

    resp = await auth_client.post(
        "/api/v1/safety-reports", json={**REPORT, "patient_id": patient["id"]}
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["patient_id"] == patient["id"]


@pytest.mark.asyncio
async def test_a_report_with_no_links_is_still_accepted(auth_client) -> None:
    """Both links are optional — a near-miss that names no chart is still a near-miss."""
    resp = await auth_client.post("/api/v1/safety-reports", json=REPORT)

    assert resp.status_code == 201, resp.text
    assert resp.json()["patient_id"] is None


@pytest.mark.asyncio
async def test_a_report_on_a_withdrawn_chart_is_still_accepted(auth_client) -> None:
    """Deliberately not gated on ``is_deleted``.

    A near-miss on a chart that has since been withdrawn is exactly what post-market
    surveillance is for, and the report is about what the system did — it is not a way back
    into the record, which stays 404 at every clinical route.
    """
    patient = await create_patient(auth_client)
    assert (await auth_client.delete(f"/api/v1/patients/{patient['id']}")).status_code in (200, 204)

    resp = await auth_client.post(
        "/api/v1/safety-reports", json={**REPORT, "patient_id": patient["id"]}
    )

    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_the_report_reaches_the_owners_audit_trail(auth_client) -> None:
    """The linkage the register is for: a report investigable against the chart's own trail."""
    patient = await create_patient(auth_client)
    await auth_client.post("/api/v1/safety-reports", json={**REPORT, "patient_id": patient["id"]})

    trail = await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")
    actions = [entry["action"] for entry in trail.json()["items"]]
    assert "safety_report_filed" in actions


# --- the general form -------------------------------------------------------------------------


def _body_routes_taking_a_patient_id(app) -> list[tuple[str, str]]:
    """(method, path) for every route whose request body declares a ``patient_id`` field."""
    out: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.body_field is None:
            continue
        model = getattr(route.body_field, "type_", None)
        fields = getattr(model, "model_fields", None)
        if not fields or "patient_id" not in fields:
            continue
        if "{patient_id}" in route.path:
            continue  # already covered by the path sweep in test_route_authz
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, route.path))
    return out


@pytest.mark.asyncio
async def test_the_body_sweep_still_finds_something_to_check(app) -> None:
    """A sweep that silently matches nothing proves nothing."""
    assert _body_routes_taking_a_patient_id(app), (
        "no route takes patient_id in a body any more — delete this sweep, or fix the "
        "introspection that stopped finding them"
    )


@pytest.mark.asyncio
async def test_no_route_accepts_another_accounts_patient_id_in_its_body(
    app, auth_client, second_auth_client
) -> None:
    """The path sweep's counterpart. Account B must not reach account A's chart through a body."""
    patient = await create_patient(auth_client)
    # Minimal bodies keyed by path; a route added later with different required fields will
    # 422 here, which this treats as safe — it never reached the patient.
    bodies = {"/api/v1/safety-reports": REPORT}

    leaked: list[str] = []
    for method, path in _body_routes_taking_a_patient_id(app):
        body = {**bodies.get(path, {}), "patient_id": patient["id"]}
        resp = await second_auth_client.request(method, path, json=body)
        if resp.status_code < 400:
            leaked.append(f"{method} {path} -> {resp.status_code}")

    assert not leaked, "cross-account access via request body: " + "; ".join(leaked)
