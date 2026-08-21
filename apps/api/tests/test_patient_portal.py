"""The patient portal: credentials, redaction, and the results embargo.

The clinically load-bearing test is
``test_a_critical_value_is_withheld_until_a_clinician_has_acknowledged_it``. A portal that shows
every result the moment it is ingested tells a patient about a life-threatening value alone, at
3am, before anyone clinical has seen it.

The second is ``test_the_two_credential_vocabularies_never_resolve_each_other``. A patient's
token reaching a clinician's route would be a whole-panel disclosure.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.portal_redaction import (
    PortalLabInput,
    PortalMedicationInput,
    decide_lab_release,
    grant_is_live,
    present_medication,
)
from app.models.audit_log import AuditLog
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.patient_portal_grant import PatientPortalGrant
from app.services.patient_portal_service import MAX_GRANT_DAYS, hash_portal_token
from tests.conftest import create_patient


def _lab_input(**overrides) -> PortalLabInput:
    payload = {
        "lab_result_id": "lab-1",
        "marker_name": "Potassium",
        "value_numeric": 4.2,
        "value_text": None,
        "unit": "mmol/L",
        "reference_low": 3.5,
        "reference_high": 5.1,
        "sample_date": date(2026, 1, 5),
        "is_abnormal": False,
        "acknowledged": False,
    }
    payload.update(overrides)
    return PortalLabInput(**payload)


# --- The release decision, as a pure function ---------------------------------------------


def test_an_ordinary_result_is_released_with_its_range():
    decision = decide_lab_release(_lab_input())
    assert decision.released is True
    assert decision.value_numeric == 4.2
    assert (decision.reference_low, decision.reference_high) == (3.5, 5.1)
    assert decision.withheld_reason is None


def test_a_critical_value_is_withheld_until_a_clinician_has_acknowledged_it():
    """The embargo. A potassium of 6.9 needs a phone call, not a web page."""
    withheld = decide_lab_release(_lab_input(value_numeric=6.9, is_abnormal=True))
    assert withheld.released is False
    assert withheld.withheld_reason == "awaiting_clinician_review"
    assert withheld.withheld_summary is not None

    released = decide_lab_release(
        _lab_input(value_numeric=6.9, is_abnormal=True, acknowledged=True)
    )
    assert released.released is True
    assert released.value_numeric == 6.9


def test_a_withheld_decision_carries_no_value_at_all():
    """Not filtered at serialization — absent from the decision object, so there is nothing for
    a later response model to accidentally include."""
    withheld = decide_lab_release(_lab_input(value_numeric=6.9))
    assert withheld.value_numeric is None
    assert withheld.value_text is None
    assert withheld.unit is None
    assert withheld.reference_low is None and withheld.reference_high is None
    assert withheld.is_abnormal is None
    # The marker and the date survive: the patient is told a result exists and is being looked
    # at, which is what prompts the phone call.
    assert withheld.marker_name == "Potassium"
    assert withheld.sample_date == date(2026, 1, 5)


def test_a_result_the_screen_cannot_read_is_released_not_embargoed():
    """The asymmetry with the clinician's queue, and it is deliberate: withholding every result
    the screen cannot place would embargo most of a record on the strength of a unit string."""
    unreadable_unit = decide_lab_release(
        _lab_input(marker_name="Potassium", value_numeric=6.9, unit="furlongs")
    )
    uncurated_marker = decide_lab_release(
        _lab_input(marker_name="Anti-Widget Antibody", value_numeric=9999.0, unit="u")
    )
    assert unreadable_unit.released is True
    assert uncurated_marker.released is True


def test_a_qualitative_result_has_nothing_to_screen_and_is_released():
    decision = decide_lab_release(
        _lab_input(value_numeric=None, value_text="Reactive", marker_name="HBsAg")
    )
    assert decision.released is True
    assert decision.value_text == "Reactive"


def test_a_medicine_is_named_the_way_the_patient_was_handed_it():
    """A patient handed a strip labelled "Crocin" and shown "Paracetamol" has been shown a
    different medicine as far as they can tell, and the outcome of that is a double dose."""
    shown = present_medication(
        PortalMedicationInput(
            medication_event_id="m1",
            display_name="Crocin 650",
            generic_name="Paracetamol",
            dose="650 mg",
            frequency="TDS",
            route="oral",
            status="start",
            started_on=date(2026, 1, 1),
            stopped_on=None,
        )
    )
    assert shown.name == "Crocin 650"


def test_a_medicine_with_no_brand_falls_back_to_the_generic_then_to_a_placeholder():
    generic_only = present_medication(
        PortalMedicationInput(
            medication_event_id="m1",
            display_name=None,
            generic_name="Metformin",
            dose=None,
            frequency=None,
            route=None,
            status="start",
            started_on=None,
            stopped_on=None,
        )
    )
    unnamed = present_medication(
        PortalMedicationInput(
            medication_event_id="m2",
            display_name=None,
            generic_name=None,
            dose=None,
            frequency=None,
            route=None,
            status="start",
            started_on=None,
            stopped_on=None,
        )
    )
    assert generic_only.name == "Metformin"
    assert unnamed.name == "Unnamed medicine"


@pytest.mark.parametrize(
    ("expires_delta", "revoked", "expected"),
    [
        (timedelta(days=1), False, True),
        (timedelta(days=-1), False, False),
        (timedelta(days=1), True, False),
        (timedelta(days=-1), True, False),
    ],
)
def test_a_grant_is_live_only_when_neither_expired_nor_revoked(expires_delta, revoked, expected):
    """Both conditions, and neither implies the other."""
    now = datetime(2026, 8, 21, tzinfo=UTC)
    assert (
        grant_is_live(
            expires_at=now + expires_delta,
            revoked_at=now if revoked else None,
            now=now,
        )
        is expected
    )


# --- Issuing and withdrawing --------------------------------------------------------------


async def _issue(client, patient_id: str, **overrides):
    return await client.post(f"/api/v1/patients/{patient_id}/portal-access", json={**overrides})


@pytest_asyncio.fixture
async def portal_client(app):
    """A client with no clinician credentials, for the patient's side of the API.

    Deliberately its own ``AsyncClient`` rather than a header swap on ``client``: ``auth_client``
    is built *from* the ``client`` fixture and is the same object, so setting a portal token on
    it silently signs the clinician out for the rest of the test — which reads as the portal
    rejecting a valid credential.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _portal(portal_client, token: str):
    portal_client.headers["Authorization"] = f"Bearer {token}"
    return portal_client


async def test_issuing_returns_the_token_once_and_stores_only_its_hash(auth_client, db):
    """A database dump must not be a set of working credentials to living patients' records."""
    patient = await create_patient(auth_client)
    resp = await _issue(auth_client, patient["id"], label="Printed at the desk")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    token = body["token"]
    assert token and len(token) > 20
    assert body["is_live"] is True
    assert body["last_used_at"] is None

    row = await db.get(PatientPortalGrant, uuid.UUID(body["id"]))
    assert row is not None
    assert row.token_hash == hash_portal_token(token)
    assert token not in row.token_hash

    # And the token is never retrievable afterwards.
    listed = await auth_client.get(f"/api/v1/patients/{patient['id']}/portal-access")
    assert listed.status_code == 200
    assert "token" not in listed.json()["grants"][0]
    assert token not in listed.text


async def test_a_grant_longer_than_the_ceiling_is_refused(auth_client):
    patient = await create_patient(auth_client)
    resp = await _issue(auth_client, patient["id"], days_valid=MAX_GRANT_DAYS + 1)
    assert resp.status_code == 422


async def test_only_the_chart_owner_may_issue_a_credential(auth_client, second_auth_client):
    patient = await create_patient(auth_client)
    resp = await _issue(second_auth_client, patient["id"])
    assert resp.status_code == 404


async def test_a_chart_whose_patient_withdrew_consent_cannot_be_opened_to_a_portal(auth_client, db):
    """Issuing portal access against a withdrawal would be the clearest possible violation."""
    patient = await create_patient(auth_client)
    row = await db.get(Patient, uuid.UUID(patient["id"]))
    assert row is not None
    row.consent_given = False
    row.consent_withdrawn_at = datetime.now(UTC)
    await db.commit()
    resp = await _issue(auth_client, patient["id"])
    assert resp.status_code >= 400
    assert resp.status_code != 201


async def test_withdrawing_a_credential_stops_it_from_the_next_request(auth_client, portal_client):
    patient = await create_patient(auth_client)
    issued = (await _issue(auth_client, patient["id"])).json()
    assert (
        await _portal(portal_client, issued["token"]).get("/api/v1/portal/me")
    ).status_code == 200

    revoked = await auth_client.request(
        "DELETE",
        f"/api/v1/patients/{patient['id']}/portal-access/{issued['id']}",
        json={"reason": "Patient reported a lost phone."},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["is_live"] is False
    assert (
        await _portal(portal_client, issued["token"]).get("/api/v1/portal/me")
    ).status_code == 401


async def test_withdrawing_an_already_withdrawn_credential_is_a_404(auth_client):
    """A patient ringing about a lost phone deserves "I have just stopped this" to be true."""
    patient = await create_patient(auth_client)
    issued = (await _issue(auth_client, patient["id"])).json()
    path = f"/api/v1/patients/{patient['id']}/portal-access/{issued['id']}"
    body = {"reason": "Lost phone."}
    assert (await auth_client.request("DELETE", path, json=body)).status_code == 200
    second = await auth_client.request("DELETE", path, json=body)
    assert second.status_code == 404
    assert second.json()["code"] == "portal_grant_not_found"


async def test_the_grant_list_includes_withdrawn_credentials(auth_client):
    """ "Who has had access to this record, and between when" is the question."""
    patient = await create_patient(auth_client)
    issued = (await _issue(auth_client, patient["id"])).json()
    await auth_client.request(
        "DELETE",
        f"/api/v1/patients/{patient['id']}/portal-access/{issued['id']}",
        json={"reason": "Episode of care ended."},
    )
    listed = (await auth_client.get(f"/api/v1/patients/{patient['id']}/portal-access")).json()
    assert len(listed["grants"]) == 1
    assert listed["grants"][0]["is_live"] is False
    assert listed["grants"][0]["revocation_reason"] == "Episode of care ended."


# --- Authentication -----------------------------------------------------------------------


async def test_every_way_of_failing_gives_the_same_401(auth_client, portal_client, db):
    """Somebody who found a link on a shared phone must not learn what kind of thing they
    found: expired, withdrawn, for a deleted chart, or never a token at all."""
    patient = await create_patient(auth_client)
    expired = (await _issue(auth_client, patient["id"], days_valid=1)).json()
    row = await db.get(PatientPortalGrant, uuid.UUID(expired["id"]))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.commit()

    revoked = (await _issue(auth_client, patient["id"])).json()
    await auth_client.request(
        "DELETE",
        f"/api/v1/patients/{patient['id']}/portal-access/{revoked['id']}",
        json={"reason": "Withdrawn."},
    )

    bodies = []
    for token in (expired["token"], revoked["token"], "not-a-token-at-all", ""):
        resp = await _portal(portal_client, token).get("/api/v1/portal/me")
        assert resp.status_code == 401, token
        bodies.append(resp.json())
    assert len({(b["code"], b["message"]) for b in bodies}) == 1


async def test_a_withdrawn_chart_closes_the_portal(auth_client, portal_client):
    """A credential is not a copy that outlives the record it points at."""
    patient = await create_patient(auth_client)
    issued = (await _issue(auth_client, patient["id"])).json()
    assert (
        await _portal(portal_client, issued["token"]).get("/api/v1/portal/me")
    ).status_code == 200
    deleted = await auth_client.delete(f"/api/v1/patients/{patient['id']}")
    assert deleted.status_code in (200, 204), deleted.text
    assert (
        await _portal(portal_client, issued["token"]).get("/api/v1/portal/me")
    ).status_code == 401


async def test_the_two_credential_vocabularies_never_resolve_each_other(auth_client, portal_client):
    """A patient's token on a clinician's route would be a whole-panel disclosure, and a
    clinician's token on a portal route would attach their session to an arbitrary chart.

    Neither is prevented by a check that could be edited wrong: a portal token is an opaque
    random string that ``decode_token`` cannot parse, and a JWT is not a row in
    ``patient_portal_grants``.
    """
    patient = await create_patient(auth_client)
    portal_token = (await _issue(auth_client, patient["id"])).json()["token"]
    clinician_token = auth_client.headers["Authorization"].split(" ", 1)[1]

    # Patient credential -> clinician routes.
    for path in ("/api/v1/patients", f"/api/v1/patients/{patient['id']}", "/api/v1/auth/me"):
        resp = await _portal(portal_client, portal_token).get(path)
        assert resp.status_code == 401, path

    # Clinician credential -> portal routes.
    for path in ("/api/v1/portal/me", "/api/v1/portal/labs", "/api/v1/portal/medications"):
        resp = await _portal(portal_client, clinician_token).get(path)
        assert resp.status_code == 401, path


async def test_a_credential_opens_exactly_one_chart(auth_client, portal_client):
    """There is no patient id in any portal path, so there is nothing to substitute."""
    first = await create_patient(auth_client, full_name="Ramesh Kumar")
    second = await create_patient(auth_client, full_name="Asha Devi")
    token = (await _issue(auth_client, first["id"])).json()["token"]
    body = (await _portal(portal_client, token).get("/api/v1/portal/me")).json()
    assert body["full_name"] == "Ramesh Kumar"
    assert "Asha" not in str(body)
    # And the second patient's own credential opens only theirs.
    other_token = (await _issue(auth_client, second["id"])).json()["token"]
    other = (await _portal(portal_client, other_token).get("/api/v1/portal/me")).json()
    assert other["full_name"] == "Asha Devi"


async def test_using_a_credential_stamps_last_used_at(auth_client, portal_client, db):
    """A link issued three months ago and never opened is a piece of paper somebody lost."""
    patient = await create_patient(auth_client)
    issued = (await _issue(auth_client, patient["id"])).json()
    await _portal(portal_client, issued["token"]).get("/api/v1/portal/me")
    listed = (await auth_client.get(f"/api/v1/patients/{patient['id']}/portal-access")).json()
    assert listed["grants"][0]["last_used_at"] is not None


# --- The patient's reads ------------------------------------------------------------------


async def _seed_labs(db, patient_id: str, rows: list[dict]) -> list[LabResult]:
    created = []
    for row in rows:
        lab = LabResult(patient_id=uuid.UUID(patient_id), **row)
        db.add(lab)
        created.append(lab)
    await db.commit()
    for lab in created:
        await db.refresh(lab)
    return created


async def test_the_labs_route_releases_ordinary_results_with_their_ranges(
    auth_client, portal_client, db
):
    patient = await create_patient(auth_client)
    await _seed_labs(
        db,
        patient["id"],
        [
            {
                "marker_name": "Potassium",
                "value_numeric": 4.2,
                "unit": "mmol/L",
                "reference_range_low": 3.5,
                "reference_range_high": 5.1,
                "sample_date": datetime(2026, 1, 5, tzinfo=UTC),
                "is_abnormal": False,
            }
        ],
    )
    token = (await _issue(auth_client, patient["id"])).json()["token"]
    body = (await _portal(portal_client, token).get("/api/v1/portal/labs")).json()
    assert body["withheld_count"] == 0
    assert body["results"][0]["released"] is True
    assert body["results"][0]["value_numeric"] == 4.2
    assert body["results"][0]["reference_high"] == 5.1


async def test_the_labs_route_embargoes_a_panic_value_end_to_end(auth_client, portal_client, db):
    """The clinical rule, through the database and the response model."""
    patient = await create_patient(auth_client)
    await _seed_labs(
        db,
        patient["id"],
        [
            {
                "marker_name": "Potassium",
                "value_numeric": 6.9,
                "unit": "mmol/L",
                "sample_date": datetime(2026, 1, 5, tzinfo=UTC),
                "is_abnormal": True,
            }
        ],
    )
    token = (await _issue(auth_client, patient["id"])).json()["token"]
    resp = await _portal(portal_client, token).get("/api/v1/portal/labs")
    body = resp.json()
    assert body["withheld_count"] == 1
    entry = body["results"][0]
    assert entry["released"] is False
    assert entry["value_numeric"] is None
    # The number itself is nowhere in the payload.
    assert "6.9" not in resp.text
    # But its existence is, which is what prompts the phone call.
    assert entry["marker_name"] == "Potassium"
    assert entry["withheld_reason"] == "awaiting_clinician_review"


async def test_a_clinicians_acknowledgement_releases_the_result(auth_client, portal_client, db):
    """Nothing else releases it: not time passing, not the chart having been opened."""
    patient = await create_patient(auth_client)
    labs = await _seed_labs(
        db,
        patient["id"],
        [
            {
                "marker_name": "Potassium",
                "value_numeric": 6.9,
                "unit": "mmol/L",
                "sample_date": datetime(2026, 1, 5, tzinfo=UTC),
                "is_abnormal": True,
            }
        ],
    )
    token = (await _issue(auth_client, patient["id"])).json()["token"]
    assert (await _portal(portal_client, token).get("/api/v1/portal/labs")).json()[
        "withheld_count"
    ] == 1

    acked = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/labs/critical-flags/{labs[0].id}/acknowledge",
        json={"acknowledged_by": "Dr Kumar"},
    )
    assert acked.status_code in (200, 201), acked.text

    body = (await _portal(portal_client, token).get("/api/v1/portal/labs")).json()
    assert body["withheld_count"] == 0
    assert body["results"][0]["value_numeric"] == 6.9


async def test_a_withdrawn_result_is_not_shown_to_the_patient(auth_client, portal_client, db):
    patient = await create_patient(auth_client)
    await _seed_labs(
        db,
        patient["id"],
        [
            {
                "marker_name": "Potassium",
                "value_numeric": 4.2,
                "unit": "mmol/L",
                "sample_date": datetime(2026, 1, 5, tzinfo=UTC),
                "is_deleted": True,
            }
        ],
    )
    token = (await _issue(auth_client, patient["id"])).json()["token"]
    assert (await _portal(portal_client, token).get("/api/v1/portal/labs")).json()["results"] == []


async def test_the_medications_route_shows_current_medicines_by_their_brand(
    auth_client, portal_client, db
):
    patient = await create_patient(auth_client)
    db.add(
        MedicationEvent(
            patient_id=uuid.UUID(patient["id"]),
            brand_name_raw="Glycomet 500",
            generic_name="Metformin",
            dose="500",
            dose_unit="mg",
            frequency="BD",
            route="oral",
            event_type="start",
            event_date=date(2026, 1, 5),
            is_current=True,
        )
    )
    db.add(
        MedicationEvent(
            patient_id=uuid.UUID(patient["id"]),
            brand_name_raw="Old Drug",
            generic_name="Something",
            event_type="stop",
            event_date=date(2020, 1, 5),
            is_current=False,
        )
    )
    await db.commit()
    token = (await _issue(auth_client, patient["id"])).json()["token"]
    body = (await _portal(portal_client, token).get("/api/v1/portal/medications")).json()
    assert [m["name"] for m in body["medications"]] == ["Glycomet 500"]
    assert body["medications"][0]["dose"] == "500 mg"


async def test_the_appointments_route_shows_only_upcoming_scheduled_visits(
    auth_client, portal_client
):
    """A patient who did not attend a follow-up is a clinical fact the practice keeps. It reads
    very differently on the patient's own phone."""
    patient = await create_patient(auth_client)
    soon = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    booked = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments",
        json={
            "provider_name": "Dr Kumar",
            "starts_at": soon.isoformat(),
            "ends_at": (soon + timedelta(minutes=15)).isoformat(),
            "reason": "Diabetes review",
        },
    )
    assert booked.status_code == 201, booked.text
    cancelled = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments",
        json={
            "provider_name": "Dr Kumar",
            "starts_at": (soon + timedelta(days=1)).isoformat(),
            "ends_at": (soon + timedelta(days=1, minutes=15)).isoformat(),
        },
    )
    assert cancelled.status_code == 201, cancelled.text
    await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments/{cancelled.json()['id']}/cancel",
        json={"reason": "Patient rebooked."},
    )

    token = (await _issue(auth_client, patient["id"])).json()["token"]
    body = (await _portal(portal_client, token).get("/api/v1/portal/appointments")).json()
    assert [a["id"] for a in body["appointments"]] == [booked.json()["id"]]


async def test_no_portal_response_can_carry_a_note_a_diagnosis_or_a_reasoning_output():
    """Absent from the shape, not filtered at the last moment — so a column added to any of
    those tables later has nowhere to arrive."""
    from app.schemas.patient_portal import (
        PortalAppointmentResponse,
        PortalLabResponse,
        PortalMedicationResponse,
        PortalPatientResponse,
    )

    forbidden = {
        "clinician_notes",
        "presenting_complaint",
        "conditions",
        "condition",
        "diagnosis",
        "differential",
        "suggestions",
        "reasoning",
        "safety_flags",
        "interactions",
        "allergies",
        "notes",
    }
    for model in (
        PortalPatientResponse,
        PortalLabResponse,
        PortalMedicationResponse,
        PortalAppointmentResponse,
    ):
        assert set(model.model_fields) & forbidden == set(), model.__name__


# --- Audit --------------------------------------------------------------------------------


async def test_a_portal_read_is_audited_as_the_patients_and_not_the_practices(
    auth_client, portal_client, db
):
    """Without ``by_patient`` the trail would say the practice read the record at 3am."""
    patient = await create_patient(auth_client)
    token = (await _issue(auth_client, patient["id"])).json()["token"]
    await _portal(portal_client, token).get("/api/v1/portal/labs")
    row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "portal_record_viewed")))
        .scalars()
        .one()
    )
    assert str(row.patient_id) == patient["id"]
    assert row.payload["by_patient"] is True
    assert row.payload["section"] == "labs"
    # Attributed to the issuing account, because the patient has no account id and an entry
    # with no actor is one a review cannot follow.
    assert row.account_id is not None


async def test_the_grant_audit_entry_carries_neither_the_token_nor_the_label(auth_client, db):
    patient = await create_patient(auth_client)
    issued = (await _issue(auth_client, patient["id"], label="Printed for Mrs Kumar")).json()
    row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "portal_access_granted")))
        .scalars()
        .one()
    )
    serialized = str(row.payload)
    assert issued["token"] not in serialized
    assert hash_portal_token(issued["token"]) not in serialized
    assert "Mrs Kumar" not in serialized
    assert row.payload["labelled"] is True


async def test_an_anonymous_caller_reaches_no_portal_route(client):
    for path in (
        "/api/v1/portal/me",
        "/api/v1/portal/labs",
        "/api/v1/portal/medications",
        "/api/v1/portal/appointments",
    ):
        resp = await client.get(path)
        assert resp.status_code == 401, path
