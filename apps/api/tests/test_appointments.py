"""Booking through the API: conflicts, availability notes, the lifecycle, and the reminder queue.

The arithmetic is tested in ``test_core_scheduling``. What is here is everything that needs a
database or a request: that a clash is actually refused end to end, that the refusal is scoped
to one provider and one account, that closing a booking distinguishes "did not come" from
"cancelled", and that the schema's own invariants (the interval constraint, the occupancy
predicate, the migration's indexes) match the code that relies on them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.scheduling import OCCUPYING_STATUSES
from app.models import Base
from tests.column_fit import assert_fits_columns
from tests.conftest import create_patient


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat()


def _tomorrow(hour: int = 10, minute: int = 0) -> datetime:
    base = datetime.now(UTC) + timedelta(days=1)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


async def _book(client, patient_id: str, *, start: datetime, minutes: int = 30, **overrides):
    payload = {
        "provider_name": "Dr Priya Nair",
        "starts_at": _iso(start),
        "ends_at": _iso(start + timedelta(minutes=minutes)),
    }
    payload.update(overrides)
    return await client.post(f"/api/v1/patients/{patient_id}/appointments", json=payload)


# --- booking ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_booking_is_created_and_returns_its_slot(auth_client):
    patient = await create_patient(auth_client)
    start = _tomorrow()
    resp = await _book(auth_client, patient["id"], start=start, reason="Follow-up of BP control")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "scheduled"
    assert body["modality"] == "in_person"
    assert body["provider_name"] == "Dr Priya Nair"
    assert body["reason"] == "Follow-up of BP control"


@pytest.mark.asyncio
async def test_an_overlapping_booking_for_the_same_provider_is_refused(auth_client):
    """The refusal that is the point of the feature.

    A double-booked slot is two patients in one waiting room, and the second of them finds out
    by arriving. Warning and proceeding would leave nothing in the record saying anybody chose
    to overbook.
    """
    patient = await create_patient(auth_client)
    start = _tomorrow()
    assert (await _book(auth_client, patient["id"], start=start)).status_code == 201

    clash = await _book(auth_client, patient["id"], start=start + timedelta(minutes=15))
    assert clash.status_code == 409, clash.text
    assert clash.json()["code"] == "appointment_conflict"


@pytest.mark.asyncio
async def test_a_back_to_back_booking_is_accepted(auth_client):
    """Half-open intervals, end to end. With closed intervals a full morning is unbookable."""
    patient = await create_patient(auth_client)
    start = _tomorrow()
    assert (await _book(auth_client, patient["id"], start=start)).status_code == 201

    adjacent = await _book(auth_client, patient["id"], start=start + timedelta(minutes=30))
    assert adjacent.status_code == 201, adjacent.text


@pytest.mark.asyncio
async def test_two_providers_may_be_booked_at_the_same_time(auth_client):
    patient = await create_patient(auth_client)
    start = _tomorrow()
    assert (await _book(auth_client, patient["id"], start=start)).status_code == 201

    other = await _book(auth_client, patient["id"], start=start, provider_name="Dr Anil Rao")
    assert other.status_code == 201, other.text


@pytest.mark.asyncio
async def test_the_same_provider_name_in_another_account_does_not_conflict(
    auth_client, second_auth_client
):
    """Two practices may each employ a Dr Sharma, and neither can see the other's diary. A
    cross-account refusal would be a conflict with something the clinician cannot even look at.
    """
    mine = await create_patient(auth_client)
    theirs = await create_patient(second_auth_client, full_name="Another Practice's Patient")
    start = _tomorrow()

    assert (await _book(auth_client, mine["id"], start=start)).status_code == 201
    assert (await _book(second_auth_client, theirs["id"], start=start)).status_code == 201


@pytest.mark.asyncio
async def test_a_cancelled_slot_becomes_bookable_again(auth_client):
    """Cancelling is the only thing that frees time — see ``OCCUPYING_STATUSES``. If it did
    not, a clinic that reschedules twice in a morning would lock itself out of its own diary."""
    patient = await create_patient(auth_client)
    start = _tomorrow()
    first = await _book(auth_client, patient["id"], start=start)
    assert first.status_code == 201

    cancelled = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments/{first.json()['id']}/cancel",
        json={"reason": "Patient rang to rearrange"},
    )
    assert cancelled.status_code == 200, cancelled.text

    again = await _book(auth_client, patient["id"], start=start)
    assert again.status_code == 201, again.text


@pytest.mark.asyncio
async def test_an_inverted_slot_is_refused_before_anything_is_written(auth_client):
    patient = await create_patient(auth_client)
    start = _tomorrow()
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments",
        json={
            "provider_name": "Dr Priya Nair",
            "starts_at": _iso(start),
            "ends_at": _iso(start - timedelta(minutes=30)),
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "invalid_appointment_slot"

    listed = await auth_client.get(f"/api/v1/patients/{patient['id']}/appointments")
    assert listed.json() == []


@pytest.mark.asyncio
async def test_a_booking_in_the_past_is_accepted(auth_client):
    """Writing up a walk-in after the fact is ordinary. Refusing it would push people into
    backdating the visit itself, which loses the thing the record is for."""
    patient = await create_patient(auth_client)
    resp = await _book(auth_client, patient["id"], start=datetime.now(UTC) - timedelta(hours=3))
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_a_booking_beyond_the_horizon_is_refused(auth_client):
    patient = await create_patient(auth_client)
    resp = await _book(auth_client, patient["id"], start=datetime.now(UTC) + timedelta(days=900))
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_appointment_slot"


# --- availability -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_provider_with_no_recorded_hours_is_reported_as_unrecorded(auth_client):
    """Distinct from "outside hours", and the distinction is the reason the table exists: a
    question that could not be asked is never reported as one that passed."""
    patient = await create_patient(auth_client)
    resp = await _book(auth_client, patient["id"], start=_tomorrow())
    assert resp.json()["availability_note"] == "availability_not_recorded"


@pytest.mark.asyncio
async def test_a_booking_outside_recorded_hours_is_accepted_and_flagged(auth_client):
    """Advisory, not a refusal. Clinics run late, and a system that refused a 5.40pm booking
    against a 5.30pm close would teach its users to keep the diary somewhere else."""
    patient = await create_patient(auth_client)
    # A one-minute window on a weekday nothing will be booked in.
    window = await auth_client.post(
        "/api/v1/appointments/availability",
        json={
            "provider_name": "Dr Priya Nair",
            "weekday": 0,
            "start_minute": 540,
            "end_minute": 541,
        },
    )
    assert window.status_code == 201, window.text

    resp = await _book(auth_client, patient["id"], start=_tomorrow())
    assert resp.status_code == 201, resp.text
    assert resp.json()["availability_note"] == "outside_availability"


@pytest.mark.asyncio
async def test_recording_the_same_window_twice_updates_it_rather_than_failing(auth_client):
    first = await auth_client.post(
        "/api/v1/appointments/availability",
        json={
            "provider_name": "Dr Priya Nair",
            "weekday": 2,
            "start_minute": 540,
            "end_minute": 780,
        },
    )
    assert first.status_code == 201, first.text
    again = await auth_client.post(
        "/api/v1/appointments/availability",
        json={
            "provider_name": "Dr Priya Nair",
            "weekday": 2,
            "start_minute": 540,
            "end_minute": 1020,
        },
    )
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["end_minute"] == 1020


@pytest.mark.asyncio
async def test_a_window_ending_before_it_starts_is_refused_by_the_schema(auth_client):
    resp = await auth_client.post(
        "/api/v1/appointments/availability",
        json={
            "provider_name": "Dr Priya Nair",
            "weekday": 2,
            "start_minute": 780,
            "end_minute": 540,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_removing_a_window_leaves_bookings_already_made_inside_it_alone(auth_client):
    """Availability describes when a provider works; it is not a permission the diary hangs
    off. Withdrawing it must not silently invalidate appointments agreed with patients."""
    patient = await create_patient(auth_client)
    window = await auth_client.post(
        "/api/v1/appointments/availability",
        json={
            "provider_name": "Dr Priya Nair",
            "weekday": 3,
            "start_minute": 0,
            "end_minute": 1440,
        },
    )
    booked = await _book(auth_client, patient["id"], start=_tomorrow())
    assert booked.status_code == 201

    removed = await auth_client.delete(f"/api/v1/appointments/availability/{window.json()['id']}")
    assert removed.status_code == 204

    listed = await auth_client.get(f"/api/v1/patients/{patient['id']}/appointments")
    assert [row["id"] for row in listed.json()] == [booked.json()["id"]]


@pytest.mark.asyncio
async def test_another_accounts_availability_window_cannot_be_removed(
    auth_client, second_auth_client
):
    window = await auth_client.post(
        "/api/v1/appointments/availability",
        json={
            "provider_name": "Dr Priya Nair",
            "weekday": 4,
            "start_minute": 540,
            "end_minute": 780,
        },
    )
    assert window.status_code == 201
    stolen = await second_auth_client.delete(
        f"/api/v1/appointments/availability/{window.json()['id']}"
    )
    assert stolen.status_code == 404, stolen.text


# --- lifecycle ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rescheduling_moves_the_booking_without_clashing_with_itself(auth_client):
    patient = await create_patient(auth_client)
    start = _tomorrow()
    booked = await _book(auth_client, patient["id"], start=start)
    appointment_id = booked.json()["id"]

    moved = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments/{appointment_id}/reschedule",
        json={
            "starts_at": _iso(start + timedelta(minutes=10)),
            "ends_at": _iso(start + timedelta(minutes=40)),
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["starts_at"].startswith(_iso(start + timedelta(minutes=10))[:16])


@pytest.mark.asyncio
async def test_rescheduling_into_another_bookings_slot_is_refused(auth_client):
    patient = await create_patient(auth_client)
    start = _tomorrow()
    first = await _book(auth_client, patient["id"], start=start)
    second = await _book(auth_client, patient["id"], start=start + timedelta(hours=2))

    clash = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments/{second.json()['id']}/reschedule",
        json={"starts_at": _iso(start), "ends_at": _iso(start + timedelta(minutes=30))},
    )
    assert clash.status_code == 409
    assert clash.json()["code"] == "appointment_conflict"
    assert first.status_code == 201


@pytest.mark.asyncio
async def test_a_cancelled_booking_cannot_be_rescheduled(auth_client):
    """Its slot has been released and may already be somebody else's."""
    patient = await create_patient(auth_client)
    start = _tomorrow()
    booked = await _book(auth_client, patient["id"], start=start)
    appointment_id = booked.json()["id"]
    await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments/{appointment_id}/cancel",
        json={"reason": "Patient declined follow-up"},
    )

    moved = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments/{appointment_id}/reschedule",
        json={
            "starts_at": _iso(start + timedelta(days=1)),
            "ends_at": _iso(start + timedelta(days=1, minutes=30)),
        },
    )
    assert moved.status_code == 409
    assert moved.json()["code"] == "appointment_not_open"


@pytest.mark.asyncio
async def test_a_missed_appointment_is_recorded_as_no_show_and_not_as_cancelled(auth_client):
    """The distinction is the reason both statuses exist. A cancelled follow-up is one somebody
    decided against; a missed one is a patient who has fallen out of care."""
    patient = await create_patient(auth_client)
    booked = await _book(auth_client, patient["id"], start=_tomorrow())

    closed = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments/{booked.json()['id']}/outcome",
        json={"attended": False},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "no_show"
    assert closed.json()["cancelled_at"] is None


@pytest.mark.asyncio
async def test_an_attended_appointment_closes_as_completed(auth_client):
    patient = await create_patient(auth_client)
    booked = await _book(auth_client, patient["id"], start=_tomorrow())
    closed = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments/{booked.json()['id']}/outcome",
        json={"attended": True},
    )
    assert closed.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_a_cancellation_reason_of_only_whitespace_is_refused(auth_client):
    """``min_length`` counts raw characters, so three spaces satisfied it and stored "". Same
    hole that was closed on hard-block override reasoning and on the SBAR fields."""
    patient = await create_patient(auth_client)
    booked = await _book(auth_client, patient["id"], start=_tomorrow())
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments/{booked.json()['id']}/cancel",
        json={"reason": "    "},
    )
    assert resp.status_code == 422


# --- diary and reminders --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_diary_returns_the_accounts_forthcoming_bookings_only(
    auth_client, second_auth_client
):
    mine = await create_patient(auth_client)
    theirs = await create_patient(second_auth_client, full_name="Another Practice's Patient")
    await _book(auth_client, mine["id"], start=_tomorrow())
    await _book(second_auth_client, theirs["id"], start=_tomorrow(hour=14))

    diary = await auth_client.get("/api/v1/appointments/diary")
    assert diary.status_code == 200, diary.text
    assert {row["patient_id"] for row in diary.json()} == {mine["id"]}


@pytest.mark.asyncio
async def test_the_diary_can_be_narrowed_to_one_provider(auth_client):
    patient = await create_patient(auth_client)
    await _book(auth_client, patient["id"], start=_tomorrow())
    await _book(auth_client, patient["id"], start=_tomorrow(hour=14), provider_name="Dr Anil Rao")

    narrowed = await auth_client.get(
        "/api/v1/appointments/diary", params={"provider_name": "Dr Anil Rao"}
    )
    assert [row["provider_name"] for row in narrowed.json()] == ["Dr Anil Rao"]


@pytest.mark.asyncio
async def test_the_reminder_queue_never_claims_anything_was_sent(auth_client):
    """This deployment has no messaging transport. A queue that implied one would stop the
    clinic telephoning, which is worse than offering no reminders at all."""
    resp = await auth_client.get("/api/v1/appointments/reminders")
    assert resp.status_code == 200, resp.text
    assert resp.json()["delivery_channel"] == "none"


@pytest.mark.asyncio
async def test_a_reminder_falls_due_inside_its_lead_time(auth_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "appointment_reminder_leads", "120")
    patient = await create_patient(auth_client)
    # 150 minutes out, with a 120-minute lead: the reminder falls due in 30 minutes, inside the
    # hour-long window asked for below. An appointment closer than the lead time is *past* its
    # reminder, which is deliberately not returned — see the core module's docstring.
    await _book(auth_client, patient["id"], start=datetime.now(UTC) + timedelta(minutes=150))

    resp = await auth_client.get("/api/v1/appointments/reminders", params={"horizon_minutes": 60})
    items = resp.json()["items"]
    assert [item["lead_minutes"] for item in items] == [120]
    assert items[0]["patient_id"] == patient["id"]


@pytest.mark.asyncio
async def test_a_cancelled_appointment_generates_no_reminder(auth_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "appointment_reminder_leads", "120")
    patient = await create_patient(auth_client)
    booked = await _book(
        auth_client, patient["id"], start=datetime.now(UTC) + timedelta(minutes=150)
    )
    await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments/{booked.json()['id']}/cancel",
        json={"reason": "Patient rang to cancel"},
    )

    resp = await auth_client.get("/api/v1/appointments/reminders", params={"horizon_minutes": 60})
    assert resp.json()["items"] == []


def test_a_malformed_reminder_lead_is_dropped_rather_than_crashing_the_read():
    """This setting is read on a routine diary request. A typo in it must not take the
    scheduling routes down — a reminder that does not fire is visible in the queue, and a
    startup crash on a comma is not something the clinic can recover from."""
    from app.config import Settings

    parsed = Settings(appointment_reminder_leads="1440, oops, 120, -5, 120").reminder_lead_minutes
    assert parsed == (1440, 120)


# --- audit --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_booking_is_audited_without_the_reason_text(auth_client):
    """The reason a patient is coming is clinician prose about a patient, and the trail is
    unencrypted, append-only and never pruned. Only whether one was written is recorded."""
    patient = await create_patient(auth_client)
    await _book(
        auth_client,
        patient["id"],
        start=_tomorrow(),
        reason="Query recurrence of the breast lump",
    )

    audit = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/audit",
        params={"action": "appointment_booked", "limit": 50},
    )
    entries = audit.json()["items"]
    assert len(entries) == 1
    payload = entries[0]["payload"]
    assert payload["reason_recorded"] is True
    assert payload["provider_name"] == "Dr Priya Nair"
    assert "breast" not in str(payload).lower()


@pytest.mark.asyncio
async def test_rescheduling_records_both_ends_of_the_move(auth_client):
    """The row afterwards holds only the new time, and "this was moved from Tuesday" is the
    question asked about an appointment somebody missed."""
    patient = await create_patient(auth_client)
    start = _tomorrow()
    booked = await _book(auth_client, patient["id"], start=start)
    await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments/{booked.json()['id']}/reschedule",
        json={
            "starts_at": _iso(start + timedelta(days=2)),
            "ends_at": _iso(start + timedelta(days=2, minutes=30)),
        },
    )

    audit = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/audit",
        params={"action": "appointment_rescheduled", "limit": 50},
    )
    payload = audit.json()["items"][0]["payload"]
    assert payload["previous_starts_at"] != payload["starts_at"]


@pytest.mark.asyncio
async def test_reading_the_chart_list_is_audited_as_a_disclosure(auth_client):
    patient = await create_patient(auth_client)
    await _book(auth_client, patient["id"], start=_tomorrow())
    await auth_client.get(f"/api/v1/patients/{patient['id']}/appointments")

    audit = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/audit",
        params={"action": "appointment_list_viewed", "limit": 50},
    )
    assert audit.json()["items"][0]["payload"] == {"appointment_count": 1}


# --- schema invariants ----------------------------------------------------------------------


def test_the_occupancy_predicate_matches_the_occupying_statuses():
    """``uq_appointments_provider_start`` spells its status list out literally, because
    ``text()`` must take a literal everywhere in this codebase (see
    tests/test_sql_injection_surface.py). This is what holds the two spellings together — a
    status added to ``OCCUPYING_STATUSES`` without touching the index would leave the database
    enforcing occupancy over a different set than the service reads.
    """
    expected = ", ".join(f"'{status}'" for status in OCCUPYING_STATUSES)
    index = next(
        index
        for index in Base.metadata.tables["appointments"].indexes
        if index.name == "uq_appointments_provider_start"
    )
    for dialect in ("sqlite", "postgresql"):
        predicate = str(index.dialect_options[dialect]["where"])
        assert f"status IN ({expected})" in predicate, predicate


def test_the_migration_indexes_match_the_orm():
    """0001 builds a fresh database from ORM metadata and 0040 alters an existing one. Read
    apart, the two descriptions of the same index drift and a fresh deploy stops matching an
    upgraded one — the failure ``test_the_two_list_indexes_match_the_migration_definitions``
    guards for migration 0027.
    """
    import ast
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[3]
        / "data"
        / "migrations"
        / "versions"
        / "0040_appointments.py"
    )
    tree = ast.parse(migration.read_text())
    declared: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "_INDEXES" for t in targets):
            continue
        value = getattr(node, "value", None)
        if not isinstance(value, ast.List):
            continue
        for entry in value.elts:
            assert isinstance(entry, ast.Tuple)
            name, table, definition = (element.value for element in entry.elts[:3])
            declared[name] = (table, definition)
    assert declared, "no _INDEXES list found in 0040_appointments.py"

    for name, (table_name, definition) in declared.items():
        columns = tuple(
            part.strip().split()[0] for part in definition.strip("()").split(",") if part.strip()
        )
        table = Base.metadata.tables[table_name]
        index = next((i for i in table.indexes if i.name == name), None)
        assert index is not None, f"{table_name} has no index {name} in the ORM"
        orm_columns = tuple(
            getattr(expr, "name", None) or str(expr).split()[0] for expr in index.expressions
        )
        assert orm_columns == columns, (
            f"migration 0040 creates {name} on {table_name} {definition}, but the ORM declares "
            f"{orm_columns} — a fresh create_all deploy would differ from an upgraded database"
        )


@pytest.mark.asyncio
async def test_a_provider_name_at_the_column_ceiling_survives_a_column_typed_database():
    """SQLite enforces no VARCHAR length; PostgreSQL raises. The suite would stay green over a
    row that could never be written in production — see ``tests/column_fit.py``."""
    from app.models.appointment import Appointment

    start = _tomorrow()
    row = Appointment(
        account_id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        provider_name="D" * 200,
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        status="scheduled",
        modality="in_person",
        appointment_type="A" * 50,
        source_protocol_key="P" * 60,
    )
    assert_fits_columns(row)
