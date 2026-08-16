"""SBAR handover: the structure, the computed checklist, the freeze, and the receipt.

The four SBAR columns are the cheap half of this feature. Two things in it carry actual design
weight and get most of the tests:

* **The checklist is derived from the chart, never typed.** A free-form handover checklist is a
  list of what the outgoing clinician remembered, which is the faculty handover is failing.
  Zero-count items are omitted rather than shown as satisfied, because a list that always shows
  five rows — three of them permanently "nothing to do" — is one people learn to tick without
  reading, and then the two that mattered get ticked the same way.

* **The checklist is recomputed at send time and compared.** A handover drafted at 6pm and sent
  at 8pm may be describing a chart that has since acquired a panic potassium. A confirmation of
  a state that no longer holds is worse than no confirmation at all: it is a signed assertion
  that somebody reviewed something they never saw. Same judgement — and the same failure mode —
  as ``reasoning_chart_changed_under_run``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.exceptions import (
    HandoffChecklistStaleError,
    HandoffNotSentError,
    HandoffSentError,
)
from app.models.allergy import Allergy
from app.models.audit_log import AuditLog
from app.models.handoff import FROZEN_ON_SEND, PatientHandoff
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.handoff_service import CHECKLIST_ITEMS, HandoffService
from tests.conftest import create_patient

pytestmark = pytest.mark.asyncio

SBAR = {
    "situation": "68F, day 2 post-op, new onset AF with rate 130.",
    "background": "Hypertension, T2DM. Elective hemicolectomy 20/08.",
    "assessment": "Rate-related; no chest pain, haemodynamically stable.",
    "recommendation": "Repeat ECG at 06:00; escalate if rate stays above 120.",
}


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"handoff-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Handoff Patient",
        date_of_birth=datetime(1958, 3, 2).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _draft(db, account: Account, patient: Patient) -> PatientHandoff:
    return await HandoffService(db).create(account_id=account.id, patient_id=patient.id, **SBAR)


async def _send(db, account: Account, patient: Patient, handoff: PatientHandoff, **overrides):
    service = HandoffService(db)
    keys = overrides.pop(
        "confirmed_checklist_keys",
        [
            item.key
            for item in await service.build_checklist(account_id=account.id, patient_id=patient.id)
        ],
    )
    return await service.send(
        account_id=account.id,
        patient_id=patient.id,
        handoff_id=handoff.id,
        from_clinician=overrides.pop("from_clinician", "Dr Rao"),
        to_clinician=overrides.pop("to_clinician", "Dr Iyer"),
        confirmed_checklist_keys=keys,
    )


# --- The checklist ------------------------------------------------------------------------------


async def test_an_empty_chart_has_an_empty_checklist(db):
    """Nothing to hand over means no rows, not five rows saying "nothing to do"."""
    account, patient = await _account_and_patient(db)
    items = await HandoffService(db).build_checklist(account_id=account.id, patient_id=patient.id)
    assert items == []


async def test_a_documented_allergy_appears_on_the_checklist(db):
    account, patient = await _account_and_patient(db)
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name="Penicillin",
            allergen_type="drug",
            status="active",
            severity="life_threatening",
        )
    )
    await db.flush()

    items = await HandoffService(db).build_checklist(account_id=account.id, patient_id=patient.id)
    assert [i.key for i in items] == ["documented_allergies"]
    assert items[0].count == 1


async def test_an_unacknowledged_panic_value_appears_on_the_checklist(db):
    account, patient = await _account_and_patient(db)
    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name="Potassium",
            value_numeric=7.4,
            unit="mmol/L",
            sample_date=datetime.now(UTC),
        )
    )
    await db.flush()

    items = await HandoffService(db).build_checklist(account_id=account.id, patient_id=patient.id)
    assert "unacknowledged_critical_labs" in {i.key for i in items}


async def test_an_acknowledged_panic_value_leaves_the_checklist(db):
    """The two features compose: acknowledging the value is what makes it no longer outstanding."""
    from app.services.lab_safety_service import LabSafetyService

    account, patient = await _account_and_patient(db)
    lab = LabResult(
        patient_id=patient.id,
        marker_name="Potassium",
        value_numeric=7.4,
        unit="mmol/L",
        sample_date=datetime.now(UTC),
    )
    db.add(lab)
    await db.flush()
    await LabSafetyService(db).acknowledge(
        account_id=account.id,
        patient_id=patient.id,
        lab_result_id=lab.id,
        acknowledged_by="Dr Rao",
        action_note=None,
    )

    items = await HandoffService(db).build_checklist(account_id=account.id, patient_id=patient.id)
    assert "unacknowledged_critical_labs" not in {i.key for i in items}


async def test_another_patients_panic_value_is_not_on_this_chart_s_checklist(db):
    """The queue is panel-wide; the checklist is about one chart, and must filter it back down."""
    account, patient = await _account_and_patient(db)
    other = Patient(
        account_id=account.id,
        full_name="Someone Else",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(other)
    await db.flush()
    db.add(
        LabResult(
            patient_id=other.id,
            marker_name="Potassium",
            value_numeric=7.4,
            unit="mmol/L",
            sample_date=datetime.now(UTC),
        )
    )
    await db.flush()

    items = await HandoffService(db).build_checklist(account_id=account.id, patient_id=patient.id)
    assert items == []


async def test_current_medications_are_counted(db):
    from app.models.drug_vocabulary import DrugVocabulary

    account, patient = await _account_and_patient(db)
    vocab = (
        (
            await db.execute(
                select(DrugVocabulary).where(DrugVocabulary.generic_name.ilike("Metformin"))
            )
        )
        .scalars()
        .first()
    )
    assert vocab is not None
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            drug_vocabulary_id=vocab.id,
            generic_name=vocab.generic_name,
            event_type="continue",
            is_current=True,
        )
    )
    await db.flush()

    items = await HandoffService(db).build_checklist(account_id=account.id, patient_id=patient.id)
    assert {i.key: i.count for i in items}["current_medications"] == 1


async def test_every_checklist_key_carries_a_description(db):
    """A key with no phrasing renders as a bare identifier on a clinician's screen."""
    assert all(text.strip() for text in CHECKLIST_ITEMS.values())


async def test_checklist_descriptions_are_statements_not_instructions(db):
    """Rule #4: prescriber-framed. A checklist that issues orders gets ignored wholesale."""
    for text in CHECKLIST_ITEMS.values():
        lowered = text.lower()
        for imperative in ("review the", "check the", "you must", "ensure that"):
            assert not lowered.startswith(imperative), text


# --- Draft, send, freeze ------------------------------------------------------------------------


async def test_a_draft_is_editable(db):
    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)
    updated = await HandoffService(db).update(
        account_id=account.id,
        patient_id=patient.id,
        handoff_id=handoff.id,
        assessment="Reassessed: rate now 95 after metoprolol.",
    )
    assert updated.assessment.startswith("Reassessed")
    assert updated.status == "draft"


async def test_sending_freezes_the_content(db):
    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)
    await _send(db, account, patient, handoff)

    with pytest.raises(HandoffSentError):
        await HandoffService(db).update(
            account_id=account.id,
            patient_id=patient.id,
            handoff_id=handoff.id,
            assessment="Rewritten after the fact.",
        )


async def test_the_frozen_column_list_covers_everything_the_sender_asserted(db):
    """A clinical column added without deciding whether sending covers it is a silent hole.

    The migration builds its trigger from this same tuple, so an omission here is an omission in
    the database's own guard rather than only in the service's.
    """
    for column in (
        "situation",
        "background",
        "assessment",
        "recommendation",
        "from_clinician",
        "to_clinician",
        "checklist",
        "sent_at",
        "patient_id",
    ):
        assert column in FROZEN_ON_SEND, column
    # And the things that must stay writable after the freeze, or the acknowledgement and the
    # chart withdrawal both break.
    for column in (
        "status",
        "acknowledged_at",
        "acknowledged_by",
        "acknowledgement_note",
        "is_deleted",
        "updated_at",
    ):
        assert column not in FROZEN_ON_SEND, column


async def test_a_sent_handoff_cannot_be_sent_again(db):
    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)
    await _send(db, account, patient, handoff)
    with pytest.raises(HandoffSentError):
        await _send(db, account, patient, handoff)


async def test_sending_records_both_clinicians_and_snapshots_the_checklist(db):
    account, patient = await _account_and_patient(db)
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name="Penicillin",
            allergen_type="drug",
            status="active",
        )
    )
    await db.flush()
    handoff = await _draft(db, account, patient)
    sent = await _send(db, account, patient, handoff)

    assert sent.status == "sent"
    assert sent.from_clinician == "Dr Rao"
    assert sent.to_clinician == "Dr Iyer"
    assert sent.sent_at is not None
    assert [item["key"] for item in sent.checklist] == ["documented_allergies"]
    assert sent.checklist[0]["count"] == 1
    assert sent.checklist[0]["description"] == CHECKLIST_ITEMS["documented_allergies"]


# --- The stale-checklist refusal ----------------------------------------------------------------


async def test_a_risk_that_appeared_after_the_draft_refuses_the_send(db):
    """The heart of the feature.

    The clinician confirmed an empty checklist; by send time the chart has a panic potassium
    they never saw. Accepting the send would put a signed assertion on the record that the
    outgoing clinician reviewed the chart's outstanding risks.
    """
    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)
    confirmed_when_empty: list[str] = []

    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name="Potassium",
            value_numeric=7.4,
            unit="mmol/L",
            sample_date=datetime.now(UTC),
        )
    )
    await db.flush()

    with pytest.raises(HandoffChecklistStaleError):
        await _send(db, account, patient, handoff, confirmed_checklist_keys=confirmed_when_empty)


async def test_a_risk_that_went_away_also_refuses_the_send(db):
    """Both directions, not just "something new appeared".

    An item that has *gone away* — a critical value acknowledged by someone else in the meantime
    — means the clinician confirmed a picture that is no longer current, just as much as a new
    item does. Accepting the superset would wave a stale confirmation through whenever the chart
    happened to improve, which is precisely when nobody is looking.
    """
    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)

    with pytest.raises(HandoffChecklistStaleError):
        await _send(
            db,
            account,
            patient,
            handoff,
            confirmed_checklist_keys=["documented_allergies"],
        )


async def test_re_reading_the_checklist_lets_the_send_through(db):
    """The refusal is recoverable: read it again, confirm what is actually there, send."""
    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)
    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name="Potassium",
            value_numeric=7.4,
            unit="mmol/L",
            sample_date=datetime.now(UTC),
        )
    )
    await db.flush()

    with pytest.raises(HandoffChecklistStaleError):
        await _send(db, account, patient, handoff, confirmed_checklist_keys=[])

    sent = await _send(db, account, patient, handoff)
    assert sent.status == "sent"
    assert [i["key"] for i in sent.checklist] == ["unacknowledged_critical_labs"]


# --- Acknowledgement ----------------------------------------------------------------------------


async def test_acknowledging_closes_the_loop(db):
    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)
    await _send(db, account, patient, handoff)

    acked = await HandoffService(db).acknowledge(
        account_id=account.id,
        patient_id=patient.id,
        handoff_id=handoff.id,
        acknowledged_by="Dr Iyer",
        note="Seen and accepted.",
    )
    assert acked.status == "acknowledged"
    assert acked.acknowledged_by == "Dr Iyer"
    assert acked.acknowledged_at is not None
    assert acked.acknowledged_by_account_id == account.id


async def test_a_draft_cannot_be_acknowledged(db):
    """A receipt for something still being written, which would then freeze it mid-sentence."""
    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)
    with pytest.raises(HandoffNotSentError):
        await HandoffService(db).acknowledge(
            account_id=account.id,
            patient_id=patient.id,
            handoff_id=handoff.id,
            acknowledged_by="Dr Iyer",
            note=None,
        )


async def test_a_handoff_cannot_be_acknowledged_twice(db):
    """One named person to one named person; a second receipt loses who took the patient on.

    Deliberately different from a critical-lab acknowledgement, which appends: a lab value can
    legitimately be seen by several clinicians and each attestation is worth keeping.
    """
    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)
    await _send(db, account, patient, handoff)
    service = HandoffService(db)
    await service.acknowledge(
        account_id=account.id,
        patient_id=patient.id,
        handoff_id=handoff.id,
        acknowledged_by="Dr Iyer",
        note=None,
    )
    with pytest.raises(HandoffSentError):
        await service.acknowledge(
            account_id=account.id,
            patient_id=patient.id,
            handoff_id=handoff.id,
            acknowledged_by="Dr Third",
            note=None,
        )


async def test_the_acknowledgement_records_how_long_the_patient_was_unreceived(db):
    """The interval is the number a post-incident review asks for, and nothing else holds it."""
    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)
    await _send(db, account, patient, handoff)
    await HandoffService(db).acknowledge(
        account_id=account.id,
        patient_id=patient.id,
        handoff_id=handoff.id,
        acknowledged_by="Dr Iyer",
        note=None,
    )
    await db.flush()

    row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "handoff_acknowledged")))
        .scalars()
        .one()
    )
    assert row.payload["seconds_since_sent"] >= 0
    assert row.payload["acknowledged_by"] == "Dr Iyer"


async def test_the_audit_trail_holds_the_clinicians_but_not_the_sbar_prose(db):
    """Names of *clinicians* are the point of the entry; the patient's story is not.

    The trail is unencrypted, append-only and never pruned, and SBAR prose is exactly the kind
    of text that carries a patient's name in it.
    """
    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)
    await _send(db, account, patient, handoff)
    await db.flush()

    row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "handoff_sent")))
        .scalars()
        .one()
    )
    assert row.payload["from_clinician"] == "Dr Rao"
    assert row.payload["sbar_chars"] == sum(len(v) for v in SBAR.values())
    rendered = str(row.payload)
    assert "hemicolectomy" not in rendered
    assert "68F" not in rendered


# --- Tenancy ------------------------------------------------------------------------------------


async def test_another_account_cannot_reach_the_handoff(db):
    from app.exceptions import NotFoundError

    account, patient = await _account_and_patient(db)
    handoff = await _draft(db, account, patient)
    intruder = Account(email=f"other-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(intruder)
    await db.flush()

    with pytest.raises(NotFoundError):
        await HandoffService(db).update(
            account_id=intruder.id,
            patient_id=patient.id,
            handoff_id=handoff.id,
            assessment="Not mine to write.",
        )


async def test_a_handoff_from_a_different_chart_is_not_reachable_by_id(db):
    """The path names a patient and a handoff; they have to be the same chart."""
    from app.exceptions import NotFoundError

    account, patient = await _account_and_patient(db)
    other = Patient(
        account_id=account.id,
        full_name="Other Chart",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(other)
    await db.flush()
    handoff = await _draft(db, account, patient)

    with pytest.raises(NotFoundError):
        await HandoffService(db).get(other.id, handoff.id)


# --- The HTTP surface ---------------------------------------------------------------------------


async def test_the_full_handover_journey_over_http(auth_client):
    patient = await create_patient(auth_client)
    pid = patient["id"]

    created = await auth_client.post(f"/api/v1/patients/{pid}/handoffs", json=SBAR)
    assert created.status_code == 201, created.text
    handoff_id = created.json()["id"]
    assert created.json()["status"] == "draft"

    checklist = await auth_client.get(f"/api/v1/patients/{pid}/handoffs/checklist")
    assert checklist.status_code == 200, checklist.text
    keys = [item["key"] for item in checklist.json()["items"]]

    sent = await auth_client.post(
        f"/api/v1/patients/{pid}/handoffs/{handoff_id}/send",
        json={
            "from_clinician": "Dr Rao",
            "to_clinician": "Dr Iyer",
            "confirmed_checklist_keys": keys,
        },
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "sent"

    acked = await auth_client.post(
        f"/api/v1/patients/{pid}/handoffs/{handoff_id}/acknowledge",
        json={"acknowledged_by": "Dr Iyer", "note": "Accepted."},
    )
    assert acked.status_code == 200, acked.text
    assert acked.json()["status"] == "acknowledged"
    assert acked.json()["acknowledged_by"] == "Dr Iyer"

    listed = await auth_client.get(f"/api/v1/patients/{pid}/handoffs")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


@pytest.mark.parametrize("missing", ["situation", "background", "assessment", "recommendation"])
async def test_every_sbar_field_is_required(auth_client, missing):
    """The structure's whole purpose. Verbal handover drops Assessment and Recommendation, so a
    schema that lets either be omitted lets them be forgotten in the same way."""
    patient = await create_patient(auth_client)
    payload = {k: v for k, v in SBAR.items() if k != missing}
    resp = await auth_client.post(f"/api/v1/patients/{patient['id']}/handoffs", json=payload)
    assert resp.status_code == 422


async def test_a_whitespace_only_sbar_field_is_refused(auth_client):
    """``min_length`` counts raw characters; the floor has to be applied to the stripped text."""
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/handoffs", json={**SBAR, "assessment": " " * 40}
    )
    assert resp.status_code == 422


async def test_editing_a_sent_handoff_is_a_409_over_http(auth_client):
    patient = await create_patient(auth_client)
    pid = patient["id"]
    handoff_id = (await auth_client.post(f"/api/v1/patients/{pid}/handoffs", json=SBAR)).json()[
        "id"
    ]
    await auth_client.post(
        f"/api/v1/patients/{pid}/handoffs/{handoff_id}/send",
        json={
            "from_clinician": "Dr Rao",
            "to_clinician": "Dr Iyer",
            "confirmed_checklist_keys": [],
        },
    )
    resp = await auth_client.patch(
        f"/api/v1/patients/{pid}/handoffs/{handoff_id}",
        json={"assessment": "Rewritten after the fact, which must not be possible."},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "handoff_sent"


async def test_a_stale_checklist_is_a_409_over_http(auth_client):
    patient = await create_patient(auth_client)
    pid = patient["id"]
    handoff_id = (await auth_client.post(f"/api/v1/patients/{pid}/handoffs", json=SBAR)).json()[
        "id"
    ]
    resp = await auth_client.post(
        f"/api/v1/patients/{pid}/handoffs/{handoff_id}/send",
        json={
            "from_clinician": "Dr Rao",
            "to_clinician": "Dr Iyer",
            "confirmed_checklist_keys": ["documented_allergies"],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "handoff_checklist_stale"


async def test_repeating_a_confirmed_key_is_not_a_mismatch(auth_client):
    """A client that sends one key twice has confirmed it; failing them has nothing behind it."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    # Give the chart exactly one checklist item, then confirm its key twice.
    await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/check", json={"drug_name": "Metformin"}
    )
    handoff_id = (await auth_client.post(f"/api/v1/patients/{pid}/handoffs", json=SBAR)).json()[
        "id"
    ]
    keys = [
        item["key"]
        for item in (await auth_client.get(f"/api/v1/patients/{pid}/handoffs/checklist")).json()[
            "items"
        ]
    ]
    resp = await auth_client.post(
        f"/api/v1/patients/{pid}/handoffs/{handoff_id}/send",
        json={
            "from_clinician": "Dr Rao",
            "to_clinician": "Dr Iyer",
            "confirmed_checklist_keys": keys + keys,
        },
    )
    assert resp.status_code == 200, resp.text


async def test_another_account_gets_404_on_every_handoff_route(auth_client, second_auth_client):
    patient = await create_patient(auth_client)
    pid = patient["id"]
    handoff_id = (await auth_client.post(f"/api/v1/patients/{pid}/handoffs", json=SBAR)).json()[
        "id"
    ]

    for method, path, payload in (
        ("get", f"/api/v1/patients/{pid}/handoffs", None),
        ("get", f"/api/v1/patients/{pid}/handoffs/checklist", None),
        ("post", f"/api/v1/patients/{pid}/handoffs", SBAR),
        ("patch", f"/api/v1/patients/{pid}/handoffs/{handoff_id}", {"assessment": "x" * 20}),
    ):
        call = getattr(second_auth_client, method)
        resp = await (call(path, json=payload) if payload is not None else call(path))
        assert resp.status_code == 404, path
