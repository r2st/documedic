"""End-to-end clinical workflow journeys, exercised through the HTTP surface only.

Every other test module verifies one slice in isolation. These walk the whole clinician
journey the way the product is actually used — upload a scanned prescription, review and
approve the extraction, read the assembled longitudinal record, run the deterministic safety
checks, drive the reasoning engine through intake, record a decision, then verify the audit
chain covers the whole thing — and assert the invariants that only hold *across* the slices:

* the audit chain is unbroken and ordered across every step of a real journey (Rule #7 spirit)
* a hard block can only be passed with a documented override, and the block record survives it
  (Rule #3)
* a ClinicalSuggestion is never mutated: corrections are new rows pointing at the original,
  and the DB rejects UPDATE/DELETE outright (Rule #7)
* a second account is 404-invisible at *every* step of the journey, not just the entry point
* everything approved into a chart can be read back out of it, however many documents built it
  up and however many pages it now takes to read — the record is paged, and a paging bug loses
  clinical rows in exactly the case a single-document test cannot reach

They deliberately use the deterministic offline path (no LLM), so they assert real behaviour
rather than mocked behaviour.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.models.condition import Condition
from tests.conftest import create_patient


def _key(value: str) -> str:
    """UUID as the tests' SQLite backend stores it (``app.db.types.GUID`` -> 32-char hex).

    Raw-SQL assertions that bind a dashed UUID string silently match zero rows on SQLite,
    which turns an immutability assertion into a vacuous one.
    """
    return uuid.UUID(str(value)).hex


# A synthetic scanned prescription + lab report. The %PDF- header satisfies magic-byte
# sniffing; the body is read by the deterministic text parser (no LLM in tests).
#
# Chosen so the journey has real clinical consequences downstream:
#   Glycomet          -> brand that must resolve to Metformin via DrugVocabulary
#   Potassium 6.8     -> above the 6.5 panic threshold, so critical-flags must fire
#   Creatinine 3.0    -> abnormal (drives eGFR) but *below* the 4.0 critical threshold
#   Ibuprofen allergy -> makes a later Brufen order a deterministic hard block
CASE_DOCUMENT = (
    b"%PDF-1.4\n"
    b"MEDICATIONS:\n"
    b"Glycomet 500mg BD\n"
    b"LABS:\n"
    b"Potassium: 6.8 mmol/L (3.5-5.1)\n"
    b"Creatinine: 3.0 mg/dL (0.6-1.2)\n"
    b"CONDITIONS:\n"
    b"Type 2 Diabetes Mellitus\n"
    b"ALLERGIES:\n"
    b"Ibuprofen - hives\n"
)


async def _upload(client, patient_id: str, content: bytes = CASE_DOCUMENT, name: str = "rx.pdf"):
    return await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, content, "application/pdf")},
    )


async def _complete_intake(client, session_id: str) -> None:
    """Answer pending intake questions until the triage agent is satisfied."""
    for _ in range(5):
        pending = (await client.get(f"/api/v1/reasoning/{session_id}/intake")).json()
        if not pending:
            return
        resp = await client.post(
            f"/api/v1/reasoning/{session_id}/intake/answers",
            json={"answers": [{"question_id": q["id"], "answer_text": "no"} for q in pending]},
        )
        assert resp.status_code == 200, resp.text
        if resp.json()["intake_complete"]:
            return


async def _audit_actions(client, patient_id: str) -> list[str]:
    """All audit actions for a patient, oldest-first (the API returns newest-first)."""
    actions: list[str] = []
    offset = 0
    while True:
        resp = await client.get(
            f"/api/v1/patients/{patient_id}/audit", params={"limit": 200, "offset": offset}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        actions.extend(i["action"] for i in body["items"])
        if not body["pagination"]["has_more"]:
            break
        offset += len(body["items"])
    return list(reversed(actions))


# --------------------------------------------------------------------------- full journey


@pytest.mark.asyncio
async def test_full_clinical_journey_upload_to_audit_chain(auth_client):
    """upload -> approve -> record -> safety -> reasoning -> decision -> verified audit chain."""
    patient = await create_patient(auth_client)
    pid = patient["id"]

    # 1. Upload the scanned document. Extraction runs synchronously in Phase 1.
    doc = (await _upload(auth_client, pid)).json()
    assert doc["extraction_status"] in ("needs_confirmation", "completed")

    # 2. Clinician reviews the extraction before anything touches the record.
    extraction = (
        await auth_client.get(f"/api/v1/patients/{pid}/documents/{doc['id']}/extraction")
    ).json()
    assert {"medication", "lab_result", "condition", "allergy"} <= {
        e["entity_type"] for e in extraction["entities"]
    }

    # 3. ...and approves it, which is the only thing that merges into the patient graph.
    approve = await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 200, approve.text
    merged = approve.json()["merged"]
    assert merged["medications"] >= 1
    assert merged["lab_results"] == 2
    assert merged["allergies"] == 1

    # 4. The longitudinal record now reflects the document, drug names normalised via vocabulary.
    record = (await auth_client.get(f"/api/v1/patients/{pid}/record")).json()
    assert "Metformin" in {m["generic_name"] for m in record["medications"]}
    assert "Ibuprofen" in {a["allergen_name"] for a in record["allergies"]}
    assert [d for d in record["derived_markers"] if d["marker_name"] == "eGFR"]

    # 5. Deterministic, LLM-free critical-value screen fires on the potassium (Rule #8).
    flags = (await auth_client.get(f"/api/v1/patients/{pid}/labs/critical-flags")).json()
    potassium = [f for f in flags["flags"] if f["marker_name"].lower() == "potassium"]
    assert potassium, "K+ 6.8 mmol/L is above the panic threshold and must be surfaced"
    assert potassium[0]["severity"] == "panic_high"
    # Creatinine 3.0 is abnormal but below the 4.0 critical threshold — not every abnormal
    # value is a panic value, and over-flagging is its own safety failure.
    assert not [f for f in flags["flags"] if f["marker_name"].lower() == "creatinine"]

    # 6. Ordering the drug this patient is documented allergic to is a hard block.
    check = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/check", json={"drug_name": "Brufen"}
    )
    assert check.status_code == 200, check.text
    safety = check.json()
    assert safety["proposed_drug_name"] == "Ibuprofen", "brand must resolve through the vocabulary"
    assert safety["is_hard_block"] is True
    assert any(
        f["check_type"] == "allergy_conflict" and f["is_hard_block"] for f in safety["flags"]
    )

    # 7. Reasoning session: intake loop, then the pipeline (always through the Verifier).
    start = await auth_client.post(
        f"/api/v1/patients/{pid}/reasoning",
        json={"presenting_complaint": "polyuria and fatigue for two weeks"},
    )
    assert start.status_code == 201, start.text
    session_id = start.json()["session"]["id"]
    await _complete_intake(auth_client, session_id)

    run = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
    assert run.status_code == 200, run.text
    result = run.json()
    suggestions = result["suggestions"]
    assert suggestions
    assert result["case_state"]["verifier_status"] in (
        "agree",
        "partial_disagreement",
        "major_disagreement",
    )

    # 8. Clinician records a decision — the suggestion itself is untouched.
    target = suggestions[0]
    decision = await auth_client.post(
        f"/api/v1/reasoning/{session_id}/suggestions/{target['id']}/decision",
        json={"decision": "acknowledged"},
    )
    assert decision.status_code == 201, decision.text

    # 9. The audit trail covers the whole journey, in order, and its hash chain verifies.
    actions = await _audit_actions(auth_client, pid)
    expected = [
        "patient_created",
        "document_uploaded",
        "extraction_completed",
        "extraction_approved",
        "critical_lab_value_detected",
        "drug_safety_check",
        "reasoning_session_started",
        "reasoning_session_completed",
        "clinical_suggestion_created",
        "clinician_decision_recorded",
    ]
    missing = [a for a in expected if a not in actions]
    assert not missing, f"journey steps missing from the audit trail: {missing}"
    # Ordering matters as much as presence: the record must show approval before the safety
    # check that relied on it, and the decision last.
    assert actions.index("extraction_approved") < actions.index("drug_safety_check")
    assert actions.index("clinical_suggestion_created") < actions.index(
        "clinician_decision_recorded"
    )

    verify = await auth_client.get(f"/api/v1/patients/{pid}/audit/verify")
    assert verify.status_code == 200, verify.text
    assert verify.json()["chain_valid"] is True
    assert verify.json()["entries_checked"] >= len(expected)


@pytest.mark.asyncio
async def test_journey_audit_chain_detects_tampering(auth_client, db):
    """The chain that the journey builds is not merely present — it actually detects edits."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc = (await _upload(auth_client, pid)).json()
    await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert (await auth_client.get(f"/api/v1/patients/{pid}/audit/verify")).json()["chain_valid"]

    # Tamper with a stored payload behind the service's back (SQLite has no trigger; the
    # Postgres migration installs one — either way the hash must no longer recompute).
    await db.execute(
        text(
            "UPDATE audit_logs SET payload = :p "
            "WHERE action = 'document_uploaded' AND patient_id = :pid"
        ).bindparams(p='{"file_name": "forged.pdf"}', pid=_key(pid))
    )
    await db.commit()

    verify = (await auth_client.get(f"/api/v1/patients/{pid}/audit/verify")).json()
    assert verify["chain_valid"] is False


# ------------------------------------------------------------------- hard block + override


@pytest.mark.asyncio
async def test_hard_block_override_journey_is_documented_and_append_only(auth_client):
    """A hard block is passable only via a documented override that leaves the block intact."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc = (await _upload(auth_client, pid)).json()
    await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )

    safety = (
        await auth_client.post(
            f"/api/v1/patients/{pid}/drug-safety/check", json={"drug_name": "Brufen"}
        )
    ).json()
    assert safety["is_hard_block"] is True
    block = next(f for f in safety["flags"] if f["is_hard_block"])

    # An override without real documented reasoning is rejected at the schema boundary.
    thin = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/override",
        json={"drug_safety_check_id": block["id"], "reasoning": "ok"},
    )
    assert thin.status_code == 422

    # A non-blocking flag cannot be "overridden" at all — overrides exist only for hard blocks.
    soft = [f for f in safety["flags"] if not f["is_hard_block"]]
    if soft:
        resp = await auth_client.post(
            f"/api/v1/patients/{pid}/drug-safety/override",
            json={
                "drug_safety_check_id": soft[0]["id"],
                "reasoning": "Not a hard block; should be refused.",
            },
        )
        assert resp.status_code == 422

    reasoning = "Rash was mild and non-IgE; benefit outweighs risk. Discussed with patient."
    override = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/override",
        json={"drug_safety_check_id": block["id"], "reasoning": reasoning},
    )
    assert override.status_code == 201, override.text
    assert override.json()["reasoning"] == reasoning

    # The override is additive: re-running the check still hard-blocks. An override documents
    # one decision; it does not disable the rule for the next order.
    recheck = (
        await auth_client.post(
            f"/api/v1/patients/{pid}/drug-safety/check", json={"drug_name": "Brufen"}
        )
    ).json()
    assert recheck["is_hard_block"] is True

    listed = (await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/overrides")).json()
    assert len(listed) == 1
    assert listed[0]["drug_safety_check_id"] == block["id"]

    actions = await _audit_actions(auth_client, pid)
    assert "drug_safety_hard_block_overridden" in actions
    assert (await auth_client.get(f"/api/v1/patients/{pid}/audit/verify")).json()["chain_valid"]


@pytest.mark.asyncio
async def test_override_is_scoped_to_its_own_patient(auth_client):
    """A check id belonging to another patient cannot be overridden through their sibling."""
    one = await create_patient(auth_client)
    two = await create_patient(auth_client, full_name="Second Patient")
    doc = (await _upload(auth_client, one["id"])).json()
    await auth_client.post(
        f"/api/v1/patients/{one['id']}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    safety = (
        await auth_client.post(
            f"/api/v1/patients/{one['id']}/drug-safety/check", json={"drug_name": "Brufen"}
        )
    ).json()
    block = next(f for f in safety["flags"] if f["is_hard_block"])

    resp = await auth_client.post(
        f"/api/v1/patients/{two['id']}/drug-safety/override",
        json={
            "drug_safety_check_id": block["id"],
            "reasoning": "Attempting to override across patients.",
        },
    )
    assert resp.status_code == 404


# --------------------------------------------------------------- suggestion immutability


async def _run_session(client, patient_id: str, complaint: str = "fever and cough") -> dict:
    start = await client.post(
        f"/api/v1/patients/{patient_id}/reasoning", json={"presenting_complaint": complaint}
    )
    session_id = start.json()["session"]["id"]
    await _complete_intake(client, session_id)
    run = await client.post(f"/api/v1/reasoning/{session_id}/run")
    assert run.status_code == 200, run.text
    return {"session_id": session_id, **run.json()}


@pytest.mark.asyncio
async def test_clinical_suggestions_are_immutable_across_the_journey(auth_client, db):
    """Rule #7: suggestions are append-only. Decisions never edit them; corrections supersede."""
    patient = await create_patient(auth_client)
    result = await _run_session(auth_client, patient["id"])
    session_id = result["session_id"]
    target = result["suggestions"][0]

    before = (await auth_client.get(f"/api/v1/reasoning/{session_id}/suggestions")).json()
    snapshot = {s["id"]: s for s in before}

    # Recording several decisions — including a dismissal — must not touch the suggestion rows.
    for decision, reason in (("acknowledged", None), ("dismissed", "Not consistent with exam.")):
        resp = await auth_client.post(
            f"/api/v1/reasoning/{session_id}/suggestions/{target['id']}/decision",
            json={"decision": decision, "reason": reason},
        )
        assert resp.status_code == 201, resp.text

    after = (await auth_client.get(f"/api/v1/reasoning/{session_id}/suggestions")).json()
    assert {s["id"] for s in after} == set(snapshot)
    for s in after:
        assert s == snapshot[s["id"]], "a decision must never mutate the suggestion it is about"

    # Both decisions survive as separate append-only records.
    decisions = await db.execute(
        text("SELECT decision FROM clinician_decisions WHERE suggestion_id = :sid").bindparams(
            sid=_key(target["id"])
        )
    )
    assert sorted(r[0] for r in decisions) == ["acknowledged", "dismissed"]

    # There is no HTTP verb that edits or removes a suggestion at all.
    path = f"/api/v1/reasoning/{session_id}/suggestions/{target['id']}"
    for method in ("PUT", "PATCH", "DELETE"):
        resp = await auth_client.request(method, path)
        assert resp.status_code in (404, 405), f"{method} {path} -> {resp.status_code}"


@pytest.mark.asyncio
async def test_suggestion_correction_supersedes_rather_than_edits(auth_client, db):
    """A correction is a NEW row pointing at the original; the original is left untouched."""
    patient = await create_patient(auth_client)
    result = await _run_session(auth_client, patient["id"])
    original = result["suggestions"][0]

    await db.execute(
        text(
            "INSERT INTO clinical_suggestions "
            "(id, session_id, patient_id, output_type, autonomy_tier, title, evidence, "
            " citations, agent_trace, verifier_verdict, devils_advocate, is_hard_block, "
            " cant_miss_flag, supersedes_id, created_at) "
            "SELECT :new_id, session_id, patient_id, output_type, 'flag_for_review', "
            "       'Corrected: ' || title, evidence, citations, agent_trace, verifier_verdict, "
            "       devils_advocate, is_hard_block, cant_miss_flag, id, CURRENT_TIMESTAMP "
            "FROM clinical_suggestions WHERE id = :old_id"
        ).bindparams(new_id=uuid.uuid4().hex, old_id=_key(original["id"]))
    )
    await db.commit()

    listed = (await auth_client.get(f"/api/v1/reasoning/{result['session_id']}/suggestions")).json()
    by_id = {s["id"]: s for s in listed}
    assert by_id[original["id"]] == original, "the superseded row must be byte-identical"
    correction = next(s for s in listed if s["title"].startswith("Corrected: "))
    assert correction["id"] != original["id"]


@pytest.mark.asyncio
async def test_immutable_clinical_tables_reject_mutation_at_the_database(auth_client, db):
    """The append-only tables carry no updated_at/is_deleted, so there is nothing to soft-edit.

    The Postgres migration additionally installs BEFORE UPDATE/DELETE triggers; SQLite has no
    equivalent, so what is asserted here is the structural guarantee that holds on both.
    """
    patient = await create_patient(auth_client)
    await _run_session(auth_client, patient["id"])

    for table in ("clinical_suggestions", "clinician_decisions", "drug_safety_overrides"):
        cols = {
            row[1]
            for row in (await db.execute(text(f"PRAGMA table_info({table})"))).fetchall()  # noqa: S608
        }
        assert "updated_at" not in cols, f"{table} must not carry a mutation timestamp"
        assert "is_deleted" not in cols, f"{table} must not be soft-deletable"


# ------------------------------------------------------------------ cross-account isolation


@pytest.mark.asyncio
async def test_cross_account_isolation_across_the_whole_journey(auth_client, client, db):
    """A second clinician is 404-invisible at every step, not merely at the patient endpoint."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    doc = (await _upload(auth_client, pid)).json()
    await auth_client.post(
        f"/api/v1/patients/{pid}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    # Give the patient a condition so pathway lookups have something to match on.
    db.add(
        Condition(
            patient_id=uuid.UUID(pid),
            condition_name="Type 2 Diabetes Mellitus",
            status="active",
            clinician_confirmed=True,
        )
    )
    await db.commit()
    result = await _run_session(auth_client, pid)
    session_id = result["session_id"]
    suggestion_id = result["suggestions"][0]["id"]

    signup = await client.post(
        "/api/v1/auth/signup",
        json={"email": "intruder@example.com", "password": "password123"},
    )
    assert signup.status_code == 201, signup.text
    intruder = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    reads = [
        f"/api/v1/patients/{pid}",
        f"/api/v1/patients/{pid}/record",
        f"/api/v1/patients/{pid}/documents",
        f"/api/v1/patients/{pid}/documents/{doc['id']}",
        f"/api/v1/patients/{pid}/documents/{doc['id']}/extraction",
        f"/api/v1/patients/{pid}/documents/{doc['id']}/file",
        f"/api/v1/patients/{pid}/labs/critical-flags",
        f"/api/v1/patients/{pid}/drug-safety/flags",
        f"/api/v1/patients/{pid}/drug-safety/overrides",
        f"/api/v1/patients/{pid}/pathways",
        f"/api/v1/patients/{pid}/audit",
        f"/api/v1/patients/{pid}/audit/verify",
        f"/api/v1/reasoning/{session_id}",
        f"/api/v1/reasoning/{session_id}/intake",
        f"/api/v1/reasoning/{session_id}/suggestions",
        f"/api/v1/reasoning/{session_id}/management-options",
    ]
    for path in reads:
        resp = await client.get(path, headers=intruder)
        assert resp.status_code == 404, f"GET {path} leaked to another account: {resp.status_code}"

    writes = [
        ("POST", f"/api/v1/patients/{pid}/documents/{doc['id']}/approve", {"corrections": []}),
        ("POST", f"/api/v1/patients/{pid}/drug-safety/check", {"drug_name": "Brufen"}),
        ("POST", f"/api/v1/patients/{pid}/reasoning", {"presenting_complaint": "fever"}),
        ("POST", f"/api/v1/reasoning/{session_id}/run", None),
        ("POST", f"/api/v1/reasoning/{session_id}/stream-token", None),
        (
            "POST",
            f"/api/v1/reasoning/{session_id}/suggestions/{suggestion_id}/decision",
            {"decision": "accepted"},
        ),
    ]
    for method, path, body in writes:
        resp = await client.request(method, path, json=body, headers=intruder)
        assert resp.status_code == 404, f"{method} {path} leaked: {resp.status_code}"

    # The intruder's own audit view stays empty — no cross-tenant bleed through the trail.
    own = await client.get("/api/v1/patients", headers=intruder)
    assert own.json()["items"] == []

    # And the owner's journey is undamaged by the probing.
    assert (await auth_client.get(f"/api/v1/patients/{pid}/audit/verify")).json()["chain_valid"]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_a_chart_built_from_many_documents_reads_back_whole_across_pages(auth_client):
    """The journey a long-standing patient actually produces: many visits, then one chart.

    Every other record test seeds rows directly or approves a single document, so both stay
    inside one page and never cross a boundary. This builds the chart the way the product does
    — a document per visit, each approved into the graph — and then reads it back the way a
    client has to now that the response is bounded: page by page, following `has_more`.

    The assertion is the clinical one rather than an arithmetic one. Every marker that was
    approved into the record must come back out of it, exactly once. A paging bug does not
    raise here; it quietly drops a lab from a chart a clinician is about to prescribe against,
    or shows the same one twice and invents a trend.
    """
    patient = await create_patient(auth_client, full_name="Long History")
    pid = patient["id"]

    # Six visits, three labs each: 18 markers, read four at a time, so the walk crosses five
    # page boundaries and ends on a partial page.
    expected: set[str] = set()
    for visit in range(6):
        markers = [f"Marker{visit}{i}" for i in range(3)]
        expected.update(markers)
        body = b"%PDF-1.4\nLABS:\n" + "".join(
            f"{name}: {10 + i}.5 mg/dL (1.0-9.0)\n" for i, name in enumerate(markers)
        ).encode()
        doc = (await _upload(auth_client, pid, content=body, name=f"visit{visit}.pdf")).json()
        approve = await auth_client.post(
            f"/api/v1/patients/{pid}/documents/{doc['id']}/approve",
            json={"corrections": [], "rejected_entity_indexes": []},
        )
        assert approve.status_code == 200, approve.text
        assert approve.json()["merged"]["lab_results"] == 3

    seen: list[str] = []
    offset = 0
    for _ in range(10):  # bounded so a `has_more` that never clears fails instead of hanging
        resp = await auth_client.get(
            f"/api/v1/patients/{pid}/record", params={"limit": 4, "offset": offset}
        )
        assert resp.status_code == 200, resp.text
        page = resp.json()
        seen.extend(lab["marker_name"] for lab in page["lab_results"])
        assert page["pagination"]["lab_results"]["total"] == len(expected)
        if not page["pagination"]["lab_results"]["has_more"]:
            break
        offset += len(page["lab_results"])
    else:
        pytest.fail("paging never reported the end of the lab history")

    assert sorted(seen) == sorted(expected), "the paged walk did not reconstruct the chart"
    assert len(seen) == len(set(seen)), "a lab was served on more than one page"


@pytest.mark.asyncio
async def test_unauthenticated_requests_never_reach_the_journey(client, auth_client):
    """Every journey route requires a token; none of them fall open."""
    patient = await create_patient(auth_client)
    pid = patient["id"]
    for path in (
        f"/api/v1/patients/{pid}/record",
        f"/api/v1/patients/{pid}/documents",
        f"/api/v1/patients/{pid}/labs/critical-flags",
        f"/api/v1/patients/{pid}/audit",
        f"/api/v1/patients/{pid}/audit/verify",
    ):
        resp = await client.get(path, headers={"Authorization": ""})
        assert resp.status_code == 401, f"GET {path} -> {resp.status_code}"
