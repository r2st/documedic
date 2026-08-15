"""``audit_logs.payload`` carries facts about an action, never the text a clinician typed.

The audit trail is the one store in this system with no way back out. It is unencrypted (the
DPDP-sensitive columns on ``patients`` are encrypted; ``audit_logs.payload`` is not), it is
append-only by construction (:class:`app.services.audit_service.AuditService` exposes no update
and no delete), it is hash-chained so that editing a row invalidates every row after it, and it
is never pruned. Anything written here outlives the record it was copied from and cannot be
corrected or erased on a DPDP erasure request — including a patient's name, which is exactly
what a clinician's own prose about a patient tends to contain:

    "Ramesh, 54M, crushing chest pain since 6am"          presenting_complaint
    "Rash was mild and non-IgE; discussed with Ramesh"    hard-block override reasoning
    "Dismissed — Mrs Kumar declined the statin"           clinician decision reason

Three payloads were copying fields like those verbatim and were changed to record a fact about
the text instead (``complaint_chars``, ``reasoning_chars``, ``reason_recorded``) while pointing
at the row that still holds it. Nothing pinned that, so the next payload added to any of those
call sites could put it straight back. This file is that pin, in three layers:

* :func:`test_no_audit_payload_carries_clinician_free_text` drives one long clinical journey
  with a distinct sentinel in every free-text field the API accepts, then sweeps every audit
  row the journey wrote. Black-box: it does not care which module did the writing.
* The ``PAYLOAD_KEYS`` table below is checked against the source by
  :func:`test_every_audit_call_site_matches_the_declared_payload_contract`, which reads every
  ``record``/``AuditDraft`` call in ``app/`` by AST. That covers the call sites no test happens
  to exercise, and turns "someone added a key" into a failure that names the key.
* The per-fix tests assert the surviving payloads are the *documented* shape, and — the half
  that matters clinically — that the trail still leads to the text through ``entity_id``. An
  audit entry that drops the reasoning without pointing anywhere is not privacy-preserving,
  it is lossy.
"""

from __future__ import annotations

import ast
import json
import pathlib
import uuid
from typing import Any

from sqlalchemy import select

from app.models.audit_log import AuditLog
from tests.conftest import create_patient
from tests.test_documents import PRESCRIPTION

# One sentinel per free-text field the API accepts from a clinician. Each is distinctive enough
# that a substring hit anywhere in a payload is a real leak rather than a coincidence, and each
# is distinct from the others so a failure names the field that leaked.
SENTINEL_NAME = "Zephyrine Qadirbux"
SENTINEL_NOTES = "Brittle diabetes; prior anaphylaxis to sulfonamides."
SENTINEL_ADDRESS = "14B Marigold Lane, Kolar Gold Fields"
SENTINEL_PHONE = "9876500042"
SENTINEL_COMPLAINT = "Zephyrine, 54F, crushing retrosternal pain since 6am, diaphoretic"
SENTINEL_INTAKE_ANSWER = "No — Zephyrine denies any prior cardiac admission"
SENTINEL_DECISION_REASON = "Dismissed: Zephyrine declined admission, family collecting her"
SENTINEL_OVERRIDE_REASONING = "Rash was mild and non-IgE; benefit outweighs risk for Zephyrine."
SENTINEL_CORRECTION_VALUE = "Zephyrimycin Qadirbux 500"
SENTINEL_FILE_NAME = "zephyrine_qadirbux_cbc_2026.pdf"

SENTINELS = (
    SENTINEL_NAME,
    SENTINEL_NOTES,
    SENTINEL_ADDRESS,
    SENTINEL_PHONE,
    SENTINEL_COMPLAINT,
    SENTINEL_INTAKE_ANSWER,
    SENTINEL_DECISION_REASON,
    SENTINEL_OVERRIDE_REASONING,
    SENTINEL_CORRECTION_VALUE,
    SENTINEL_FILE_NAME,
)


async def _all_payloads(db) -> list[tuple[str, dict]]:
    """Every audit row written so far, as ``(action, payload)``, oldest first."""
    rows = (await db.execute(select(AuditLog).order_by(AuditLog.sequence.asc()))).scalars().all()
    return [(row.action, row.payload) for row in rows]


def _assert_no_sentinels(entries: list[tuple[str, dict]]) -> None:
    """Assert no sentinel appears anywhere in any payload, at any nesting depth.

    Serialising to JSON rather than walking the dict is deliberate: a payload is stored as JSON,
    so a sentinel buried in a list of dicts (``critical_lab_value_detected`` writes one) is in
    the stored value regardless of the shape that produced it, and a walk that only inspects
    top-level values would miss it.
    """
    for action, payload in entries:
        rendered = json.dumps(payload, default=str)
        for sentinel in SENTINELS:
            assert sentinel not in rendered, (
                f"{sentinel!r} reached audit_logs.payload on {action!r}: {rendered}\n"
                "audit_logs.payload is unencrypted, immutable and never pruned. Record a fact "
                "about the text (a length, a boolean, an id) and leave the text on the row "
                "entity_id already points at."
            )


# --------------------------------------------------------------- the end-to-end sentinel sweep


async def test_no_audit_payload_carries_clinician_free_text(auth_client, db):
    """One journey through every endpoint that takes prose, then a sweep of the whole trail.

    The journey is deliberately long. Each of the three payloads that was fixed lives on a
    different verb — starting a reasoning run, recording a decision on a suggestion, overriding
    a hard block — and none of them is reachable without the steps before it, so a test that
    only exercised one of the three would leave the others unpinned.

    The action-coverage assertion at the end is what stops this from passing vacuously: if a
    refactor makes the override step 422 rather than 201, the sweep still finds no sentinel
    (because nothing was written) and would go green on a path it never ran.
    """
    patient = await create_patient(
        auth_client,
        full_name=SENTINEL_NAME,
        notes=SENTINEL_NOTES,
        address_text=SENTINEL_ADDRESS,
        phone=SENTINEL_PHONE,
    )
    pid = patient["id"]

    # --- documents: the file name is the classic accidental identifier.
    upload = await auth_client.post(
        f"/api/v1/patients/{pid}/documents",
        files={"file": (SENTINEL_FILE_NAME, PRESCRIPTION, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]

    extraction = await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc_id}/extraction")
    assert extraction.status_code == 200, extraction.text

    # --- a clinician correction: the *value* is typed prose; only its field name may be audited.
    approve = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc_id}/approve",
        json={
            "corrections": [
                {
                    "entity_index": 0,
                    "field_name": "brand_name_raw",
                    "value": SENTINEL_CORRECTION_VALUE,
                }
            ],
            "rejected_entity_indexes": [],
        },
    )
    assert approve.status_code == 200, approve.text

    # --- drug safety: a hard block, then the documented override that is the only way past it.
    check = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/check", json={"drug_name": "Brufen"}
    )
    assert check.status_code == 200, check.text
    assert check.json()["is_hard_block"] is True, "the fixture allergy must produce a hard block"
    block = next(f for f in check.json()["flags"] if f["is_hard_block"])

    override = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/override",
        json={
            "drug_safety_check_id": block["id"],
            "reasoning": SENTINEL_OVERRIDE_REASONING,
        },
    )
    assert override.status_code == 201, override.text

    # --- reasoning: complaint, intake answers, the run, and a decision on what it produced.
    start = await auth_client.post(
        f"/api/v1/patients/{pid}/reasoning",
        json={"presenting_complaint": SENTINEL_COMPLAINT},
    )
    assert start.status_code == 201, start.text
    session_id = start.json()["session"]["id"]

    for _ in range(4):
        pending = (await auth_client.get(f"/api/v1/reasoning/{session_id}/intake")).json()
        if not pending:
            break
        answered = await auth_client.post(
            f"/api/v1/reasoning/{session_id}/intake/answers",
            json={
                "answers": [
                    {"question_id": q["id"], "answer_text": SENTINEL_INTAKE_ANSWER} for q in pending
                ]
            },
        )
        assert answered.status_code == 200, answered.text
        if answered.json()["intake_complete"]:
            break

    run = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert run.status_code == 200, run.text
    suggestions = run.json()["suggestions"]
    assert suggestions, "the journey needs a suggestion to record a decision against"

    decision = await auth_client.post(
        f"/api/v1/reasoning/{session_id}/suggestions/{suggestions[0]['id']}/decision",
        json={"decision": "dismissed", "reason": SENTINEL_DECISION_REASON},
    )
    assert decision.status_code == 201, decision.text

    # --- the read paths, which audit disclosure and are the other place counts could grow text.
    for path in (
        f"/api/v1/patients/{pid}",
        f"/api/v1/patients/{pid}/record",
        f"/api/v1/patients/{pid}/documents",
        f"/api/v1/patients/{pid}/drug-safety/flags",
        f"/api/v1/patients/{pid}/pathways",
        f"/api/v1/reasoning/{session_id}/suggestions",
    ):
        assert (await auth_client.get(path)).status_code == 200, path

    # --- an update, whose payload names the changed fields and must not carry their values.
    patched = await auth_client.patch(
        f"/api/v1/patients/{pid}", json={"notes": f"{SENTINEL_NOTES} Reviewed today."}
    )
    assert patched.status_code == 200, patched.text

    entries = await _all_payloads(db)
    _assert_no_sentinels(entries)

    # The journey must actually have produced the entries the sweep claims to have cleared.
    actions = {action for action, _ in entries}
    for required in (
        "patient_created",
        "patient_updated",
        "document_uploaded",
        "extraction_completed",
        "extraction_viewed",
        "field_corrected",
        "extraction_approved",
        "drug_safety_check",
        "drug_safety_hard_block_overridden",
        "reasoning_session_started",
        "reasoning_intake_answered",
        "reasoning_session_completed",
        "clinical_suggestion_created",
        "clinician_decision_recorded",
        "patient_record_viewed",
        "drug_safety_flags_viewed",
    ):
        assert required in actions, f"the journey never reached {required!r}; the sweep is vacuous"


# ------------------------------------------------------- the three payloads that were carrying it


async def test_starting_a_run_audits_the_complaint_length_and_not_the_complaint(auth_client, db):
    """The complaint is the field most likely to name the patient, and it stays on the session."""
    patient = await create_patient(auth_client, full_name=SENTINEL_NAME)
    start = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": SENTINEL_COMPLAINT},
    )
    assert start.status_code == 201, start.text
    session_id = start.json()["session"]["id"]

    row = (
        await db.execute(select(AuditLog).where(AuditLog.action == "reasoning_session_started"))
    ).scalar_one()

    assert set(row.payload) == {"online", "complaint_chars"}
    assert row.payload["complaint_chars"] == len(SENTINEL_COMPLAINT)
    # The trail is not lossy: entity_id points at the row that does hold the complaint.
    assert str(row.entity_id) == session_id
    session = (await auth_client.get(f"/api/v1/reasoning/{session_id}")).json()
    assert session["presenting_complaint"] == SENTINEL_COMPLAINT


async def test_overriding_a_hard_block_audits_the_reasoning_length_and_the_override_id(
    auth_client, db
):
    """The justification for prescribing past an allergy is prose about a named patient."""
    patient = await create_patient(auth_client, full_name=SENTINEL_NAME)
    pid = patient["id"]
    doc = (
        await auth_client.post(
            f"/api/v1/patients/{pid}/documents",
            files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
        )
    ).json()
    await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    check = (
        await auth_client.post(
            f"/api/v1/patients/{pid}/drug-safety/check", json={"drug_name": "Brufen"}
        )
    ).json()
    block = next(f for f in check["flags"] if f["is_hard_block"])

    override = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/override",
        json={"drug_safety_check_id": block["id"], "reasoning": SENTINEL_OVERRIDE_REASONING},
    )
    assert override.status_code == 201, override.text

    row = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == "drug_safety_hard_block_overridden")
        )
    ).scalar_one()

    assert set(row.payload) == {"check_type", "summary", "override_id", "reasoning_chars"}
    assert row.payload["reasoning_chars"] == len(SENTINEL_OVERRIDE_REASONING)
    assert row.payload["override_id"] == override.json()["id"]
    # The reasoning survives on drug_safety_overrides, which the payload's id leads to.
    listed = (await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/overrides")).json()
    assert [o["reasoning"] for o in listed] == [SENTINEL_OVERRIDE_REASONING]


async def test_a_decision_audits_that_a_reason_was_documented_not_what_it_said(auth_client, db):
    """``reason_recorded`` is the compliance question — was the tier's requirement met — and it
    is a boolean, so it answers that without copying a sentence about a patient."""
    patient = await create_patient(auth_client, full_name=SENTINEL_NAME)
    start = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "fever and cough for three days"},
    )
    session_id = start.json()["session"]["id"]
    for _ in range(4):
        pending = (await auth_client.get(f"/api/v1/reasoning/{session_id}/intake")).json()
        if not pending:
            break
        state = await auth_client.post(
            f"/api/v1/reasoning/{session_id}/intake/answers",
            json={"answers": [{"question_id": q["id"], "answer_text": "no"} for q in pending]},
        )
        if state.json()["intake_complete"]:
            break
    suggestion = (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).json()[
        "suggestions"
    ][0]

    resp = await auth_client.post(
        f"/api/v1/reasoning/{session_id}/suggestions/{suggestion['id']}/decision",
        json={"decision": "dismissed", "reason": SENTINEL_DECISION_REASON},
    )
    assert resp.status_code == 201, resp.text

    row = (
        await db.execute(select(AuditLog).where(AuditLog.action == "clinician_decision_recorded"))
    ).scalar_one()

    assert set(row.payload) == {"decision", "decision_record_id", "reason_recorded"}
    assert row.payload["decision"] == "dismissed"
    assert row.payload["reason_recorded"] is True
    assert row.payload["decision_record_id"] == resp.json()["id"]


async def test_a_decision_without_a_reason_records_the_absence_rather_than_an_empty_string(
    auth_client, db
):
    """``reason_recorded`` has to distinguish "documented" from "left blank" — that is the whole
    point of keeping it — so a whitespace-only reason counts as absent, not as present."""
    patient = await create_patient(auth_client)
    start = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/reasoning",
        json={"presenting_complaint": "fever and cough for three days"},
    )
    session_id = start.json()["session"]["id"]
    for _ in range(4):
        pending = (await auth_client.get(f"/api/v1/reasoning/{session_id}/intake")).json()
        if not pending:
            break
        state = await auth_client.post(
            f"/api/v1/reasoning/{session_id}/intake/answers",
            json={"answers": [{"question_id": q["id"], "answer_text": "no"} for q in pending]},
        )
        if state.json()["intake_complete"]:
            break
    suggestions = (await auth_client.post(f"/api/v1/reasoning/{session_id}/run")).json()[
        "suggestions"
    ]

    blank = await auth_client.post(
        f"/api/v1/reasoning/{session_id}/suggestions/{suggestions[0]['id']}/decision",
        json={"decision": "acknowledged", "reason": "   "},
    )
    assert blank.status_code == 201, blank.text
    omitted = await auth_client.post(
        f"/api/v1/reasoning/{session_id}/suggestions/{suggestions[1]['id']}/decision",
        json={"decision": "acknowledged"},
    )
    assert omitted.status_code == 201, omitted.text

    rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.action == "clinician_decision_recorded")
                .order_by(AuditLog.sequence.asc())
            )
        )
        .scalars()
        .all()
    )
    assert [r.payload["reason_recorded"] for r in rows] == [False, False]


async def test_a_correction_audits_the_field_it_touched_and_not_the_value_typed_in(auth_client, db):
    """A correction is a clinician retyping an extracted value; the value is the clinical datum."""
    patient = await create_patient(auth_client, full_name=SENTINEL_NAME)
    pid = patient["id"]
    doc = (
        await auth_client.post(
            f"/api/v1/patients/{pid}/documents",
            files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
        )
    ).json()

    approve = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc['id']}/approve",
        json={
            "corrections": [
                {
                    "entity_index": 0,
                    "field_name": "brand_name_raw",
                    "value": SENTINEL_CORRECTION_VALUE,
                }
            ],
            "rejected_entity_indexes": [],
        },
    )
    assert approve.status_code == 200, approve.text

    row = (
        await db.execute(select(AuditLog).where(AuditLog.action == "field_corrected"))
    ).scalar_one()

    assert row.payload == {"corrections": [{"entity_index": 0, "field": "brand_name_raw"}]}


async def test_an_upload_audits_the_type_and_digest_and_not_the_file_name(auth_client, db):
    """Scans arrive named after the patient; the name lives on the document row entity_id names."""
    patient = await create_patient(auth_client, full_name=SENTINEL_NAME)
    upload = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": (SENTINEL_FILE_NAME, PRESCRIPTION, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text

    row = (
        await db.execute(select(AuditLog).where(AuditLog.action == "document_uploaded"))
    ).scalar_one()

    assert set(row.payload) == {"file_type", "sha256"}
    assert str(row.entity_id) == upload.json()["id"]
    assert upload.json()["file_name"] == SENTINEL_FILE_NAME


# ------------------------------------------------------------ the declared payload-key contract

# Every audit action this application writes, mapped to the exact set of payload keys it may
# carry. Adding a key to a call site without adding it here fails
# :func:`test_every_audit_call_site_matches_the_declared_payload_contract` — which is the point:
# a payload key is a decision about what goes into a permanent unencrypted store, and it should
# be made deliberately rather than by adding a field to a dict.
PAYLOAD_KEYS: dict[str, set[str]] = {
    # --- auth. ``email`` here is the *clinician's* account address, not a patient's, and it is
    # what the lockout check matches failed attempts on; the rest is request provenance.
    "auth_signup": {"email"},
    "auth_login_success": {"email", "ip_address", "user_agent"},
    "auth_login_failed": {"email", "ip_address"},
    # ``scope`` is "email" or "ip_address": which of the two separately-budgeted arms tripped.
    # They call for different operator responses — a guessing run against one clinician versus
    # a distributed run or a shared egress address whose ceiling needs raising — so an entry
    # that does not say which one is not actionable.
    "auth_login_locked_out": {"email", "ip_address", "failed_attempts", "scope"},
    "auth_token_refreshed": {"ip_address", "user_agent"},
    "auth_refresh_token_reuse_detected": {"reused_session_id", "revoked_count"},
    "auth_session_revoked": {"session_id"},
    "auth_session_idle_expired": {"ip_address", "session_id"},
    "auth_logout": set(),
    "auth_logout_all": {"revoked_count"},
    # --- password change and reset. No token, raw or hashed, and no new password: the trail
    # records that a credential moved, never anything that helps move it again.
    "auth_password_changed": {"revoked_sessions"},
    # Empty on purpose: the action name is the whole fact, and there is nothing about a wrong
    # password that can be recorded without recording something about the password.
    "auth_password_change_failed": set(),
    "auth_password_reset_requested": {"ip_address", "superseded_tokens"},
    "auth_password_reset_throttled": {"ip_address", "requests_in_window"},
    # ``token_id`` is the row's own primary key, not the token — the token exists only as a
    # SHA-256 hash and never leaves ``password_reset_tokens`` even in that form.
    "auth_password_reset_completed": {"token_id", "revoked_sessions"},
    "auth_password_reset_token_reused": {"token_id"},
    # Retention sweep. Counts and configuration, no identifiers: the rows it removed are gone,
    # and naming them would recreate in this table the record the sweep exists to retire.
    "auth_sessions_purged": {"removed", "retention_days"},
    # --- patient. ``changed_fields`` is field *names*; the new values are the PII.
    "patient_created": {"consent_given"},
    # consent_given is the one field *value* this payload carries. It is the opposite of PII --
    # it is the lawful basis for holding everything that is one -- and without it a grant and a
    # withdrawal were the same entry.
    "patient_updated": {"changed_fields", "consent_given"},
    "patient_deleted": {"soft_delete"},
    "patient_viewed": set(),
    "patient_record_viewed": set(),
    # ``format`` is the interchange format, ``resources`` a count. Neither is patient data, and
    # together they are what makes a disclosure entry reviewable: which shape left, and how
    # much of the chart was in it.
    "patient_record_exported": {"format", "resources"},
    # --- documents. No file_name anywhere: uploads are named after the patient.
    "document_uploaded": {"file_type", "sha256"},
    "document_list_viewed": {"document_count"},
    "document_downloaded": {"file_type"},
    "extraction_viewed": {"extraction_status", "entity_count"},
    "extraction_completed": {
        "entity_count",
        "confirmation_required_count",
        "ocr_fallback_used",
        "status",
    },
    "extraction_failed": {"failure_type"},
    "field_corrected": {"corrections"},
    "extraction_approved": {"merged", "rejected_count"},
    # --- clinical. ``flags`` here is structured measurements plus the lab row ids they came
    # from, so a reviewer can get back to the result; no free text is interpolated into it.
    "critical_lab_value_detected": {"flags"},
    # ``unreadable`` is the same shape for the rows the screen declined to evaluate: the lab
    # row id, the marker name as the lab printed it, the number, the unit string (or null) and
    # a fixed reason code. All of it is already on the lab_results row this points at, and none
    # of it is clinician free text.
    "critical_lab_value_not_evaluated": {"unreadable"},
    "drug_safety_check": {"drug", "reference_id", "flag_count", "hard_block"},
    "drug_safety_flags_viewed": {"drugs_evaluated", "flag_count"},
    "drug_safety_hard_block_overridden": {
        "check_type",
        "summary",
        "override_id",
        "reasoning_chars",
    },
    "patient_pathways_viewed": {"matched_pathways", "unmapped_conditions"},
    "safety_report_filed": {"category", "severity"},
    # --- reasoning. complaint_chars/reason_recorded are the fixes this file exists for.
    "reasoning_session_started": {"online", "complaint_chars"},
    "reasoning_intake_answered": {"answered", "intake_complete"},
    # Both are enum-ish facts about the claim, not clinical content: a session status and a
    # boolean. Nothing about the case itself is copied here — the complaint stays on the
    # session row that ``entity_id`` points at.
    "reasoning_run_started": {"previous_status", "took_over_abandoned_run"},
    "reasoning_session_failed": {"error"},
    "reasoning_session_completed": {
        "autonomy_tier",
        "verifier_status",
        "degraded",
        "suggestions",
        "hard_blocks",
    },
    "hard_block_triggered": {"hard_blocks"},
    "clinical_suggestion_created": {
        "output_type",
        "autonomy_tier",
        "title",
        "is_hard_block",
        "cant_miss_flag",
    },
    "clinical_suggestions_viewed": {"subset", "suggestion_count"},
    "clinician_decision_recorded": {"decision", "decision_record_id", "reason_recorded"},
    # --- Phase 4 instrumentation.
    "validation_run_executed": {"vignettes", "metrics"},
    "regulatory_dossier_generated": {"generated_at"},
}

# Call sites that pass an already-built dict rather than a literal, so the keys are not readable
# from the call. Each is named here so "unreadable" stays an explicit, reviewed exception rather
# than a silent hole in the AST sweep.
PAYLOAD_NOT_A_LITERAL = {
    # GraphService.merge_entities' per-entity-type insert counts: {"medications": 2, ...}.
    "graph_merged",
}

# Field names that are, by definition, prose a person typed about a patient. None of them may
# appear as a payload key on any action. Spelling the ban out by name is what makes a
# reintroduction fail with the reason rather than with "unexpected key".
FREE_TEXT_KEYS = {
    "address_text",
    "answer_text",
    "complaint",
    "detail",
    "error_detail",
    "file_name",
    "full_name",
    "message",
    "notes",
    "presenting_complaint",
    "reason",
    "reasoning",
    "text",
}

_APP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"
# The service that *defines* the audit API. Its call forwards the caller's payload through, so
# it has no action or keys of its own to check.
_AUDIT_SERVICE = _APP_ROOT / "services" / "audit_service.py"


def _audit_call_sites() -> list[tuple[str, str, int, set[str] | None]]:
    """Every audit append in ``app/``, as ``(file, action, line, payload_keys or None)``.

    ``None`` for the keys means the payload was not a dict literal at the call site, which is
    the case :data:`PAYLOAD_NOT_A_LITERAL` covers. A call whose ``action`` is not a literal
    string is skipped: the only one is the forwarding call inside ``AuditService.record``.
    """
    sites: list[tuple[str, str, int, set[str] | None]] = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path == _AUDIT_SERVICE:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if called not in ("record", "record_many", "AuditDraft"):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            action = kwargs.get("action")
            if not isinstance(action, ast.Constant) or not isinstance(action.value, str):
                continue
            payload = kwargs.get("payload")
            keys: set[str] | None
            if payload is None:
                keys = set()
            elif isinstance(payload, ast.Dict):
                keys = {
                    k.value
                    for k in payload.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
            else:
                keys = None
            sites.append((str(path.relative_to(_APP_ROOT)), action.value, node.lineno, keys))
    return sites


def test_the_audit_call_sites_are_discoverable_at_all() -> None:
    """Guards the AST sweep against passing because it found nothing to check."""
    sites = _audit_call_sites()
    assert len(sites) >= 30, f"only found {len(sites)} audit call sites; the AST walk is broken"
    assert {action for _, action, _, _ in sites} & {
        "reasoning_session_started",
        "drug_safety_hard_block_overridden",
        "clinician_decision_recorded",
    } == {
        "reasoning_session_started",
        "drug_safety_hard_block_overridden",
        "clinician_decision_recorded",
    }


def test_every_audit_call_site_matches_the_declared_payload_contract() -> None:
    """No call site may write a key, or a whole action, that is not declared above."""
    for file, action, line, keys in _audit_call_sites():
        where = f"{file}:{line} ({action})"
        assert action in PAYLOAD_KEYS or action in PAYLOAD_NOT_A_LITERAL, (
            f"{where} audits an action that is not in PAYLOAD_KEYS. Add it there, listing every "
            "payload key and why each one is safe to keep forever in an unencrypted table."
        )
        if action in PAYLOAD_NOT_A_LITERAL:
            continue
        assert keys is not None, (
            f"{where} passes a payload this test cannot read. Either inline the dict or add the "
            "action to PAYLOAD_NOT_A_LITERAL with a note on what the dict contains."
        )
        undeclared = keys - PAYLOAD_KEYS[action]
        assert not undeclared, (
            f"{where} writes undeclared payload key(s) {sorted(undeclared)}. audit_logs.payload "
            "is unencrypted, immutable and never pruned — if the value is text a clinician or a "
            "model wrote about a patient, record a fact about it instead and point entity_id at "
            "the row that keeps it. If it is genuinely safe, declare it in PAYLOAD_KEYS."
        )


def test_no_declared_payload_key_is_a_free_text_field() -> None:
    """The ban, spelled out by name. This is the assertion the three R25 fixes would have failed."""
    for action, keys in PAYLOAD_KEYS.items():
        offending = keys & FREE_TEXT_KEYS
        assert not offending, (
            f"{action!r} declares free-text payload key(s) {sorted(offending)}. Prose a clinician "
            "typed about a patient cannot go into a store that is unencrypted, unpruned and "
            "uneditable — it outlives the record it was copied from and survives a DPDP erasure."
        )


def test_the_declared_contract_has_no_actions_the_code_never_writes() -> None:
    """A stale entry is worse than a missing one: it makes the table look like it covers a call
    site that no longer exists, so a reader trusts a rule nothing is enforcing."""
    written = {action for _, action, _, _ in _audit_call_sites()}
    stale = set(PAYLOAD_KEYS) - written - PAYLOAD_NOT_A_LITERAL
    assert not stale, f"PAYLOAD_KEYS declares actions no call site writes: {sorted(stale)}"


# ------------------------------------------------------------------- the shape of stored values


async def test_every_stored_payload_value_is_a_scalar_or_a_shallow_structure(auth_client, db):
    """A payload is facts about an action, so its values are counts, flags, ids and enum labels.

    Not a type-checking exercise: the reason a payload is allowed to hold ``complaint_chars`` and
    not ``complaint`` is that a number cannot carry an identifier. This asserts the stored values
    keep that property — every string is either short enough to be a label, an id or a digest, or
    it is one of the few generated summaries the trail deliberately keeps.
    """
    patient = await create_patient(auth_client, full_name=SENTINEL_NAME)
    pid = patient["id"]
    doc = (
        await auth_client.post(
            f"/api/v1/patients/{pid}/documents",
            files={"file": ("rx.pdf", PRESCRIPTION, "application/pdf")},
        )
    ).json()
    await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/check", json={"drug_name": "Brufen"}
    )

    # Generated clinical prose the trail keeps on purpose: a suggestion title and a deterministic
    # safety summary are what make an entry readable months later, and neither is user input.
    generated_prose = {"title", "summary", "hard_blocks"}

    def _check(action: str, key: str, value: Any) -> None:
        if isinstance(value, bool | int | float) or value is None:
            return
        if isinstance(value, list):
            for item in value:
                _check(action, key, item)
            return
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                _check(action, f"{key}.{sub_key}", sub_value)
            return
        assert isinstance(value, str), f"{action}.{key} is a {type(value).__name__}"
        if key.split(".")[-1] in generated_prose:
            return
        try:
            uuid.UUID(value)
            return
        except ValueError:
            pass
        assert len(value) <= 80, (
            f"{action}.{key} stores an {len(value)}-character string. A payload value that long "
            "is prose, and prose about a patient does not belong in audit_logs.payload."
        )

    for action, payload in await _all_payloads(db):
        for key, value in payload.items():
            _check(action, key, value)
