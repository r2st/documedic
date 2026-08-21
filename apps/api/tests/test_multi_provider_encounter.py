"""Multi-provider encounters: sharing one consultation, and the roles that come with it.

The load-bearing test is ``test_a_participant_reaches_the_encounter_and_nothing_else_on_the
_chart``. Everything this feature adds is a new door into clinical data owned by somebody else,
and the whole design rests on that door opening onto exactly one room.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.encounter_roles import (
    ENCOUNTER_ROLES,
    ROLES_FROZEN_BY_SIGNATURE,
    capabilities_for,
    may_read,
    may_sign,
)
from app.models.audit_log import AuditLog
from app.models.encounter_participant import EncounterParticipant
from tests.conftest import create_patient

SECOND_EMAIL = "doc2@example.com"


# --- The capability table -----------------------------------------------------------------


def test_every_role_reads_and_exactly_two_attest():
    """A role added later that forgets to say whether it may sign inherits "may not" — the sets
    are enumerated per role rather than defaulted."""
    assert all(may_read(role) for role in ENCOUNTER_ROLES)
    assert {role for role in ENCOUNTER_ROLES if may_sign(role)} == {"author", "supervising"}


def test_an_unrecognised_role_may_do_nothing():
    """Fail-closed. A typo'd role that reads as "may do everything" is the failure this whole
    module exists to make impossible."""
    assert may_sign("administrator") is False
    assert may_read("") is False
    with pytest.raises(KeyError):
        capabilities_for("administrator")


def test_the_roles_frozen_by_a_signature_are_exactly_the_ones_that_assert_attendance():
    """`consulting` is deliberately absent: a second opinion sought a week later is an ordinary
    thing to record against a signed note, and it changes no attestation."""
    assert ROLES_FROZEN_BY_SIGNATURE == frozenset({"author", "supervising"})


def test_the_migrations_role_list_matches_the_application_vocabulary():
    """0042 spells the check constraint literally, on purpose — a migration records what the
    schema became. This is what holds the two together as the vocabulary grows."""
    import pathlib

    migration = (
        pathlib.Path(__file__).resolve().parents[3]
        / "data/migrations/versions/0042_encounter_participants.py"
    ).read_text()
    for role in ENCOUNTER_ROLES:
        assert f"'{role}'" in migration, role
    line = next(ln for ln in migration.splitlines() if ln.startswith("_ROLE_VALUES"))
    assert line.count("'") == 2 * len(ENCOUNTER_ROLES)


# --- Granting -----------------------------------------------------------------------------


async def _visit(client, patient_id: str, **overrides) -> dict:
    payload = {
        "encounter_date": "2026-01-05",
        "encounter_type": "outpatient",
        "presenting_complaint": "Chest pain on exertion",
        "clinician_notes": "ECG normal. Troponin pending.",
    }
    payload.update(overrides)
    resp = await client.post(f"/api/v1/patients/{patient_id}/encounters", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _share(client, patient_id: str, encounter_id: str, **overrides):
    payload = {
        "email": SECOND_EMAIL,
        "role": "consulting",
        "purpose": "Cardiology opinion on the exertional chest pain.",
    }
    payload.update(overrides)
    return await client.post(
        f"/api/v1/patients/{patient_id}/encounters/{encounter_id}/participants", json=payload
    )


async def test_sharing_a_consultation_records_the_role_and_the_purpose(
    auth_client, second_auth_client
):
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    resp = await _share(auth_client, patient["id"], visit["id"], role="supervising")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "supervising"
    assert body["may_sign"] is True
    assert body["email"] == SECOND_EMAIL
    assert body["purpose"].startswith("Cardiology opinion")
    assert body["removed_at"] is None


async def test_a_purpose_is_required_and_a_blank_one_is_a_422(auth_client, second_auth_client):
    """An access grant to a clinical record that cannot say why it was made is the row nobody
    can justify at a review."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    resp = await _share(auth_client, patient["id"], visit["id"], purpose="   ")
    assert resp.status_code == 422


async def test_an_address_with_no_account_is_a_404_not_a_silent_success(auth_client):
    """Telling a clinician the record was shared when it was not is how a consultation ends up
    being emailed instead."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    resp = await _share(auth_client, patient["id"], visit["id"], email="nobody@example.com")
    assert resp.status_code == 404
    assert resp.json()["code"] == "participant_account_not_found"


async def test_the_email_lookup_is_case_insensitive(auth_client, second_auth_client):
    """Accounts are stored lowercased; a lookup that did not match would report "no such
    colleague" for an address differing only in case."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    resp = await _share(auth_client, patient["id"], visit["id"], email="Doc2@Example.COM")
    assert resp.status_code == 201, resp.text


async def test_an_unknown_role_is_rejected_by_the_request_model(auth_client, second_auth_client):
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    resp = await _share(auth_client, patient["id"], visit["id"], role="administrator")
    assert resp.status_code == 422


async def test_a_second_live_grant_to_the_same_colleague_is_a_409(auth_client, second_auth_client):
    """A role change is a withdrawal and a fresh grant, so the record keeps both spells."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    assert (await _share(auth_client, patient["id"], visit["id"])).status_code == 201
    second = await _share(auth_client, patient["id"], visit["id"], role="observing")
    assert second.status_code == 409
    assert second.json()["code"] == "participant_already_present"


async def test_only_the_chart_owner_may_share_a_consultation(auth_client, second_auth_client):
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    resp = await _share(second_auth_client, patient["id"], visit["id"], email="doc@example.com")
    assert resp.status_code == 404


async def test_an_encounter_from_another_chart_cannot_be_shared_through_a_chart_i_own(
    auth_client, second_auth_client
):
    """This is the one route in the API that *hands out access*, so a cross-chart reference here
    would be a grant over somebody else's record."""
    victim = await create_patient(auth_client)
    victim_visit = await _visit(auth_client, victim["id"])
    # The attacker's own chart, on their own account, with the victim's encounter id hung off it.
    attacker_chart = await create_patient(second_auth_client, full_name="Attacker Patient")
    resp = await second_auth_client.post(
        f"/api/v1/patients/{attacker_chart['id']}/encounters/{victim_visit['id']}/participants",
        json={
            "email": SECOND_EMAIL,
            "role": "consulting",
            "purpose": "Attempting to reach another practice's consultation.",
        },
    )
    assert resp.status_code == 404


@pytest.mark.parametrize("role", sorted(ROLES_FROZEN_BY_SIGNATURE))
async def test_a_clinical_role_cannot_be_added_to_a_signed_note(
    auth_client, second_auth_client, role
):
    """Adding one afterwards rewrites who attended a visit that is already attested to."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    signed = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/encounters/{visit['id']}/sign"
    )
    assert signed.status_code == 200, signed.text
    resp = await _share(auth_client, patient["id"], visit["id"], role=role)
    assert resp.status_code == 403
    assert resp.json()["code"] == "participant_role_not_permitted"


async def test_a_consulting_role_is_still_accepted_on_a_signed_note(
    auth_client, second_auth_client
):
    """A second opinion sought a week later is ordinary, and it changes no attestation."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    await auth_client.post(f"/api/v1/patients/{patient['id']}/encounters/{visit['id']}/sign")
    resp = await _share(auth_client, patient["id"], visit["id"], role="consulting")
    assert resp.status_code == 201, resp.text


# --- The participant's view ---------------------------------------------------------------


async def test_a_participant_sees_the_visit_and_the_role_they_hold(auth_client, second_auth_client):
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    await _share(auth_client, patient["id"], visit["id"], role="supervising")

    listed = await second_auth_client.get("/api/v1/encounters/shared")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [e["id"] for e in body["encounters"]] == [visit["id"]]
    assert body["encounters"][0]["my_role"] == "supervising"
    assert body["encounters"][0]["may_sign"] is True
    assert body["encounters"][0]["clinician_notes"] == "ECG normal. Troponin pending."

    one = await second_auth_client.get(f"/api/v1/encounters/shared/{visit['id']}")
    assert one.status_code == 200, one.text
    assert one.json()["presenting_complaint"] == "Chest pain on exertion"


async def test_a_participant_reaches_the_encounter_and_nothing_else_on_the_chart(
    auth_client, second_auth_client
):
    """The load-bearing guarantee. Participation in one visit must not be widenable into access
    to a record by any combination of ids the participant holds."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    other_visit = await _visit(auth_client, patient["id"], presenting_complaint="Ankle sprain")
    await _share(auth_client, patient["id"], visit["id"])

    # The shared visit: yes.
    assert (
        await second_auth_client.get(f"/api/v1/encounters/shared/{visit['id']}")
    ).status_code == 200
    # Everything else on that chart: no.
    for path in (
        f"/api/v1/patients/{patient['id']}",
        f"/api/v1/patients/{patient['id']}/record",
        f"/api/v1/patients/{patient['id']}/encounters",
        f"/api/v1/patients/{patient['id']}/encounters/{visit['id']}",
        f"/api/v1/patients/{patient['id']}/labs/trends",
        f"/api/v1/patients/{patient['id']}/appointments",
    ):
        resp = await second_auth_client.get(path)
        assert resp.status_code == 404, f"{path} -> {resp.status_code}"
    # And the chart's *other* visit is not shared either.
    assert (
        await second_auth_client.get(f"/api/v1/encounters/shared/{other_visit['id']}")
    ).status_code == 404


async def test_the_shared_view_carries_an_age_and_never_a_date_of_birth(
    auth_client, second_auth_client
):
    """A practice given one consultation was not given the patient's identifiers. Age is what a
    clinician reads a note against; a DOB is an identifier."""
    patient = await create_patient(auth_client, date_of_birth="1968-05-10")
    visit = await _visit(auth_client, patient["id"])
    await _share(auth_client, patient["id"], visit["id"])
    body = (await second_auth_client.get(f"/api/v1/encounters/shared/{visit['id']}")).json()
    assert body["patient"]["full_name"] == "Ramesh Kumar"
    assert isinstance(body["patient"]["age_years"], int)
    assert "date_of_birth" not in body["patient"]
    assert "1968" not in str(body)


async def test_an_unshared_encounter_is_a_404_indistinguishable_from_one_that_never_existed(
    auth_client, second_auth_client
):
    """The caller is by construction outside the owning practice; a 403 would confirm that a
    given encounter id is real."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    real = await second_auth_client.get(f"/api/v1/encounters/shared/{visit['id']}")
    invented = await second_auth_client.get(f"/api/v1/encounters/shared/{uuid.uuid4()}")
    assert real.status_code == invented.status_code == 404
    assert real.json()["code"] == invented.json()["code"]


async def test_a_withdrawn_chart_disappears_from_a_participants_view(
    auth_client, second_auth_client
):
    """A share is not a copy that outlives the record."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    await _share(auth_client, patient["id"], visit["id"])
    assert (
        await second_auth_client.get(f"/api/v1/encounters/shared/{visit['id']}")
    ).status_code == 200

    deleted = await auth_client.delete(f"/api/v1/patients/{patient['id']}")
    assert deleted.status_code in (200, 204), deleted.text
    assert (
        await second_auth_client.get(f"/api/v1/encounters/shared/{visit['id']}")
    ).status_code == 404
    assert (await second_auth_client.get("/api/v1/encounters/shared")).json()["encounters"] == []


# --- Withdrawal ---------------------------------------------------------------------------


async def test_withdrawing_access_stops_it_immediately_and_keeps_the_row(
    auth_client, second_auth_client, db
):
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    participant_id = (await _share(auth_client, patient["id"], visit["id"])).json()["id"]
    assert (
        await second_auth_client.get(f"/api/v1/encounters/shared/{visit['id']}")
    ).status_code == 200

    removed = await auth_client.request(
        "DELETE",
        f"/api/v1/patients/{patient['id']}/encounters/{visit['id']}/participants/{participant_id}",
        json={"reason": "Opinion received; access no longer required."},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed_at"] is not None

    assert (
        await second_auth_client.get(f"/api/v1/encounters/shared/{visit['id']}")
    ).status_code == 404
    # The row survives: "who could see it in March" is the question a review asks.
    row = await db.get(EncounterParticipant, uuid.UUID(participant_id))
    assert row is not None
    assert row.removal_reason == "Opinion received; access no longer required."


async def test_withdrawing_an_already_withdrawn_participation_is_a_404(
    auth_client, second_auth_client
):
    """ "I have just revoked this" and "somebody revoked it in March" must not render the same."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    participant_id = (await _share(auth_client, patient["id"], visit["id"])).json()["id"]
    path = (
        f"/api/v1/patients/{patient['id']}/encounters/{visit['id']}/participants/{participant_id}"
    )
    body = {"reason": "No longer required."}
    assert (await auth_client.request("DELETE", path, json=body)).status_code == 200
    assert (await auth_client.request("DELETE", path, json=body)).status_code == 404


async def test_a_colleague_can_be_brought_back_after_being_removed(auth_client, second_auth_client):
    """The unique index is partial on the tombstone, so two spells are two rows and the gap
    between them survives."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    first = (await _share(auth_client, patient["id"], visit["id"])).json()["id"]
    await auth_client.request(
        "DELETE",
        f"/api/v1/patients/{patient['id']}/encounters/{visit['id']}/participants/{first}",
        json={"reason": "Opinion received."},
    )
    again = await _share(auth_client, patient["id"], visit["id"], role="observing")
    assert again.status_code == 201, again.text

    listed = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/encounters/{visit['id']}/participants",
        params={"include_removed": True},
    )
    assert listed.status_code == 200
    assert len(listed.json()["participants"]) == 2


async def test_the_participant_list_hides_withdrawn_rows_by_default(
    auth_client, second_auth_client
):
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    participant_id = (await _share(auth_client, patient["id"], visit["id"])).json()["id"]
    await auth_client.request(
        "DELETE",
        f"/api/v1/patients/{patient['id']}/encounters/{visit['id']}/participants/{participant_id}",
        json={"reason": "Done."},
    )
    listed = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/encounters/{visit['id']}/participants"
    )
    assert listed.json()["participants"] == []
    assert listed.json()["available_roles"] == list(ENCOUNTER_ROLES)


# --- Countersigning -----------------------------------------------------------------------


@pytest.mark.parametrize("role", ["author", "supervising"])
async def test_a_role_that_attests_may_countersign_and_the_signature_is_theirs(
    auth_client, second_auth_client, db, role
):
    """The point of the roles: a registrar writes and a consultant countersigns, and before this
    a supervised note was indistinguishable from an unsupervised one on the record."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    await _share(auth_client, patient["id"], visit["id"], role=role)

    me = await second_auth_client.get("/api/v1/auth/me")
    colleague_id = me.json()["id"]

    resp = await second_auth_client.post(f"/api/v1/encounters/shared/{visit['id']}/sign")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "signed"
    assert resp.json()["signed_by_account_id"] == colleague_id


@pytest.mark.parametrize("role", ["consulting", "observing"])
async def test_a_role_that_only_reads_is_refused_with_a_403_not_a_404(
    auth_client, second_auth_client, role
):
    """You have already been shown this encounter, so there is nothing left to conceal — and
    "you may read this and may not sign it" is what lets you do the right thing next."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    await _share(auth_client, patient["id"], visit["id"], role=role)
    resp = await second_auth_client.post(f"/api/v1/encounters/shared/{visit['id']}/sign")
    assert resp.status_code == 403
    assert resp.json()["code"] == "participant_role_not_permitted"


async def test_a_removed_participant_cannot_countersign(auth_client, second_auth_client):
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    participant_id = (
        await _share(auth_client, patient["id"], visit["id"], role="supervising")
    ).json()["id"]
    await auth_client.request(
        "DELETE",
        f"/api/v1/patients/{patient['id']}/encounters/{visit['id']}/participants/{participant_id}",
        json={"reason": "Left the team."},
    )
    resp = await second_auth_client.post(f"/api/v1/encounters/shared/{visit['id']}/sign")
    assert resp.status_code == 404


async def test_countersigning_a_signed_visit_is_a_409(auth_client, second_auth_client):
    """A second signature would either overwrite the first attestation or record two, with
    nothing on the chart to say which one it means."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    await _share(auth_client, patient["id"], visit["id"], role="supervising")
    assert (
        await second_auth_client.post(f"/api/v1/encounters/shared/{visit['id']}/sign")
    ).status_code == 200
    second = await second_auth_client.post(f"/api/v1/encounters/shared/{visit['id']}/sign")
    assert second.status_code == 409


async def test_a_countersignature_writes_the_same_audit_action_as_the_owners_route(
    auth_client, second_auth_client, db
):
    """It is the same clinical act. Splitting it in two would mean an audit query for "who
    signed this" had to know which door it came through."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    await _share(auth_client, patient["id"], visit["id"], role="supervising")
    await second_auth_client.post(f"/api/v1/encounters/shared/{visit['id']}/sign")
    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "encounter_signed")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert str(rows[0].entity_id) == visit["id"]


# --- Audit --------------------------------------------------------------------------------


async def test_the_grant_and_the_withdrawal_are_both_audited_without_the_email_or_the_purpose(
    auth_client, second_auth_client, db
):
    """``audit_logs.payload`` is unencrypted and never pruned. The account id identifies the
    colleague for a review without writing an address into it; the purpose is free text a
    clinician typed about a patient."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    participant_id = (await _share(auth_client, patient["id"], visit["id"])).json()["id"]
    await auth_client.request(
        "DELETE",
        f"/api/v1/patients/{patient['id']}/encounters/{visit['id']}/participants/{participant_id}",
        json={"reason": "Opinion received."},
    )
    rows = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action.in_(
                        ("encounter_participant_added", "encounter_participant_removed")
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    assert {row.action for row in rows} == {
        "encounter_participant_added",
        "encounter_participant_removed",
    }
    for row in rows:
        assert str(row.patient_id) == patient["id"]
        serialized = str(row.payload).lower()
        assert SECOND_EMAIL not in serialized
        assert "cardiology" not in serialized
        assert "opinion received" not in serialized


async def test_a_participant_reading_a_shared_visit_is_audited_against_the_chart(
    auth_client, second_auth_client, db
):
    """ "Were they entitled to" and "did they actually look" are different questions."""
    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"])
    await _share(auth_client, patient["id"], visit["id"])
    await second_auth_client.get(f"/api/v1/encounters/shared/{visit['id']}")
    row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "shared_encounter_viewed")))
        .scalars()
        .one()
    )
    assert str(row.patient_id) == patient["id"]
    assert row.payload["role"] == "consulting"


async def test_an_anonymous_caller_reaches_neither_side(client):
    patient_id, encounter_id = uuid.uuid4(), uuid.uuid4()
    for method, path in (
        ("GET", "/api/v1/encounters/shared"),
        ("GET", f"/api/v1/encounters/shared/{encounter_id}"),
        ("POST", f"/api/v1/encounters/shared/{encounter_id}/sign"),
        (
            "GET",
            f"/api/v1/patients/{patient_id}/encounters/{encounter_id}/participants",
        ),
    ):
        resp = await client.request(method, path)
        assert resp.status_code == 401, path


async def test_the_shared_list_is_ordered_most_recently_granted_first(
    auth_client, second_auth_client, db
):
    patient = await create_patient(auth_client)
    first = await _visit(auth_client, patient["id"], presenting_complaint="First visit")
    second = await _visit(auth_client, patient["id"], presenting_complaint="Second visit")
    await _share(auth_client, patient["id"], first["id"])
    # SQLite's created_at has one-second resolution, so the second grant is nudged forward
    # rather than trusted to sort after the first by wall clock alone.
    await _share(auth_client, patient["id"], second["id"])
    rows = (
        (
            await db.execute(
                select(EncounterParticipant).where(
                    EncounterParticipant.encounter_id == uuid.UUID(second["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.created_at = datetime.now(UTC).replace(microsecond=0)
    await db.commit()

    listed = await second_auth_client.get("/api/v1/encounters/shared")
    assert [e["id"] for e in listed.json()["encounters"]][0] == second["id"]
