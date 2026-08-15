"""The encounter lifecycle: draft -> in_progress -> signed, and amendment after that.

What these tests are protecting is a single claim: **once a clinician signs a visit note, what
that note says can never change**. Everything else here — the transitions, the reasons, the
audit entries — exists to make that claim usable rather than merely true. A record that can be
rewritten silently cannot answer the question it exists to answer, which is what the chart said
at the moment a decision was taken.

So the file is organised around the ways that claim could be broken:

* by editing a signed encounter directly (``PATCH``), or by walking its status backwards;
* by signing twice, which leaves two attestations and no rule for which one the chart means;
* by amending without saying why, which spends the one amendment a visit allows and records
  nothing a later reader can use;
* by two clinicians amending the same visit at once, which would leave two successors to one
  consultation;
* by an amendment that is only *drafted* nevertheless retiring the original — a correction
  nobody signed silently displacing one somebody did.

The service-level guard is what runs here, because the suite runs on SQLite. The database-level
guard — ``trg_encounters_signed_frozen``, which refuses the UPDATE whatever issues it — is
PostgreSQL-only and is covered in ``test_encounter_freeze_postgres.py``.
"""

from __future__ import annotations

import pytest

from tests.conftest import create_patient

pytestmark = pytest.mark.asyncio

REASON = "Potassium was transcribed as 3.4; the source report reads 5.4. Correcting the note."


async def _chart(auth_client) -> str:
    patient = await create_patient(auth_client)
    return patient["id"]


async def _open(auth_client, patient_id: str, **overrides) -> dict:
    body = {
        "encounter_date": "2026-08-14",
        "encounter_type": "outpatient",
        "presenting_complaint": "Breathless on exertion for three weeks",
        "clinician_notes": "BP 148/92. Bibasal crackles.",
    }
    body.update(overrides)
    resp = await auth_client.post(f"/api/v1/patients/{patient_id}/encounters", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _sign(auth_client, patient_id: str, encounter_id: str):
    return await auth_client.post(f"/api/v1/patients/{patient_id}/encounters/{encounter_id}/sign")


async def _amend(auth_client, patient_id: str, encounter_id: str, **overrides):
    body = {"amendment_reason": REASON}
    body.update(overrides)
    return await auth_client.post(
        f"/api/v1/patients/{patient_id}/encounters/{encounter_id}/amend", json=body
    )


# --- opening and editing ------------------------------------------------------------------


async def test_a_new_visit_opens_as_an_unsigned_draft(auth_client):
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)

    assert encounter["status"] == "draft"
    # No attestation until somebody signs. A draft that arrived carrying a signer would be a
    # signature nobody gave.
    assert encounter["signed_at"] is None
    assert encounter["signed_by_account_id"] is None
    assert encounter["amends_encounter_id"] is None


async def test_a_draft_can_be_edited_and_moved_to_in_progress(auth_client):
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)

    resp = await auth_client.patch(
        f"/api/v1/patients/{patient_id}/encounters/{encounter['id']}",
        json={
            "clinician_notes": "BP 148/92. Bibasal crackles. JVP raised.",
            "status": "in_progress",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "in_progress"
    assert "JVP raised" in resp.json()["clinician_notes"]
    # The complaint was not in the body and must be exactly as it was: a partial update that
    # blanks the fields it was not given is how half a consultation disappears.
    assert resp.json()["presenting_complaint"] == "Breathless on exertion for three weeks"


async def test_the_edit_route_will_not_sign(auth_client):
    """``status`` on the PATCH body accepts only the two unsigned states.

    Signing through a generic edit would be a signature with no ``encounter_signed`` entry in
    the trail and no signer on the row — the attestation would exist and be unattributable.
    """
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)

    resp = await auth_client.patch(
        f"/api/v1/patients/{patient_id}/encounters/{encounter['id']}",
        json={"status": "signed"},
    )
    assert resp.status_code == 422, resp.text


# --- signing ------------------------------------------------------------------------------


async def test_signing_records_who_attested_and_when(auth_client):
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)

    resp = await _sign(auth_client, patient_id, encounter["id"])
    assert resp.status_code == 200, resp.text
    signed = resp.json()
    assert signed["status"] == "signed"
    assert signed["signed_at"] is not None
    assert signed["signed_by_account_id"] is not None


async def test_a_signed_visit_cannot_be_edited(auth_client):
    """The claim the whole module exists for."""
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, encounter["id"])

    resp = await auth_client.patch(
        f"/api/v1/patients/{patient_id}/encounters/{encounter['id']}",
        json={"clinician_notes": "Rewritten after the fact."},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "encounter_signed"

    # And nothing was written. A 409 over a partial write would be the worst of both.
    current = await auth_client.get(f"/api/v1/patients/{patient_id}/encounters/{encounter['id']}")
    assert current.json()["clinician_notes"] == "BP 148/92. Bibasal crackles."


async def test_a_signed_visit_cannot_be_walked_back_to_a_draft(auth_client):
    """The same edit wearing a different hat: move it back to draft, then edit it freely."""
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, encounter["id"])

    resp = await auth_client.patch(
        f"/api/v1/patients/{patient_id}/encounters/{encounter['id']}",
        json={"status": "draft"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "encounter_signed"


async def test_signing_twice_is_refused(auth_client):
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, encounter["id"])

    resp = await _sign(auth_client, patient_id, encounter["id"])
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "encounter_transition"


async def test_the_first_signature_survives_a_second_attempt(auth_client):
    """A refused second signature must not have moved the timestamp on the way out."""
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)
    first = (await _sign(auth_client, patient_id, encounter["id"])).json()

    await _sign(auth_client, patient_id, encounter["id"])

    current = await auth_client.get(f"/api/v1/patients/{patient_id}/encounters/{encounter['id']}")
    assert current.json()["signed_at"] == first["signed_at"]


# --- amendment ----------------------------------------------------------------------------


async def test_an_unsigned_visit_cannot_be_amended(auth_client):
    """There is nothing to preserve, so the correction belongs in the draft itself."""
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)

    resp = await _amend(auth_client, patient_id, encounter["id"])
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "encounter_transition"


async def test_an_amendment_is_a_new_draft_that_names_what_it_supersedes(auth_client):
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, original["id"])

    resp = await _amend(
        auth_client,
        patient_id,
        original["id"],
        clinician_notes="BP 148/92. Bibasal crackles. Potassium 5.4.",
    )
    assert resp.status_code == 201, resp.text
    amendment = resp.json()

    assert amendment["id"] != original["id"]
    assert amendment["status"] == "draft"
    assert amendment["amends_encounter_id"] == original["id"]
    assert amendment["amendment_reason"] == REASON
    assert "5.4" in amendment["clinician_notes"]


async def test_an_amendment_inherits_the_fields_it_does_not_restate(auth_client):
    """An amendment fixing only the note must still read as a complete version of the visit."""
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, original["id"])

    amendment = (
        await _amend(auth_client, patient_id, original["id"], clinician_notes="Corrected.")
    ).json()

    assert amendment["encounter_date"] == original["encounter_date"]
    assert amendment["encounter_type"] == original["encounter_type"]
    assert amendment["presenting_complaint"] == original["presenting_complaint"]


async def test_an_explicit_null_clears_a_field_an_omission_would_have_kept(auth_client):
    """``exclude_unset``, not ``is None``: omitting a field and blanking it are different asks.

    Collapsing them would make "the complaint was recorded against the wrong visit, remove it"
    inexpressible — the clinician would have to leave prose on the chart they know is wrong.
    """
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, original["id"])

    amendment = (
        await _amend(auth_client, patient_id, original["id"], presenting_complaint=None)
    ).json()

    assert amendment["presenting_complaint"] is None
    assert amendment["clinician_notes"] == original["clinician_notes"]


async def test_an_amendment_without_a_real_reason_is_refused(auth_client):
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, original["id"])

    for blank in ("", "   ", "typo", ".\n.\n."):
        resp = await _amend(auth_client, patient_id, original["id"], amendment_reason=blank)
        assert resp.status_code == 422, f"{blank!r} was accepted as a reason"


async def test_the_original_is_untouched_while_the_amendment_is_only_drafted(auth_client):
    """A correction nobody signed must not displace one somebody did."""
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, original["id"])
    await _amend(auth_client, patient_id, original["id"], clinician_notes="Corrected.")

    current = await auth_client.get(f"/api/v1/patients/{patient_id}/encounters/{original['id']}")
    assert current.json()["status"] == "signed"
    assert current.json()["amended_at"] is None
    assert current.json()["clinician_notes"] == "BP 148/92. Bibasal crackles."


async def test_signing_the_amendment_is_what_retires_the_original(auth_client):
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, original["id"])
    amendment = (
        await _amend(auth_client, patient_id, original["id"], clinician_notes="Corrected.")
    ).json()

    resp = await _sign(auth_client, patient_id, amendment["id"])
    assert resp.status_code == 200, resp.text

    superseded = (
        await auth_client.get(f"/api/v1/patients/{patient_id}/encounters/{original['id']}")
    ).json()
    assert superseded["status"] == "amended"
    assert superseded["amended_at"] is not None
    # And its content is exactly what was attested to. The amendment carries the correction;
    # the original carries what the chart said when the decision was made.
    assert superseded["clinician_notes"] == "BP 148/92. Bibasal crackles."


async def test_only_one_amendment_of_a_visit_can_be_signed(auth_client):
    """Two clinicians may both open an amendment; only one can become the successor.

    Drafts are deliberately unconstrained — two people noticing the same error is ordinary — but
    "the current version of this visit" has to be a question the chart can answer.
    """
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, original["id"])

    first = (await _amend(auth_client, patient_id, original["id"], clinician_notes="A")).json()
    second = (await _amend(auth_client, patient_id, original["id"], clinician_notes="B")).json()

    assert (await _sign(auth_client, patient_id, first["id"])).status_code == 200
    losing = await _sign(auth_client, patient_id, second["id"])
    assert losing.status_code == 409, losing.text
    assert losing.json()["code"] == "encounter_already_amended"

    # Nothing the losing clinician typed was destroyed: it is still there as a draft.
    still_there = await auth_client.get(f"/api/v1/patients/{patient_id}/encounters/{second['id']}")
    assert still_there.json()["clinician_notes"] == "B"
    assert still_there.json()["status"] == "draft"


async def test_an_amended_visit_cannot_be_amended_again(auth_client):
    """The chain is linear: amend the current version, not the one it replaced."""
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, original["id"])
    amendment = (await _amend(auth_client, patient_id, original["id"])).json()
    await _sign(auth_client, patient_id, amendment["id"])

    resp = await _amend(auth_client, patient_id, original["id"])
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "encounter_already_amended"


async def test_an_amendment_can_itself_be_amended(auth_client):
    """The chain continues forward. A note corrected once may need correcting again."""
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, original["id"])
    first = (await _amend(auth_client, patient_id, original["id"], clinician_notes="A")).json()
    await _sign(auth_client, patient_id, first["id"])

    second = await _amend(auth_client, patient_id, first["id"], clinician_notes="B")
    assert second.status_code == 201, second.text
    assert second.json()["amends_encounter_id"] == first["id"]


# --- listing and scoping ------------------------------------------------------------------


async def test_the_list_shows_drafts_and_superseded_visits_alongside_the_current_one(auth_client):
    """Nothing is filtered out. A draft nobody signed is still part of what the chart holds,
    and the note as attested is the reason the amendment mechanism exists."""
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, original["id"])
    amendment = (await _amend(auth_client, patient_id, original["id"])).json()
    await _sign(auth_client, patient_id, amendment["id"])
    unsigned = await _open(auth_client, patient_id, encounter_date="2026-08-01")

    resp = await auth_client.get(f"/api/v1/patients/{patient_id}/encounters")
    assert resp.status_code == 200, resp.text
    by_id = {item["id"]: item for item in resp.json()["items"]}

    assert by_id[original["id"]]["status"] == "amended"
    assert by_id[amendment["id"]]["status"] == "signed"
    assert by_id[unsigned["id"]]["status"] == "draft"
    assert resp.json()["pagination"]["total"] == 3


async def test_the_list_is_newest_first(auth_client):
    patient_id = await _chart(auth_client)
    await _open(auth_client, patient_id, encounter_date="2026-01-04")
    await _open(auth_client, patient_id, encounter_date="2026-08-14")
    await _open(auth_client, patient_id, encounter_date="2026-05-30")

    resp = await auth_client.get(f"/api/v1/patients/{patient_id}/encounters")
    dates = [item["encounter_date"] for item in resp.json()["items"]]
    assert dates == ["2026-08-14", "2026-05-30", "2026-01-04"]


async def test_paging_a_chart_with_tied_dates_neither_repeats_nor_skips(auth_client):
    """The ordering column is a Date, so ties are the ordinary case, not the edge one.

    LIMIT/OFFSET over a partial order is a lottery: page 2 can repeat a row page 1 showed and
    silently skip another. Same defect, same fix, as the patient list.
    """
    patient_id = await _chart(auth_client)
    for _ in range(6):
        await _open(auth_client, patient_id, encounter_date="2026-08-14")

    seen: list[str] = []
    for offset in (0, 2, 4):
        resp = await auth_client.get(
            f"/api/v1/patients/{patient_id}/encounters",
            params={"limit": 2, "offset": offset},
        )
        seen.extend(item["id"] for item in resp.json()["items"])

    assert len(seen) == 6
    assert len(set(seen)) == 6


async def test_a_visit_from_another_chart_is_not_reachable_through_this_one(auth_client):
    """Encounter reads are scoped to the patient in the path as well as to the encounter id.

    Without that, a valid encounter id from chart A opened under chart B's URL would return
    chart A's note — and write chart A's disclosure into chart B's audit trail, which is the
    class of bug ``test_body_scoped_patient_references`` exists for.
    """
    first = await _chart(auth_client)
    second = await _chart(auth_client)
    encounter = await _open(auth_client, first)

    resp = await auth_client.get(f"/api/v1/patients/{second}/encounters/{encounter['id']}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "encounter_not_found"


async def test_a_future_visit_date_is_refused(auth_client):
    """A mistyped year sorts an empty consultation to the top of the chart."""
    patient_id = await _chart(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient_id}/encounters",
        json={"encounter_date": "2099-01-01"},
    )
    assert resp.status_code == 422, resp.text


# --- the trail ----------------------------------------------------------------------------


async def _actions(auth_client, patient_id: str) -> list[dict]:
    resp = await auth_client.get(f"/api/v1/patients/{patient_id}/audit", params={"limit": 200})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


async def test_every_step_of_the_lifecycle_leaves_a_trail_entry(auth_client):
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await auth_client.patch(
        f"/api/v1/patients/{patient_id}/encounters/{original['id']}",
        json={"status": "in_progress"},
    )
    await _sign(auth_client, patient_id, original["id"])
    amendment = (await _amend(auth_client, patient_id, original["id"])).json()
    await _sign(auth_client, patient_id, amendment["id"])

    actions = {entry["action"] for entry in await _actions(auth_client, patient_id)}
    assert {
        "encounter_created",
        "encounter_updated",
        "encounter_signed",
        "encounter_amendment_opened",
        "encounter_amended",
    } <= actions


async def test_the_amendment_entry_is_recorded_against_the_visit_it_retired(auth_client):
    """So that walking one visit's trail shows it stopping being current, and what replaced it,
    without scanning the whole chart for a row pointing back at it."""
    patient_id = await _chart(auth_client)
    original = await _open(auth_client, patient_id)
    await _sign(auth_client, patient_id, original["id"])
    amendment = (await _amend(auth_client, patient_id, original["id"])).json()
    await _sign(auth_client, patient_id, amendment["id"])

    entries = [
        entry
        for entry in await _actions(auth_client, patient_id)
        if entry["action"] == "encounter_amended"
    ]
    assert len(entries) == 1
    assert entries[0]["entity_id"] == original["id"]
    assert entries[0]["payload"]["amended_by_encounter_id"] == amendment["id"]


async def test_reading_a_visit_is_itself_a_recorded_disclosure(auth_client):
    """The response carries the complaint and the notes, so the read is a disclosure — and the
    question this trail is asked months later is "who saw this"."""
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)

    await auth_client.get(f"/api/v1/patients/{patient_id}/encounters/{encounter['id']}")
    await auth_client.get(f"/api/v1/patients/{patient_id}/encounters")

    actions = {entry["action"] for entry in await _actions(auth_client, patient_id)}
    assert {"encounter_viewed", "encounter_list_viewed"} <= actions


async def test_the_trail_records_which_fields_an_edit_touched_and_not_what_they_say(auth_client):
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)
    secret = "Patient disclosed intravenous drug use in 2019"

    await auth_client.patch(
        f"/api/v1/patients/{patient_id}/encounters/{encounter['id']}",
        json={"clinician_notes": secret},
    )

    entries = [
        entry
        for entry in await _actions(auth_client, patient_id)
        if entry["action"] == "encounter_updated"
    ]
    assert entries[0]["payload"]["fields"] == ["clinician_notes"]
    assert secret not in str(entries[0]["payload"])


# --- consent ------------------------------------------------------------------------------


async def test_a_withdrawn_chart_stops_accepting_visits_but_stays_readable(auth_client):
    """Withdrawal under the DPDP Act has to stop processing, not erase the record."""
    patient_id = await _chart(auth_client)
    encounter = await _open(auth_client, patient_id)

    resp = await auth_client.patch(f"/api/v1/patients/{patient_id}", json={"consent_given": False})
    assert resp.status_code == 200, resp.text

    blocked = await auth_client.post(
        f"/api/v1/patients/{patient_id}/encounters",
        json={"encounter_date": "2026-08-14"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "consent_withdrawn"

    readable = await auth_client.get(f"/api/v1/patients/{patient_id}/encounters/{encounter['id']}")
    assert readable.status_code == 200
