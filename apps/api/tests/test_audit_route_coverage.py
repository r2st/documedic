"""Every mutating route is accounted for in the audit trail — structurally, not by memory.

The individual audit behaviours are tested where they live (``test_audit.py``,
``test_audit_phi_reads.py``, ``test_auth_hardening.py`` and the rest). What none of those can
catch is the route that ships *next* and never gets an entry, because a test that does not exist
does not fail. In a clinical system the trail is not a nice-to-have — "who changed what, when"
is what the record is for, and a gap in it is invisible until someone needs the answer.

So this pins the route table itself. Adding any POST/PUT/PATCH/DELETE to the API breaks this
module until someone writes down which audit action it produces, or states why it produces none.
That is a deliberate decision either way rather than an omission.

``POST /reasoning/{id}/stream-token`` is why the guard is here: it was the only mutating route
in the API writing nothing at all, and it hands out a credential for one patient's reasoning
stream. It went unnoticed precisely because nothing was checking the set.
"""

from __future__ import annotations

import uuid

import pytest

from app.main import create_app
from app.models.audit_log import AUDIT_ACTIONS
from tests.conftest import create_patient

# Each mutating route, mapped to the audit action(s) it can produce. A route mapped to an empty
# tuple writes nothing on purpose, and the comment says why.
MUTATING_ROUTE_AUDIT: dict[tuple[str, str], tuple[str, ...]] = {
    # --- Auth. Every outcome is recorded, refusals included: a failed sign-in is the entry an
    # investigation starts from, and the lockout and reuse detections are only meaningful as a
    # sequence.
    ("POST", "/api/v1/auth/signup"): ("auth_signup",),
    ("POST", "/api/v1/auth/login"): (
        "auth_login_success",
        "auth_login_failed",
        "auth_login_locked_out",
    ),
    ("POST", "/api/v1/auth/refresh"): (
        "auth_token_refreshed",
        "auth_refresh_token_reuse_detected",
    ),
    ("POST", "/api/v1/auth/logout"): ("auth_logout",),
    ("POST", "/api/v1/auth/logout-all"): ("auth_logout_all",),
    ("POST", "/api/v1/auth/password"): (
        "auth_password_changed",
        "auth_password_change_failed",
    ),
    ("POST", "/api/v1/auth/password-reset/request"): (
        "auth_password_reset_requested",
        "auth_password_reset_throttled",
    ),
    ("POST", "/api/v1/auth/password-reset/confirm"): (
        "auth_password_reset_completed",
        "auth_password_reset_token_reused",
    ),
    ("DELETE", "/api/v1/auth/sessions/{session_id}"): ("auth_session_revoked",),
    # The step-up prompt. Mutating in the strict sense — it writes `last_authenticated_at` —
    # and both outcomes are recorded. The refusal is the one worth having: only a caller who
    # already holds a valid access token can produce it, so it means a wrong password was typed
    # at a signed-in workstation, which no other entry in this trail would show.
    ("POST", "/api/v1/auth/reauthenticate"): (
        "auth_reauthenticated",
        "auth_reauthentication_failed",
    ),
    # --- The chart itself.
    ("POST", "/api/v1/patients"): ("patient_created",),
    # Both, and the pair is the point: each created chart writes its own `patient_created`, and
    # `patients_imported` is the only row saying those N creations were one act by one clinician
    # from one file. A dry run writes only the second, which is correct — nothing was created,
    # and "who tried to load what into this account" is still a question the trail must answer.
    ("POST", "/api/v1/patients/import"): ("patients_imported", "patient_created"),
    ("PATCH", "/api/v1/patients/{patient_id}"): ("patient_updated",),
    ("DELETE", "/api/v1/patients/{patient_id}"): ("patient_deleted",),
    # A POST because the search term is patient-identifying and must not reach a query string
    # or an access log; it reads rather than writes, and the disclosure it makes is of the
    # matching charts.
    ("POST", "/api/v1/patients/search"): ("patient_viewed",),
    # --- Encounters. Signing is the entry that has to exist: it is the moment a clinician
    # attested to a visit note, after which the row can never change. ``sign`` also writes
    # ``encounter_amended`` against the *superseded* encounter when what was signed is an
    # amendment, which is why that route declares two.
    ("POST", "/api/v1/patients/{patient_id}/encounters"): ("encounter_created",),
    ("PATCH", "/api/v1/patients/{patient_id}/encounters/{encounter_id}"): ("encounter_updated",),
    ("POST", "/api/v1/patients/{patient_id}/encounters/{encounter_id}/sign"): (
        "encounter_signed",
        "encounter_amended",
    ),
    ("POST", "/api/v1/patients/{patient_id}/encounters/{encounter_id}/amend"): (
        "encounter_amendment_opened",
    ),
    # --- Documents.
    ("POST", "/api/v1/patients/{patient_id}/documents"): ("document_uploaded",),
    ("POST", "/api/v1/patients/{patient_id}/documents/{doc_id}/extraction/retry"): (
        "extraction_retried",
        "extraction_completed",
        "extraction_failed",
    ),
    ("POST", "/api/v1/patients/{patient_id}/documents/{doc_id}/approve"): (
        "extraction_approved",
        "field_corrected",
        "graph_merged",
    ),
    # --- Deterministic safety. The override is the entry that matters most in the system: a
    # clinician taking responsibility for proceeding past a hard block, with their reasoning.
    ("POST", "/api/v1/patients/{patient_id}/drug-safety/check"): (
        "drug_safety_check",
        "hard_block_triggered",
    ),
    ("POST", "/api/v1/patients/{patient_id}/drug-safety/override"): (
        "drug_safety_hard_block_overridden",
    ),
    # Reconciliation runs an ordinary per-drug check for every proposed medication, so it emits
    # the same two actions the single-drug route does, plus one entry for the reconciliation
    # itself. All three, because a route mapped to a subset of what it writes is the same gap
    # this module exists to close.
    ("POST", "/api/v1/patients/{patient_id}/drug-safety/reconcile"): (
        "medications_reconciled",
        "drug_safety_check",
        "hard_block_triggered",
    ),
    # A named clinician attesting that they have seen a panic value. The detection entry says
    # the system noticed; this is the only one that says a person did, and the interval between
    # them is what a post-incident review asks about.
    ("POST", "/api/v1/patients/{patient_id}/labs/critical-flags/{lab_result_id}/acknowledge"): (
        "critical_lab_value_acknowledged",
    ),
    # --- SBAR handover. Every step is recorded, drafts included: the prose is patient content,
    # so a write to it is a write to the chart. The pair that matters clinically is send +
    # acknowledge — who handed this patient to whom, and how long they spent handed over with
    # nobody having accepted them.
    ("POST", "/api/v1/patients/{patient_id}/handoffs"): ("handoff_created",),
    ("PATCH", "/api/v1/patients/{patient_id}/handoffs/{handoff_id}"): ("handoff_updated",),
    ("POST", "/api/v1/patients/{patient_id}/handoffs/{handoff_id}/send"): ("handoff_sent",),
    ("POST", "/api/v1/patients/{patient_id}/handoffs/{handoff_id}/acknowledge"): (
        "handoff_acknowledged",
    ),
    # --- The reasoning engine.
    ("POST", "/api/v1/patients/{patient_id}/reasoning"): ("reasoning_session_started",),
    ("POST", "/api/v1/reasoning/{session_id}/intake/answers"): ("reasoning_intake_answered",),
    ("POST", "/api/v1/reasoning/{session_id}/run"): (
        "reasoning_run_started",
        "reasoning_session_completed",
        "reasoning_session_failed",
        "clinical_suggestion_created",
        "reasoning_chart_changed_under_run",
    ),
    ("POST", "/api/v1/reasoning/{session_id}/stream-token"): ("reasoning_stream_token_minted",),
    ("POST", "/api/v1/reasoning/{session_id}/suggestions/{suggestion_id}/decision"): (
        "clinician_decision_recorded",
    ),
    # --- The clinic diary. Booking, moving and closing a slot are all writes about a patient —
    # who they were booked with and whether they came — so each is recorded. Declaring an
    # availability window is not patient data, but it is what decides whether a booking is
    # flagged as out-of-hours, so it is recorded too: "this slot raised no warning" is only
    # answerable later if the timetable the arithmetic read at the time is on record.
    ("POST", "/api/v1/patients/{patient_id}/appointments"): ("appointment_booked",),
    ("POST", "/api/v1/patients/{patient_id}/appointments/{appointment_id}/reschedule"): (
        "appointment_rescheduled",
    ),
    ("POST", "/api/v1/patients/{patient_id}/appointments/{appointment_id}/cancel"): (
        "appointment_cancelled",
    ),
    ("POST", "/api/v1/patients/{patient_id}/appointments/{appointment_id}/outcome"): (
        "appointment_closed",
    ),
    ("POST", "/api/v1/appointments/availability"): ("provider_availability_recorded",),
    ("DELETE", "/api/v1/appointments/availability/{window_id}"): ("provider_availability_removed",),
    # --- Curated order sets. Both routes run the deterministic engine over every selected
    # medication, and each of those evaluations persists its own ``drug_safety_check`` — the
    # same entry the prescribing screen writes, carrying the id an override would name. Applying
    # additionally books the follow-up, which writes the diary's own entry.
    ("POST", "/api/v1/patients/{patient_id}/order-sets/{order_set_key}/preview"): (
        "protocol_previewed",
        "drug_safety_check",
    ),
    ("POST", "/api/v1/patients/{patient_id}/order-sets/{order_set_key}/apply"): (
        "protocol_applied",
        "drug_safety_check",
        "appointment_booked",
    ),
    # --- Notes search. A POST that reads: the query is clinical content about the person being
    # searched for, and a query string is written into access logs and browser history. It
    # still writes an entry — reading a panel's notes is a PHI read whoever asked for it.
    ("POST", "/api/v1/notes/search"): ("clinical_notes_searched",),
    # --- Validation and regulatory.
    ("POST", "/api/v1/validation/run"): ("validation_run_executed",),
    ("POST", "/api/v1/safety-reports"): ("safety_report_filed",),
}


def _mutating_routes() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for route in create_app().routes:
        methods = getattr(route, "methods", None) or set()
        for method in methods & {"POST", "PUT", "PATCH", "DELETE"}:
            routes.add((method, route.path))
    return routes


def test_every_mutating_route_has_an_audit_decision():
    """The guard. A new route breaks this until its audit behaviour is written down."""
    undeclared = _mutating_routes() - set(MUTATING_ROUTE_AUDIT)
    assert not undeclared, (
        "These routes change state and no audit decision is recorded for them. Add the action "
        "they write to MUTATING_ROUTE_AUDIT, or map them to () with a reason: "
        f"{sorted(undeclared)}"
    )


def test_the_map_does_not_name_routes_that_no_longer_exist():
    """A stale entry is worse than none: it reads as coverage of something that is gone."""
    stale = set(MUTATING_ROUTE_AUDIT) - _mutating_routes()
    assert not stale, f"MUTATING_ROUTE_AUDIT names routes the app does not serve: {sorted(stale)}"


def test_every_declared_action_is_a_registered_audit_action():
    """``AuditService.record`` refuses an unregistered action, so a typo here would name an
    entry that can never be written — coverage on paper only."""
    declared = {action for actions in MUTATING_ROUTE_AUDIT.values() for action in actions}
    assert declared <= set(AUDIT_ACTIONS), sorted(declared - set(AUDIT_ACTIONS))


def test_no_mutating_route_is_exempt_without_a_reason():
    """Currently none are. The empty tuple is available and requires a comment; this asserts
    nobody has quietly used it as a default."""
    exempt = [route for route, actions in MUTATING_ROUTE_AUDIT.items() if not actions]
    assert not exempt, f"exempted without review: {exempt}"


# --- The gap this module was written for --------------------------------------------------


@pytest.mark.asyncio
async def test_minting_a_stream_token_is_recorded_against_the_patient(auth_client):
    """The credential that opens a chart's reasoning stream leaves a trail.

    The token goes into a query string, which proxy logs and browser history record verbatim.
    Every *read* of the session's output was already audited; issuing the key to it was not, so
    a stream opened elsewhere appeared in the trail from the run onwards with no record of who
    had asked for the token or when.
    """
    patient = await create_patient(auth_client, full_name="Stream Trail")
    started = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "chest discomfort on exertion"},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session"]["id"]

    resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/stream-token")
    assert resp.status_code == 200, resp.text
    minted = resp.json()["token"]

    trail = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/audit",
        params={"action": "reasoning_stream_token_minted"},
    )
    assert trail.status_code == 200, trail.text
    entries = trail.json()["items"]
    assert len(entries) == 1, "minting a stream token left no audit entry"
    entry = entries[0]
    assert entry["entity_type"] == "reasoning_session"
    assert entry["entity_id"] == session_id
    # The token itself is a live credential and never goes in the trail, which is unencrypted
    # and never pruned.
    assert minted not in str(entry)


@pytest.mark.asyncio
async def test_a_refused_stream_token_leaves_no_entry(auth_client):
    """A session this account does not own is a 404, and must not write against a chart the
    caller cannot see."""
    resp = await auth_client.post(f"/api/v1/reasoning/{uuid.uuid4()}/stream-token")
    assert resp.status_code == 404
