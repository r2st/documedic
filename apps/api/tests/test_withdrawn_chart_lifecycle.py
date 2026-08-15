"""What a withdrawn chart may still show, and to whom.

``DELETE /patients/{id}`` is a soft delete: the chart stops being usable, the rows survive
underneath, and the withdrawal is itself appended to the patient's audit trail. Three separate
questions fall out of that, and this module pins all three because they were answered
inconsistently:

* **Clinical content must stop resolving.** Every patient-scoped route reaches its rows through
  ``PatientService.get``, which filters ``is_deleted`` — except the reasoning routes, which were
  scoped by *session* id and never looked at the patient again after the session was opened. A
  session id kept from before the withdrawal was therefore a working door into the chart, and
  ``case_state`` carries ``patient_graph_snapshot``: the entire longitudinal record, frozen, and
  exactly the payload ``GET /patients/{id}/record`` had just started refusing.

* **The audit trail must keep resolving.** It is append-only, hash-chained and never pruned
  precisely so it outlives the record — and the only route to it was gated on the chart not
  being withdrawn, so the withdrawal made its own record unreachable along with every access
  logged before it. ``GET ../audit/verify``, the tamper check, went with it.

* **Both answers must hold for every clinician on the account**, not only the one who withdrew
  the chart. Two clinicians share a practice login (``colleague_client``), and the one with the
  Reasoning Theatre already open is the one who would otherwise keep reading.
"""

from __future__ import annotations

import pytest

from tests.conftest import create_patient


async def _open_session(client, patient_id: str) -> str:
    resp = await client.post(
        f"/api/v1/patients/{patient_id}/reasoning",
        json={"presenting_complaint": "Fever and cough for four days"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["session"]["id"]


# --------------------------------------------------------------- clinical content closes

# Every reasoning route that takes a session id. Named as (method, path suffix, body) so a route
# added later without a patient check shows up here as a missing entry rather than as a silent
# hole — the whole failure this pins is one route out of eight being forgotten.
_SESSION_ROUTES = [
    ("GET", "", None),
    ("GET", "/intake", None),
    ("GET", "/suggestions", None),
    ("POST", "/intake/answers", {"answers": []}),
    ("POST", "/run", None),
    ("POST", "/stream-token", None),
]


@pytest.mark.parametrize(("method", "suffix", "body"), _SESSION_ROUTES)
@pytest.mark.asyncio
async def test_a_withdrawn_chart_stops_resolving_through_its_reasoning_session(
    auth_client, method, suffix, body
):
    """A session id from before the withdrawal is not a way back into the chart."""
    patient = await create_patient(auth_client)
    session_id = await _open_session(auth_client, patient["id"])
    url = f"/api/v1/reasoning/{session_id}{suffix}"

    # The route works while the chart is in use — otherwise this test would pass for the wrong
    # reason (a typo in the URL is also a 404).
    before = await auth_client.request(method, url, json=body)
    assert before.status_code < 400, before.text

    assert (await auth_client.delete(f"/api/v1/patients/{patient['id']}")).status_code == 200

    after = await auth_client.request(method, url, json=body)
    assert after.status_code == 404, f"{method} {suffix} still answered: {after.text}"
    assert after.json()["code"] == "reasoning_session_not_found"


@pytest.mark.asyncio
async def test_a_withdrawn_charts_frozen_snapshot_is_not_readable(auth_client):
    """The specific disclosure: ``case_state`` holds a copy of the whole record.

    ``GET /patients/{id}/record`` and ``GET /reasoning/{id}`` return the same clinical facts —
    one live, one frozen at the moment the session was opened. Refusing the first and serving
    the second is not a smaller disclosure, it is the same one a step later.
    """
    patient = await create_patient(auth_client)
    session_id = await _open_session(auth_client, patient["id"])

    opened = await auth_client.get(f"/api/v1/reasoning/{session_id}")
    assert opened.status_code == 200
    # The presenting complaint is free text a clinician typed about this patient, and it is on
    # the session row that the route returns.
    assert "Fever and cough" in opened.text

    await auth_client.delete(f"/api/v1/patients/{patient['id']}")

    assert (await auth_client.get(f"/api/v1/patients/{patient['id']}/record")).status_code == 404
    withdrawn = await auth_client.get(f"/api/v1/reasoning/{session_id}")
    assert withdrawn.status_code == 404
    assert "Fever and cough" not in withdrawn.text


@pytest.mark.asyncio
async def test_a_decision_cannot_be_recorded_against_a_withdrawn_chart(auth_client, db):
    """The write side. Recording a clinician decision is a new row about this patient."""
    import uuid

    from app.models.clinical_suggestion import ClinicalSuggestion

    patient = await create_patient(auth_client)
    session_id = await _open_session(auth_client, patient["id"])
    suggestion = ClinicalSuggestion(
        session_id=uuid.UUID(session_id),
        patient_id=uuid.UUID(patient["id"]),
        output_type="differential",
        autonomy_tier="suggestive",
        title="Community-acquired pneumonia",
    )
    db.add(suggestion)
    await db.commit()

    await auth_client.delete(f"/api/v1/patients/{patient['id']}")

    resp = await auth_client.post(
        f"/api/v1/reasoning/{session_id}/suggestions/{suggestion.id}/decision",
        json={"decision": "accepted"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "reasoning_session_not_found"


@pytest.mark.asyncio
async def test_a_colleague_with_the_theatre_open_is_closed_out_too(auth_client, colleague_client):
    """Withdrawal binds the whole account, not the client that issued it.

    Two clinicians on one practice login. One withdraws the chart; the other is mid-session with
    the session id already in hand, which is the only way this hole was ever reached in practice.
    """
    patient = await create_patient(auth_client)
    session_id = await _open_session(colleague_client, patient["id"])
    assert (await colleague_client.get(f"/api/v1/reasoning/{session_id}")).status_code == 200

    await auth_client.delete(f"/api/v1/patients/{patient['id']}")

    assert (await colleague_client.get(f"/api/v1/reasoning/{session_id}")).status_code == 404
    suggestions = await colleague_client.get(f"/api/v1/reasoning/{session_id}/suggestions")
    assert suggestions.status_code == 404


@pytest.mark.asyncio
async def test_withdrawal_does_not_erase_the_session_rows_underneath(auth_client, db):
    """Closed to the API, still present in the database.

    The distinction matters: the immutable ``ClinicalSuggestion`` rows and the session they hang
    under are referenced by the audit chain, so hiding them is correct and deleting them is not.
    """
    import uuid

    from sqlalchemy import select

    from app.models.reasoning_session import ReasoningSession

    patient = await create_patient(auth_client)
    session_id = await _open_session(auth_client, patient["id"])
    await auth_client.delete(f"/api/v1/patients/{patient['id']}")

    row = (
        await db.execute(
            select(ReasoningSession).where(ReasoningSession.id == uuid.UUID(session_id))
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.is_deleted is False, "the session itself was not deleted, only its chart withdrawn"


# --------------------------------------------------------------- the audit trail stays open


@pytest.mark.asyncio
async def test_the_audit_trail_survives_the_withdrawal_that_is_recorded_in_it(auth_client):
    """The trail must outlive the chart, including the ``patient_deleted`` entry itself."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    await auth_client.get(f"/api/v1/patients/{pid}")  # a read to have something to account for

    before = await auth_client.get(f"/api/v1/patients/{pid}/audit")
    assert before.status_code == 200
    count_before = before.json()["pagination"]["total"]
    assert count_before > 0

    assert (await auth_client.delete(f"/api/v1/patients/{pid}")).status_code == 200

    after = await auth_client.get(f"/api/v1/patients/{pid}/audit")
    assert after.status_code == 200, "the trail became unreachable the moment it mattered most"
    actions = [entry["action"] for entry in after.json()["items"]]
    assert "patient_deleted" in actions
    assert "patient_created" in actions
    assert after.json()["pagination"]["total"] == count_before + 1


@pytest.mark.asyncio
async def test_the_tamper_check_still_runs_on_a_withdrawn_chart(auth_client):
    """A hash-chain verification that stops working once a record is deleted verifies nothing."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    await auth_client.delete(f"/api/v1/patients/{pid}")

    resp = await auth_client.get(f"/api/v1/patients/{pid}/audit/verify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["chain_valid"] is True
    assert body["entries_checked"] > 0


@pytest.mark.asyncio
async def test_widening_the_audit_read_did_not_widen_it_across_accounts(
    auth_client, second_auth_client
):
    """The ``is_deleted`` predicate was dropped; the ``account_id`` one was not.

    Worth its own test rather than trusting the diff: this is the one place in the API that
    deliberately serves rows for a chart marked deleted, so it is the one place where losing the
    tenancy predicate would be least likely to show up anywhere else.
    """
    patient = await create_patient(auth_client)
    pid = patient["id"]
    await auth_client.delete(f"/api/v1/patients/{pid}")

    assert (await second_auth_client.get(f"/api/v1/patients/{pid}/audit")).status_code == 404
    assert (await second_auth_client.get(f"/api/v1/patients/{pid}/audit/verify")).status_code == 404


@pytest.mark.asyncio
async def test_the_clinical_reads_stayed_closed_when_the_audit_read_opened(auth_client):
    """The counterpart to the test above, in the other direction.

    ``get_for_audit`` exists so the trail survives withdrawal. Nothing else may follow it: the
    trail holds identifiers and counts by design, and the routes below hold the chart.
    """
    patient = await create_patient(auth_client)
    pid = patient["id"]
    await auth_client.delete(f"/api/v1/patients/{pid}")

    for url in (
        f"/api/v1/patients/{pid}",
        f"/api/v1/patients/{pid}/record",
        f"/api/v1/patients/{pid}/drug-safety/flags",
        f"/api/v1/patients/{pid}/labs/critical-flags",
        f"/api/v1/patients/{pid}/documents",
    ):
        resp = await auth_client.get(url)
        assert resp.status_code == 404, f"{url} answered for a withdrawn chart: {resp.text}"
