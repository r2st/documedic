"""Multi-visit clinical workflows and concurrent access, exercised through HTTP only.

``test_e2e_clinical_workflow`` walks one visit end to end. These cover what only shows up
across *several* steps and *several* clinicians:

* the record accumulates across visits, and the safety verdict for the same drug changes as
  it does — a check is a function of the record at that moment, not of the request
* the order documents arrive in does not change the resulting record or verdict
* two clinicians on the same patient see each other's writes immediately, and append-only
  clinical tables keep both of their entries rather than one clobbering the other
* interaction fan-out: one proposed drug against several interacting current medications,
  with each severity mapped independently and the most conservative outcome winning

The reasoning engine is not driven here — it has its own suites. Everything below runs on
the deterministic offline path, so the assertions are about real behaviour.
"""

from __future__ import annotations

import pytest

from tests.conftest import create_patient


def _document(
    *,
    medications: tuple[str, ...] = (),
    labs: tuple[str, ...] = (),
    conditions: tuple[str, ...] = (),
    allergies: tuple[str, ...] = (),
) -> bytes:
    """A synthetic scanned document. The %PDF- header satisfies magic-byte sniffing; the body
    is read by the deterministic text parser (no LLM in tests)."""
    parts = [b"%PDF-1.4\n"]
    for header, lines in (
        (b"MEDICATIONS:", medications),
        (b"LABS:", labs),
        (b"CONDITIONS:", conditions),
        (b"ALLERGIES:", allergies),
    ):
        if lines:
            parts.append(header + b"\n")
            parts.extend(line.encode() + b"\n" for line in lines)
    return b"".join(parts)


async def _ingest(client, patient_id: str, content: bytes, name: str = "visit.pdf") -> dict:
    """Upload a document and approve its extraction — the only path into the patient graph."""
    upload = await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, content, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]
    approve = await client.post(
        f"/api/v1/patients/{patient_id}/documents/{doc_id}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 200, approve.text
    return approve.json()["merged"]


async def _check(client, patient_id: str, drug: str) -> dict:
    resp = await client.post(
        f"/api/v1/patients/{patient_id}/drug-safety/check", json={"drug_name": drug}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_type(safety: dict, check_type: str) -> list[dict]:
    return [f for f in safety["flags"] if f["check_type"] == check_type]


# ------------------------------------------------------------------ accumulating record


@pytest.mark.asyncio
async def test_the_safety_verdict_tracks_the_record_as_visits_accumulate(auth_client):
    """The same proposed drug goes clean -> interaction -> hard block as the record grows.

    This is the property that makes the check trustworthy: it reads the patient's state at
    the moment of asking, so a document filed after an earlier "no flags" answer changes the
    next answer. A check that cached, or that only read the document it was asked about,
    would pass every single-step test and fail this one.
    """
    pid = (await create_patient(auth_client))["id"]

    # Visit 1 — nothing on file. Proposing aspirin is unremarkable.
    first = await _check(auth_client, pid, "Aspirin")
    assert first["flags"] == []
    assert first["checked_against"]["current_medications"] == 0

    # Visit 2 — a warfarin prescription is filed.
    await _ingest(auth_client, pid, _document(medications=("Warfarin 5mg OD",)), "visit2.pdf")
    second = await _check(auth_client, pid, "Aspirin")
    interactions = _by_type(second, "drug_interaction")
    assert len(interactions) == 1, second["flags"]
    assert interactions[0]["severity"] == "critical"  # 'major' rule -> critical, not a block
    assert second["is_hard_block"] is False

    # Visit 3 — an aspirin allergy is documented. The same question now has a different answer.
    await _ingest(auth_client, pid, _document(allergies=("Aspirin - urticaria",)), "visit3.pdf")
    third = await _check(auth_client, pid, "Aspirin")
    assert third["is_hard_block"] is True
    assert any(f["is_hard_block"] for f in _by_type(third, "allergy_conflict"))
    # The interaction did not disappear when the more severe finding arrived — conservative
    # output wins, but it wins by adding, not by replacing.
    assert _by_type(third, "drug_interaction"), third["flags"]


@pytest.mark.asyncio
async def test_deteriorating_renal_function_turns_an_accepted_drug_into_a_hard_block(auth_client):
    """Labs filed at a later visit drive eGFR, which drives the renal contraindication.

    Metformin is the case that matters clinically in India: routine at normal renal function,
    contraindicated once eGFR falls below 30. Nothing about the *drug* changed between these
    two checks — only a creatinine result did.
    """
    pid = (await create_patient(auth_client))["id"]

    await _ingest(
        auth_client,
        pid,
        _document(labs=("Creatinine: 1.0 mg/dL (0.6-1.2)",)),
        "labs-baseline.pdf",
    )
    baseline = await _check(auth_client, pid, "Metformin")
    assert baseline["checked_against"]["egfr_available"] is True
    assert _by_type(baseline, "renal_dose") == [], baseline["flags"]

    await _ingest(
        auth_client,
        pid,
        _document(labs=("Creatinine: 3.0 mg/dL (0.6-1.2)",)),
        "labs-followup.pdf",
    )
    deteriorated = await _check(auth_client, pid, "Metformin")
    renal = _by_type(deteriorated, "renal_dose")
    assert renal, deteriorated["flags"]
    assert renal[0]["is_hard_block"] is True
    assert deteriorated["is_hard_block"] is True


@pytest.mark.asyncio
async def test_the_order_documents_arrive_in_does_not_change_the_verdict(auth_client):
    """Two patients, same three documents, opposite filing order — identical safety answer.

    Paper histories arrive out of order all the time (a lab report posted weeks after the
    prescription that prompted it). If ingestion order leaked into the record, the same
    patient would get different answers depending on clerical accident.
    """
    docs = {
        "rx": _document(medications=("Warfarin 5mg OD",)),
        "labs": _document(labs=("Creatinine: 3.0 mg/dL (0.6-1.2)",)),
        "dx": _document(conditions=("Type 2 Diabetes Mellitus",)),
    }

    forward = (await create_patient(auth_client, full_name="Order Forward"))["id"]
    for key in ("rx", "labs", "dx"):
        await _ingest(auth_client, forward, docs[key], f"{key}.pdf")

    reverse = (await create_patient(auth_client, full_name="Order Reverse"))["id"]
    for key in ("dx", "labs", "rx"):
        await _ingest(auth_client, reverse, docs[key], f"{key}.pdf")

    def _comparable(safety: dict) -> list[tuple]:
        return sorted((f["check_type"], f["severity"], f["is_hard_block"]) for f in safety["flags"])

    for drug in ("Aspirin", "Metformin"):
        a = await _check(auth_client, forward, drug)
        b = await _check(auth_client, reverse, drug)
        assert _comparable(a) == _comparable(b), f"{drug} verdict depended on filing order"
        assert a["is_hard_block"] == b["is_hard_block"]
        assert a["checked_against"] == b["checked_against"]


@pytest.mark.asyncio
async def test_a_full_visit_chain_leaves_every_step_in_the_record_and_the_audit(auth_client):
    """intake document -> labs -> prescription -> safety check -> override, all persisted.

    Asserts the steps compose: the record read after the chain contains every entity the
    chain filed, and the audit trail contains an entry for every step in the order they
    happened.
    """
    pid = (await create_patient(auth_client))["id"]

    await _ingest(
        auth_client,
        pid,
        _document(conditions=("Type 2 Diabetes Mellitus",), allergies=("Ibuprofen - hives",)),
        "intake.pdf",
    )
    await _ingest(auth_client, pid, _document(labs=("HbA1c: 9.2 % (4.0-5.6)",)), "labs.pdf")
    await _ingest(auth_client, pid, _document(medications=("Glycomet 500mg BD",)), "rx.pdf")

    record = (await auth_client.get(f"/api/v1/patients/{pid}/record")).json()
    assert "Metformin" in {m["generic_name"] for m in record["medications"]}
    assert "Ibuprofen" in {a["allergen_name"] for a in record["allergies"]}
    assert "Type 2 Diabetes Mellitus" in {c["condition_name"] for c in record["conditions"]}
    assert "HbA1c" in {lab["marker_name"] for lab in record["lab_results"]}

    blocked = await _check(auth_client, pid, "Brufen")
    assert blocked["is_hard_block"] is True
    block_id = next(f["id"] for f in blocked["flags"] if f["is_hard_block"])

    override = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/override",
        json={
            "drug_safety_check_id": block_id,
            "reasoning": "Discussed with patient; prior reaction was mild and non-IgE mediated.",
        },
    )
    assert override.status_code == 201, override.text

    audit = (await auth_client.get(f"/api/v1/patients/{pid}/audit", params={"limit": 200})).json()
    actions = [i["action"] for i in reversed(audit["items"])]
    for step in (
        "patient_created",
        "extraction_approved",
        "drug_safety_check",
        "drug_safety_hard_block_overridden",
    ):
        assert step in actions, f"{step} missing from {actions}"
    assert actions.index("drug_safety_check") < actions.index("drug_safety_hard_block_overridden")
    # The override is a separate record; the block it documents is untouched.
    assert (await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/flags")).status_code == 200
    still_blocked = await _check(auth_client, pid, "Brufen")
    assert still_blocked["is_hard_block"] is True, "an override must not disarm the rule"


# ------------------------------------------------------- two clinicians, the same patient


@pytest.mark.asyncio
async def test_a_colleagues_prescription_is_visible_to_the_next_check_immediately(
    auth_client, colleague_client
):
    """One clinician files warfarin; the other's aspirin check must already know about it.

    This is the read-your-colleague's-writes case that makes a shared chart safe. A per-client
    cache of the patient's medications — an obvious optimisation — would break exactly here.
    """
    pid = (await create_patient(auth_client))["id"]

    before = await _check(colleague_client, pid, "Aspirin")
    assert _by_type(before, "drug_interaction") == []

    await _ingest(auth_client, pid, _document(medications=("Warfarin 5mg OD",)), "rx.pdf")

    after = await _check(colleague_client, pid, "Aspirin")
    assert _by_type(after, "drug_interaction"), (
        "the second clinician's check did not see the first clinician's prescription"
    )


@pytest.mark.asyncio
async def test_both_clinicians_checks_are_recorded_separately(auth_client, colleague_client):
    """Same patient, same drug, both clinicians: one verdict, two persisted checks, two audits.

    Safety checks are append-only — every ask is evidence of what the clinician was shown at
    that moment — so neither request may overwrite the other's record of it.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, _document(medications=("Warfarin 5mg OD",)), "rx.pdf")

    first = await _check(auth_client, pid, "Aspirin")
    second = await _check(colleague_client, pid, "Aspirin")

    assert first["is_hard_block"] == second["is_hard_block"]
    assert {f["check_type"] for f in first["flags"]} == {f["check_type"] for f in second["flags"]}
    # Distinct persisted rows: the two checks did not collapse into one.
    assert {f["id"] for f in first["flags"]}.isdisjoint({f["id"] for f in second["flags"]})

    audit = (await auth_client.get(f"/api/v1/patients/{pid}/audit", params={"limit": 200})).json()
    checks = [i for i in audit["items"] if i["action"] == "drug_safety_check"]
    assert len(checks) == 2, f"expected both checks in the audit trail, got {len(checks)}"


@pytest.mark.asyncio
async def test_two_clinicians_reading_the_same_chart_see_identical_content(
    auth_client, colleague_client
):
    """Neither clinician gets a personalised or partially-filtered view of a shared patient.

    Deliberately sequential. Requests cannot be issued truly in parallel here: every request
    — reads included, via ``patient_record_viewed`` — appends to the global hash chain, and
    appends are serialised in production by a PostgreSQL transaction advisory lock that the
    SQLite test bind has no equivalent for. The lock itself, and the fact that it is taken
    before the chain tail is read, are asserted directly in ``test_infra_coverage``.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(
        auth_client,
        pid,
        _document(medications=("Warfarin 5mg OD",), allergies=("Penicillin - rash",)),
        "rx.pdf",
    )

    mine = await auth_client.get(f"/api/v1/patients/{pid}/record")
    theirs = await colleague_client.get(f"/api/v1/patients/{pid}/record")
    my_flags = await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/flags")
    their_flags = await colleague_client.get(f"/api/v1/patients/{pid}/drug-safety/flags")

    assert mine.status_code == theirs.status_code == 200
    assert mine.json() == theirs.json()
    assert my_flags.json() == their_flags.json()


@pytest.mark.asyncio
async def test_both_clinicians_overrides_of_one_hard_block_survive(auth_client, colleague_client):
    """Two documented overrides of the same block are two records, not a last-write-wins field.

    Who accepted a hard block, and why, is exactly the thing a later reviewer needs. Storing
    it as state on the check would lose the first clinician's reasoning.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, _document(allergies=("Ibuprofen - hives",)), "allergy.pdf")

    blocked = await _check(auth_client, pid, "Brufen")
    block_id = next(f["id"] for f in blocked["flags"] if f["is_hard_block"])

    reasons = [
        "Reviewed with patient: prior reaction was mild flushing, not urticaria.",
        "Second opinion — proceeding under observation with rescue medication on hand.",
    ]
    for client, reason in ((auth_client, reasons[0]), (colleague_client, reasons[1])):
        resp = await client.post(
            f"/api/v1/patients/{pid}/drug-safety/override",
            json={"drug_safety_check_id": block_id, "reasoning": reason},
        )
        assert resp.status_code == 201, resp.text

    listed = (await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/overrides")).json()
    for_this_block = [o for o in listed if o["drug_safety_check_id"] == block_id]
    assert len(for_this_block) == 2
    assert {o["reasoning"] for o in for_this_block} == set(reasons)


@pytest.mark.asyncio
async def test_interleaved_edits_by_two_clinicians_both_reach_the_record(
    auth_client, colleague_client
):
    """Alternating ingestion from two clinicians accumulates rather than clobbering."""
    pid = (await create_patient(auth_client))["id"]

    await _ingest(auth_client, pid, _document(medications=("Warfarin 5mg OD",)), "a1.pdf")
    await _ingest(colleague_client, pid, _document(conditions=("Hypertension",)), "b1.pdf")
    await _ingest(auth_client, pid, _document(labs=("HbA1c: 9.2 % (4.0-5.6)",)), "a2.pdf")
    await _ingest(colleague_client, pid, _document(allergies=("Penicillin - rash",)), "b2.pdf")

    record = (await auth_client.get(f"/api/v1/patients/{pid}/record")).json()
    assert "Warfarin" in {m["generic_name"] for m in record["medications"]}
    assert "Hypertension" in {c["condition_name"] for c in record["conditions"]}
    assert "HbA1c" in {lab["marker_name"] for lab in record["lab_results"]}
    assert "Penicillin" in {a["allergen_name"] for a in record["allergies"]}

    documents = (await colleague_client.get(f"/api/v1/patients/{pid}/documents")).json()
    assert len(documents) == 4, "a document filed by one clinician is missing for the other"


@pytest.mark.asyncio
async def test_a_third_account_stays_invisible_throughout_a_shared_journey(
    auth_client, colleague_client, second_auth_client
):
    """Sharing a chart between two clinicians on one account must not widen it to a third."""
    pid = (await create_patient(auth_client))["id"]
    await _ingest(colleague_client, pid, _document(medications=("Warfarin 5mg OD",)), "rx.pdf")

    for method, path, kwargs in (
        ("get", f"/api/v1/patients/{pid}", {}),
        ("get", f"/api/v1/patients/{pid}/record", {}),
        ("get", f"/api/v1/patients/{pid}/documents", {}),
        ("get", f"/api/v1/patients/{pid}/drug-safety/flags", {}),
        ("get", f"/api/v1/patients/{pid}/audit", {}),
        (
            "post",
            f"/api/v1/patients/{pid}/drug-safety/check",
            {"json": {"drug_name": "Aspirin"}},
        ),
    ):
        resp = await getattr(second_auth_client, method)(path, **kwargs)
        assert resp.status_code == 404, f"{method.upper()} {path} leaked to another account"


# ------------------------------------------------------------ interaction edge cases


@pytest.mark.asyncio
async def test_one_proposal_against_three_interacting_drugs_raises_three_graded_flags(
    auth_client,
):
    """Warfarin against aspirin, diclofenac and ciprofloxacin: three rules, three severities.

    Fan-out is where a "first match wins" implementation quietly hides real risk, and where
    a shared severity would flatten a moderate CYP interaction into the same alert as two
    major bleeding risks.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(
        auth_client,
        pid,
        _document(
            medications=("Ecosprin 75mg OD", "Voveran 50mg BD", "Ciplox 500mg BD"),
        ),
        "rx.pdf",
    )
    assert (await _check(auth_client, pid, "Aspirin"))["checked_against"][
        "current_medications"
    ] == 3

    safety = await _check(auth_client, pid, "Warfarin")
    interactions = _by_type(safety, "drug_interaction")
    assert len(interactions) == 3, [f["summary"] for f in interactions]

    by_partner = {f["details"]["interacting_drug"]: f for f in interactions}
    assert by_partner["Aspirin"]["severity"] == "critical"  # major
    assert by_partner["Diclofenac"]["severity"] == "critical"  # major
    assert by_partner["Ciprofloxacin"]["severity"] == "warning"  # moderate
    assert not any(f["is_hard_block"] for f in interactions)
    assert safety["is_hard_block"] is False


@pytest.mark.asyncio
async def test_a_contraindicated_pair_hard_blocks_even_among_milder_flags(auth_client):
    """One 'contraindicated' rule outranks any number of warnings (conservative wins).

    The patient is already on sildenafil *and* a nitrate, so re-ordering sildenafil raises a
    duplicate-therapy warning alongside the contraindicated interaction. The response must
    report the block, and the block must still be individually identifiable in the flag list
    rather than averaged into the milder ones.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(
        auth_client,
        pid,
        _document(medications=("Monotrate 20mg OD", "Manforce 50mg SOS")),
        "rx.pdf",
    )

    safety = await _check(auth_client, pid, "Sildenafil")
    assert safety["is_hard_block"] is True

    blocking = [f for f in safety["flags"] if f["is_hard_block"]]
    assert len(blocking) == 1
    assert blocking[0]["check_type"] == "drug_interaction"
    assert blocking[0]["severity"] == "hard_block"
    assert blocking[0]["details"]["interacting_drug"] == "Isosorbide mononitrate"

    milder = [f for f in safety["flags"] if not f["is_hard_block"]]
    assert any(f["check_type"] == "duplicate_therapy" for f in milder), safety["flags"]


@pytest.mark.asyncio
async def test_an_interaction_fires_whichever_drug_is_the_proposed_one(auth_client):
    """Interaction pairs are stored alphabetically; neither direction may be a blind spot."""
    on_warfarin = (await create_patient(auth_client, full_name="On Warfarin"))["id"]
    await _ingest(auth_client, on_warfarin, _document(medications=("Warf 5mg OD",)), "rx.pdf")

    on_aspirin = (await create_patient(auth_client, full_name="On Aspirin"))["id"]
    await _ingest(auth_client, on_aspirin, _document(medications=("Ecosprin 75mg OD",)), "rx.pdf")

    proposing_aspirin = _by_type(
        await _check(auth_client, on_warfarin, "Aspirin"), "drug_interaction"
    )
    proposing_warfarin = _by_type(
        await _check(auth_client, on_aspirin, "Warfarin"), "drug_interaction"
    )

    assert len(proposing_aspirin) == 1
    assert len(proposing_warfarin) == 1
    assert proposing_aspirin[0]["severity"] == proposing_warfarin[0]["severity"]
    assert (
        proposing_aspirin[0]["drug_interaction_id"] == proposing_warfarin[0]["drug_interaction_id"]
    )


@pytest.mark.asyncio
async def test_a_patient_on_both_halves_of_a_pair_sees_it_from_each_drugs_side(auth_client):
    """The active-flags board is per-drug, so one interaction appears on both drugs' cards.

    That is the intended shape — a clinician reviewing warfarin needs to see aspirin listed
    against it, and vice versa — but it means the *same* rule is reported twice. Pinned here
    so a future de-duplication is a deliberate decision rather than a silent regression.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(
        auth_client,
        pid,
        _document(medications=("Warf 5mg OD", "Ecosprin 75mg OD")),
        "rx.pdf",
    )

    board = (await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/flags")).json()
    interactions = [f for f in board["flags"] if f["check_type"] == "drug_interaction"]
    assert len(interactions) == 2
    assert {f["details"]["proposed_drug"] for f in interactions} == {"Warfarin", "Aspirin"}
    assert len({f["drug_interaction_id"] for f in interactions}) == 1, "not the same rule twice"


@pytest.mark.asyncio
async def test_a_drug_does_not_interact_with_itself_on_the_active_flags_board(auth_client):
    """Re-ordering the same product is a duplicate, never a self-interaction."""
    pid = (await create_patient(auth_client))["id"]
    await _ingest(
        auth_client,
        pid,
        _document(medications=("Warf 5mg OD", "Warfarin 5mg OD")),
        "rx.pdf",
    )

    board = (await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/flags")).json()
    self_pairs = [
        f
        for f in board["flags"]
        if f["check_type"] == "drug_interaction"
        and f["details"]["proposed_drug"] == f["details"]["interacting_drug"]
    ]
    assert self_pairs == [], self_pairs


@pytest.mark.asyncio
async def test_a_deactivated_interaction_rule_stops_firing(auth_client, db):
    """Retiring a rule must take it out of circulation without deleting the audit history."""
    from sqlalchemy import select

    from app.models.drug_vocabulary import DrugInteraction

    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, _document(medications=("Warf 5mg OD",)), "rx.pdf")
    assert _by_type(await _check(auth_client, pid, "Aspirin"), "drug_interaction")

    rule = (
        await db.execute(
            select(DrugInteraction).where(
                DrugInteraction.drug_a_reference_id == "ASP-75",
                DrugInteraction.drug_b_reference_id == "WARF-5",
            )
        )
    ).scalar_one()
    rule.is_active = False
    await db.commit()

    assert _by_type(await _check(auth_client, pid, "Aspirin"), "drug_interaction") == []
