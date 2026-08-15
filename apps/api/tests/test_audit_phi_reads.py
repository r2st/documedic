"""Every endpoint that discloses a patient's clinical data leaves an entry in the trail.

Under the DPDP Act the question asked of this log months later is "who saw this patient's
data", and a trail that records only writes cannot answer it: a clinician who reads a chart
they had no business reading leaves no trace at all. Six read endpoints had that gap —
document listing, extraction review, standing drug-safety flags, a session's clinical
suggestions, the management-options view of those same suggestions, and matched care
pathways.

Two properties are pinned here, and they pull in opposite directions:

* **Completeness** — the read is recorded, against the right patient, with the right action.
  Asserted through the patient's own audit endpoint, because that is the view a compliance
  reviewer or the patient actually gets. An entry recorded against a session id but no
  patient is invisible from there and does not count.
* **Payload thinness** — what is recorded is counts and identifiers, never clinical content.
  ``audit_logs.payload`` is unencrypted, immutable and never pruned, so a drug name written
  into it is a disclosure that outlives the record it describes.

The lock-ordering test is the third leg. On PostgreSQL the append lock is transaction-scoped
and released only at COMMIT, so *when* in the request the entry is appended decides how long
every other request queues behind it.
"""

from __future__ import annotations

import pytest

from tests.conftest import create_patient
from tests.test_documents import PRESCRIPTION
from tests.test_query_efficiency import counting_queries


async def _upload(auth_client, patient_id: str) -> dict:
    resp = await auth_client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _entries(auth_client, patient_id: str, action: str) -> list[dict]:
    resp = await auth_client.get(
        f"/api/v1/patients/{patient_id}/audit", params={"action": action, "limit": 200}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


async def _reasoning_session(auth_client, patient_id: str) -> str:
    started = await auth_client.post(
        f"/api/v1/patients/{patient_id}/reasoning",
        json={"presenting_complaint": "breathless climbing stairs for a month"},
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session"]["id"]
    await auth_client.post(f"/api/v1/reasoning/{session_id}/answers", json={"answers": []})
    run = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert run.status_code == 200, run.text
    return session_id


# ---------------------------------------------------------------- one per endpoint


async def test_listing_documents_is_audited(auth_client):
    patient = await create_patient(auth_client)
    await _upload(auth_client, patient["id"])

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")
    assert resp.status_code == 200

    entries = await _entries(auth_client, patient["id"], "document_list_viewed")
    assert len(entries) == 1, "the document listing left no record of the disclosure"
    assert entries[0]["payload"] == {"document_count": len(resp.json())}


async def test_viewing_an_extraction_is_audited_against_the_document(auth_client):
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"])

    resp = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/extraction"
    )
    assert resp.status_code == 200

    entries = await _entries(auth_client, patient["id"], "extraction_viewed")
    assert len(entries) == 1
    assert entries[0]["entity_id"] == doc["id"], "the entry does not name the document read"
    assert entries[0]["payload"]["entity_count"] == len(resp.json()["entities"])


async def test_reading_the_standing_safety_flags_is_audited(auth_client):
    patient = await create_patient(auth_client)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/drug-safety/flags")
    assert resp.status_code == 200

    entries = await _entries(auth_client, patient["id"], "drug_safety_flags_viewed")
    assert len(entries) == 1
    assert set(entries[0]["payload"]) == {"drugs_evaluated", "flag_count"}


async def test_reading_a_sessions_suggestions_is_audited_against_the_patient(auth_client):
    """Recorded against the patient, not only the session.

    The trail is read per patient. An entry that names a reasoning session and leaves
    ``patient_id`` null is invisible from the one view anyone actually queries, which is the
    same as not recording it.
    """
    patient = await create_patient(auth_client)
    session_id = await _reasoning_session(auth_client, patient["id"])

    resp = await auth_client.get(f"/api/v1/reasoning/{session_id}/suggestions")
    assert resp.status_code == 200

    entries = await _entries(auth_client, patient["id"], "clinical_suggestions_viewed")
    assert len(entries) == 1
    assert entries[0]["entity_id"] == session_id
    assert entries[0]["payload"] == {"suggestion_count": len(resp.json())}


async def test_reading_management_options_is_audited_like_the_full_suggestion_list(auth_client):
    """The filtered view of the same clinical output is the same disclosure.

    Auditing ``/suggestions`` but not ``/management-options`` would leave the content
    reachable by a route that records nothing.
    """
    patient = await create_patient(auth_client)
    session_id = await _reasoning_session(auth_client, patient["id"])

    resp = await auth_client.get(f"/api/v1/reasoning/{session_id}/management-options")
    assert resp.status_code == 200

    entries = await _entries(auth_client, patient["id"], "clinical_suggestions_viewed")
    assert len(entries) == 1
    assert entries[0]["payload"] == {"subset": "management", "suggestion_count": len(resp.json())}


async def test_reading_matched_pathways_is_audited(auth_client):
    patient = await create_patient(auth_client)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/pathways")
    assert resp.status_code == 200

    entries = await _entries(auth_client, patient["id"], "patient_pathways_viewed")
    assert len(entries) == 1
    assert entries[0]["payload"] == {
        "matched_pathways": len(resp.json()["pathways"]),
        "unmapped_conditions": len(resp.json()["unmapped_conditions"]),
    }


# ---------------------------------------------------------------- properties across all six

_READ_ENDPOINTS = [
    ("document_list_viewed", "/api/v1/patients/{pid}/documents"),
    ("extraction_viewed", "/api/v1/patients/{pid}/documents/{doc}/extraction"),
    ("drug_safety_flags_viewed", "/api/v1/patients/{pid}/drug-safety/flags"),
    ("patient_pathways_viewed", "/api/v1/patients/{pid}/pathways"),
]


@pytest.mark.parametrize(
    ("action", "template"), _READ_ENDPOINTS, ids=[e[0] for e in _READ_ENDPOINTS]
)
async def test_each_read_appends_exactly_one_entry_per_request(auth_client, action, template):
    """One request, one entry. Not zero, and not one per row returned.

    A per-row entry would be the read-side version of the N+1 the batched append exists to
    remove, and would bury the fact of the access under its own volume.
    """
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"])
    url = template.format(pid=patient["id"], doc=doc["id"])

    for _ in range(3):
        assert (await auth_client.get(url)).status_code == 200

    entries = await _entries(auth_client, patient["id"], action)
    assert len(entries) == 3, f"three reads of {template} produced {len(entries)} entries"


@pytest.mark.parametrize(
    ("action", "template"), _READ_ENDPOINTS, ids=[e[0] for e in _READ_ENDPOINTS]
)
async def test_a_read_audit_payload_carries_no_clinical_text(auth_client, action, template):
    """Counts and identifiers only.

    ``audit_logs`` is unencrypted, immutable and never pruned, so a drug name or a file name
    written into a payload outlives every mechanism the system has for correcting or removing
    patient data. Asserted structurally — every payload value must be a number, a bool, or one
    of a small set of enumerated status strings — rather than by grepping for known drug
    names, which would pass for any drug the test did not think of.
    """
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"])
    assert (
        await auth_client.get(template.format(pid=patient["id"], doc=doc["id"]))
    ).status_code == 200

    # The closed vocabularies a payload may name: extraction lifecycle states, and the
    # suggestion subset the management-options route discloses. Anything else is free text.
    allowed_strings = {
        "pending",
        "processing",
        "needs_confirmation",
        "completed",
        "failed",
        "management",
    }
    for entry in await _entries(auth_client, patient["id"], action):
        for key, value in entry["payload"].items():
            assert isinstance(value, int | float | bool) or value in allowed_strings, (
                f"{action} payload field {key!r} carries free text ({value!r}) — clinical "
                "content must not be written to the immutable, unencrypted audit payload"
            )


async def test_a_read_by_another_clinician_is_recorded_against_that_account(
    auth_client, second_auth_client
):
    """The trail has to name *who* read, or it answers nothing.

    Sharing is not modelled yet, so the second account cannot reach the chart at all — which
    is itself the assertion: the read is refused, and refusing leaves nothing in the patient's
    trail to confuse a later review with.
    """
    patient = await create_patient(auth_client)
    await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")

    denied = await second_auth_client.get(f"/api/v1/patients/{patient['id']}/documents")
    assert denied.status_code == 404, denied.text

    entries = await _entries(auth_client, patient["id"], "document_list_viewed")
    assert len(entries) == 1, "the refused read should not have appended an entry"
    account_ids = {e["account_id"] for e in entries}
    assert len(account_ids) == 1 and None not in account_ids


async def test_read_auditing_leaves_the_hash_chain_verifiable(auth_client):
    """Broadening the trail must not break the property that makes it worth having."""
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"])
    session_id = await _reasoning_session(auth_client, patient["id"])

    for url in (
        f"/api/v1/patients/{patient['id']}/documents",
        f"/api/v1/patients/{patient['id']}/documents/{doc['id']}/extraction",
        f"/api/v1/patients/{patient['id']}/drug-safety/flags",
        f"/api/v1/patients/{patient['id']}/pathways",
        f"/api/v1/reasoning/{session_id}/suggestions",
        f"/api/v1/reasoning/{session_id}/management-options",
    ):
        assert (await auth_client.get(url)).status_code == 200

    verify = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit/verify")).json()
    assert verify["chain_valid"] is True
    assert verify["entries_checked"] >= 6


# ---------------------------------------------------------------- lock hold time


@pytest.mark.parametrize(
    ("action", "template"), _READ_ENDPOINTS, ids=[e[0] for e in _READ_ENDPOINTS]
)
async def test_the_audit_insert_is_the_last_statement_of_a_read_request(
    auth_client, engine, action, template
):
    """The append happens after the read, not before it.

    This is the whole answer to "broadening the audit trail costs throughput". On PostgreSQL
    the append takes ``pg_advisory_xact_lock``, which has no unlock — it is released when the
    transaction COMMITs. Appending first and reading afterwards would therefore hold a single
    global lock for the length of every audited read, serializing the entire API behind the
    slowest chart being opened. Appending last makes the hold the gap between the INSERT and
    the COMMIT.

    Asserted on statement order because that is the observable form of the property; there is
    nothing to measure on SQLite, where the lock is a no-op.
    """
    patient = await create_patient(auth_client)
    doc = await _upload(auth_client, patient["id"])
    url = template.format(pid=patient["id"], doc=doc["id"])

    with counting_queries(engine) as counter:
        assert (await auth_client.get(url)).status_code == 200

    statements = [s.strip() for s in counter["statements"] if s.strip()]
    inserts = [
        i for i, s in enumerate(statements) if s.upper().startswith("INSERT INTO AUDIT_LOGS")
    ]
    assert len(inserts) == 1, f"{action} issued {len(inserts)} audit inserts"

    after = statements[inserts[0] + 1 :]
    # A savepoint release belongs to the append itself, and the tail read is part of it.
    residual = [s for s in after if not s.upper().startswith(("RELEASE", "SAVEPOINT", "COMMIT"))]
    assert residual == [], (
        f"{action} issued {len(residual)} statement(s) after appending its audit entry, so the "
        f"global append lock is held across them: {residual[:3]}"
    )


async def test_an_audited_read_appends_without_a_second_chain_tail_read(auth_client, engine):
    """One entry about one chart costs two tail reads and one insert — the floor for an append.

    Guards the incremental cost of the decision to audit reads at all: if a read endpoint ever
    grew a second append, it would double the work done under the global lock.

    Two reads, not one, since migration 0025: the global chain tail, and this patient's own.
    The second is what the per-patient link is bought with, and it buys the only check that
    notices an entry *deleted* from a chart's trail — see ``AuditService._patient_tails``. It is
    one indexed lookup per distinct patient in the batch rather than per entry, so a reasoning
    run appending nine entries about one chart still pays it once.
    """
    patient = await create_patient(auth_client)

    with counting_queries(engine) as counter:
        assert (
            await auth_client.get(f"/api/v1/patients/{patient['id']}/pathways")
        ).status_code == 200

    audit_statements = [s for s in counter["statements"] if "audit_logs" in s]
    selects = [s for s in audit_statements if s.lstrip().upper().startswith("SELECT")]
    inserts = [s for s in audit_statements if s.lstrip().upper().startswith("INSERT")]
    assert (len(selects), len(inserts)) == (2, 1), (
        f"an audited read cost {len(selects)} tail reads and {len(inserts)} inserts"
    )


# ---------------------------------------------------------------- the action registry


def test_the_action_registry_matches_what_the_code_actually_records():
    """``AUDIT_ACTIONS`` is the list a reviewer reads to learn what this system logs.

    It had drifted: ``document_downloaded``, ``patient_viewed``, ``patient_record_viewed``,
    ``extraction_failed`` and ``drug_safety_hard_block_overridden`` were all being written
    without appearing in it, while ``record_exported`` appeared in it and was written by
    nothing. Both directions are wrong in the same way — the list stops describing the system
    — so both are asserted.

    Scanned out of the source rather than collected at runtime, because the actions that
    matter most here are the ones on paths a test suite may never reach (a login lockout, a
    failed extraction).
    """
    import pathlib
    import re

    from app.models.audit_log import AUDIT_ACTIONS

    app_root = pathlib.Path(__file__).resolve().parent.parent / "app"
    recorded = {
        match
        for path in app_root.rglob("*.py")
        for match in re.findall(r'action="([a-z_]+)"', path.read_text(encoding="utf-8"))
    }

    unregistered = recorded - set(AUDIT_ACTIONS)
    assert not unregistered, (
        f"these actions are written but not in AUDIT_ACTIONS: {sorted(unregistered)}"
    )
    unwritten = set(AUDIT_ACTIONS) - recorded
    assert not unwritten, (
        f"AUDIT_ACTIONS lists actions no code path records: {sorted(unwritten)} — a registry "
        "that describes disclosures the system never makes is misinformation in a "
        "compliance-facing list"
    )


def test_every_phi_read_action_is_registered():
    """Named explicitly, so deleting a call site cannot quietly shrink the read trail.

    The test above only checks that the registry and the code agree; removing an audit call
    *and* its registry entry would satisfy it. This is the list that has to keep growing.
    """
    from app.models.audit_log import AUDIT_ACTIONS

    expected = {
        "patient_viewed",
        "patient_record_viewed",
        "document_downloaded",
        "document_list_viewed",
        "extraction_viewed",
        "drug_safety_flags_viewed",
        "clinical_suggestions_viewed",
        "patient_pathways_viewed",
    }
    assert expected <= set(AUDIT_ACTIONS), (
        f"PHI-read actions missing from the registry: {sorted(expected - set(AUDIT_ACTIONS))}"
    )


def test_audit_actions_has_no_duplicates():
    from app.models.audit_log import AUDIT_ACTIONS

    assert len(AUDIT_ACTIONS) == len(set(AUDIT_ACTIONS))
