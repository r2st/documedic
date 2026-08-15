"""Decision support driven by an ingested chart, across several patients in one clinic.

``test_reasoning`` proves the eight-agent pipeline runs and that the verifier gates its output.
It does that against patients whose charts were written directly into the database, and one
patient at a time. Neither is how the product is used, and both hide a class of failure that
only appears in between:

* the CDS is supposed to reason about *the chart*, and the chart is built by uploading and
  approving documents. Between the scan and the agents sit extraction, the drug vocabulary,
  the graph merge and the derived-marker computation — so "the interaction is in the reference
  data" and "the clinician is told about it" are separated by the whole ingestion pipeline.
* a clinic runs several patients at once. The failure that matters there is not access control
  (a different account is already refused everywhere) but *attribution* inside one account:
  session A answering with patient B's medications. That is a wrong-patient error, the kind of
  mistake a clinician has no way to catch from the screen, and nothing above tests for it.

Everything here runs on the deterministic offline path — no LLM — so the drug-safety half is
exactly what Critical Safety Rule #8 requires to keep working when the network is down. That
also means every run is marked degraded and floors at flag-for-review, which is why the
assertions below are about *which findings* appear rather than about the tier.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.allergy import Allergy
from app.models.audit_log import AuditLog
from app.models.clinical_suggestion import ClinicalSuggestion
from app.models.medication_event import MedicationEvent
from tests.conftest import create_patient
from tests.test_reasoning import _complete_intake, _start


def _prescription(*brands: str) -> bytes:
    """A scanned prescription listing `brands` one per line, as a paper Rx is written."""
    return b"%PDF-1.4\nMEDICATIONS:\n" + b"".join(f"{brand}\n".encode() for brand in brands)


async def _ingest(client, patient_id: str, data: bytes, name: str = "rx.pdf") -> dict:
    """Upload and approve — the only route by which anything reaches the chart."""
    upload = await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, data, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    approve = await client.post(
        f"/api/v1/patients/{patient_id}/documents/{upload.json()['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 200, approve.text
    return approve.json()["merged"]


async def _run_cds(client, patient_id: str, complaint: str) -> dict:
    """Start a session, answer the triage questions, and run the pipeline to completion."""
    state = await _start(client, patient_id, complaint)
    session_id = state["session"]["id"]
    await _complete_intake(client, session_id)
    resp = await client.post(f"/api/v1/reasoning/{session_id}/run")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _interaction_pairs(result: dict) -> set[frozenset[str]]:
    """The unordered drug pairs the run flagged as interacting."""
    return {
        frozenset((flag["details"]["proposed_drug"], flag["details"]["interacting_drug"]))
        for flag in result["case_state"]["drug_safety_flags"]
        if flag["check_type"] == "drug_interaction"
    }


# ------------------------------------------------------- the chart actually reaches the agents


@pytest.mark.asyncio
async def test_an_interaction_only_a_scan_knew_about_reaches_the_clinician(auth_client):
    """Two brand names on a photographed prescription, and the CDS names the hazard.

    Nothing in this test tells the system the patient takes warfarin. It uploads a scan reading
    "Warf 5mg" and "Brufen 400mg" — Indian brands, as they are actually written — and the answer
    has to come out the far end of extraction, brand-to-generic resolution through
    DrugVocabulary, the graph merge, and the interaction rules. A break anywhere along that
    chain shows up as a clinician who was never warned.
    """
    patient = await create_patient(auth_client, full_name="Scanned Rx")
    await _ingest(auth_client, patient["id"], _prescription("Warf 5mg", "Brufen 400mg"))

    result = await _run_cds(auth_client, patient["id"], "swelling of the legs")

    assert frozenset(("Warfarin", "Ibuprofen")) in _interaction_pairs(result), (
        "the warfarin/NSAID bleeding interaction did not survive the trip from the scan to the "
        f"reasoning case: {result['case_state']['drug_safety_flags']}"
    )


@pytest.mark.asyncio
async def test_a_drug_added_at_a_later_visit_changes_the_next_sessions_answer(auth_client):
    """The longitudinal claim, end to end: today's advice reflects today's chart.

    A CDS that answered from a snapshot taken at first contact would keep reassuring a clinician
    about a patient who has since been started on an interacting drug — and would do it silently,
    since the earlier session looks exactly as convincing as the later one.
    """
    patient = await create_patient(auth_client, full_name="Two Visits")
    pid = patient["id"]
    await _ingest(auth_client, pid, _prescription("Warf 5mg"), name="visit-one.pdf")

    first = await _run_cds(auth_client, pid, "routine review")
    assert _interaction_pairs(first) == set(), (
        f"warfarin alone must not interact with anything: {_interaction_pairs(first)}"
    )

    await _ingest(auth_client, pid, _prescription("Brufen 400mg"), name="visit-two.pdf")
    second = await _run_cds(auth_client, pid, "knee pain")

    assert frozenset(("Warfarin", "Ibuprofen")) in _interaction_pairs(second), (
        "the drug added at the second visit was not considered against the first visit's chart"
    )


@pytest.mark.asyncio
async def test_the_earlier_sessions_record_is_not_rewritten_by_the_later_chart(auth_client, db):
    """What the clinician was told at visit one stays what they were told (Safety Rule #7).

    The audit value of a suggestion is that it records the advice *as given*, against the chart
    as it then stood. Back-filling the earlier session with the later visit's finding would make
    the trail claim a warning was shown that nobody ever saw.
    """
    patient = await create_patient(auth_client, full_name="Immutable History")
    pid = patient["id"]
    await _ingest(auth_client, pid, _prescription("Warf 5mg"), name="visit-one.pdf")
    first = await _run_cds(auth_client, pid, "routine review")
    first_session = first["session"]["id"]
    before = {(s["id"], s["title"], s["autonomy_tier"]) for s in first["suggestions"]}

    await _ingest(auth_client, pid, _prescription("Brufen 400mg"), name="visit-two.pdf")
    await _run_cds(auth_client, pid, "knee pain")

    resp = await auth_client.get(f"/api/v1/reasoning/{first_session}/suggestions")
    assert resp.status_code == 200, resp.text
    assert {(s["id"], s["title"], s["autonomy_tier"]) for s in resp.json()} == before

    # Again at the storage layer, because the list endpoint reads through a filter and a bug that
    # rewrote rows in place could be hidden by the same filter that scopes them.
    stored = (
        await db.execute(
            select(
                ClinicalSuggestion.id,
                ClinicalSuggestion.title,
                ClinicalSuggestion.autonomy_tier,
            ).where(ClinicalSuggestion.session_id == uuid.UUID(first_session))
        )
    ).all()
    assert stored, "the first session logged no suggestions at all"
    assert {(str(i), t, tier) for i, t, tier in stored} == before


# ---------------------------------------------------------------- several patients at once


@pytest.mark.asyncio
async def test_each_sessions_safety_flags_come_only_from_its_own_patients_chart(auth_client):
    """Three charts open in one clinic, sessions interleaved. No finding may cross over.

    This is a wrong-patient error and it is invisible from the screen: a clinician reading
    "major interaction: warfarin and ibuprofen" on the right patient's page has no way to tell
    the drugs came off somebody else's chart. Deliberately interleaved — every session is started
    before any is run — because a bug that scoped by "the most recent session" would still pass
    if each patient were driven to completion in turn.
    """
    charts = {
        "Warfarin patient": ("Warf 5mg", "Brufen 400mg"),
        "Nitrate patient": ("Monotrate 20mg", "Manforce 50mg"),
        "Simple patient": ("Dolo 650mg",),
    }
    sessions: dict[str, str] = {}
    for name, brands in charts.items():
        patient = await create_patient(auth_client, full_name=name)
        await _ingest(auth_client, patient["id"], _prescription(*brands), name=f"{name}.pdf")
        state = await _start(auth_client, patient["id"], "general review")
        sessions[name] = state["session"]["id"]

    results: dict[str, dict] = {}
    for name, session_id in sessions.items():
        await _complete_intake(auth_client, session_id)
        resp = await auth_client.post(f"/api/v1/reasoning/{session_id}/run")
        assert resp.status_code == 200, resp.text
        results[name] = resp.json()

    assert frozenset(("Warfarin", "Ibuprofen")) in _interaction_pairs(results["Warfarin patient"])
    assert frozenset(("Sildenafil", "Nitroglycerin")) not in _interaction_pairs(
        results["Warfarin patient"]
    ), "the nitrate patient's contraindication was reported on the warfarin patient's session"

    nitrate_drugs = {
        drug for pair in _interaction_pairs(results["Nitrate patient"]) for drug in pair
    }
    assert "Warfarin" not in nitrate_drugs and "Ibuprofen" not in nitrate_drugs, (
        f"the warfarin patient's drugs appeared on the nitrate patient's session: {nitrate_drugs}"
    )

    # Nothing from the other two charts. Not asserted as an empty list: this patient's own
    # chart says "Dolo", which this fifty-drug vocabulary has not been seeded with, so the run
    # correctly reports that one medication could not be evaluated. That note *is* about this
    # patient — it quotes the entry off this chart — and it is the answer
    # `test_reasoning_chart_completeness` exists to keep. What must not appear is any finding
    # naming a drug this patient is not on.
    simple_flags = results["Simple patient"]["case_state"]["drug_safety_flags"]
    others = {"Warfarin", "Ibuprofen", "Sildenafil", "Nitroglycerin"}
    for flag in simple_flags:
        found = {drug for drug in others if drug.lower() in flag["summary"].lower()}
        assert not found, (
            f"a patient on one analgesic was given somebody else's safety flags: {found}"
        )
    assert [f["check_type"] for f in simple_flags] == ["unevaluated_medication"]
    assert simple_flags[0]["details"]["unresolved_medications"] == ["Dolo"]


@pytest.mark.asyncio
async def test_every_suggestion_is_attributed_to_the_patient_its_session_is_for(auth_client):
    """The attribution has to hold on each logged row, not only on the response envelope.

    The suggestion rows outlive the response: they are what the audit trail, the chart's history
    and any later export read. One carrying the wrong patient id is a clinical record filed on
    the wrong person.
    """
    first = await create_patient(auth_client, full_name="First Chart")
    second = await create_patient(auth_client, full_name="Second Chart")
    await _ingest(auth_client, first["id"], _prescription("Warf 5mg", "Brufen 400mg"))
    await _ingest(auth_client, second["id"], _prescription("Dolo 650mg"), name="second.pdf")

    for patient in (first, second):
        result = await _run_cds(auth_client, patient["id"], "chest discomfort")
        attributed = {s["patient_id"] for s in result["suggestions"]}
        assert attributed == {patient["id"]}, (
            f"suggestions for {patient['full_name']} were filed against {attributed}"
        )


@pytest.mark.asyncio
async def test_one_sessions_suggestion_list_never_includes_anothers(auth_client):
    """The list endpoint is what the Reasoning Theatre renders, so it is scoped separately."""
    first = await create_patient(auth_client, full_name="Session One")
    second = await create_patient(auth_client, full_name="Session Two")
    first_result = await _run_cds(auth_client, first["id"], "fever and cough")
    second_result = await _run_cds(auth_client, second["id"], "headache")

    for result in (first_result, second_result):
        session_id = result["session"]["id"]
        listed = (await auth_client.get(f"/api/v1/reasoning/{session_id}/suggestions")).json()
        assert {s["session_id"] for s in listed} == {session_id}

    first_ids = {s["id"] for s in first_result["suggestions"]}
    second_ids = {s["id"] for s in second_result["suggestions"]}
    assert not first_ids & second_ids, "two sessions returned the same suggestion rows"


@pytest.mark.asyncio
async def test_a_hard_block_on_one_chart_stays_off_the_other_patients_case(auth_client, db):
    """A hard block is the strongest thing the system says. It must be said about one patient.

    Shown on the wrong chart it reads as an instruction to withhold a drug from someone with no
    such allergy; missing from the right one it is a documented allergy the clinician was not
    reminded of. Both directions are asserted.
    """
    allergic = await create_patient(auth_client, full_name="Aspirin Allergic")
    tolerant = await create_patient(auth_client, full_name="No Known Allergies")
    pid = uuid.UUID(allergic["id"])
    db.add(
        MedicationEvent(
            patient_id=pid,
            generic_name="Aspirin",
            dose="75",
            event_type="continue",
            is_current=True,
            clinician_confirmed=True,
        )
    )
    db.add(
        Allergy(
            patient_id=pid,
            allergen_name="Aspirin",
            allergen_type="drug",
            status="active",
            clinician_confirmed=True,
        )
    )
    await db.commit()

    blocked = await _run_cds(auth_client, allergic["id"], "chest discomfort")
    clear = await _run_cds(auth_client, tolerant["id"], "chest discomfort")

    assert [s for s in blocked["suggestions"] if s["is_hard_block"]], (
        "a documented allergy to a current medication raised no hard block"
    )
    assert not [s for s in clear["suggestions"] if s["is_hard_block"]], (
        "the other patient's allergy hard block appeared on a chart with no allergies recorded"
    )
    assert clear["case_state"]["drug_safety_flags"] == []


@pytest.mark.asyncio
async def test_the_audit_trail_names_the_right_patient_for_each_run(auth_client, db):
    """Two runs, two patients, and the compliance record has to be able to tell them apart."""
    first = await create_patient(auth_client, full_name="Audited One")
    second = await create_patient(auth_client, full_name="Audited Two")
    first_result = await _run_cds(auth_client, first["id"], "fever")
    second_result = await _run_cds(auth_client, second["id"], "cough")

    for patient, result in ((first, first_result), (second, second_result)):
        entries = (
            (
                await db.execute(
                    select(AuditLog.patient_id).where(
                        AuditLog.entity_id == uuid.UUID(result["session"]["id"])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert entries, f"no audit entries reference {patient['full_name']}'s session"
        assert set(entries) == {uuid.UUID(patient["id"])}, (
            f"{patient['full_name']}'s session was audited against {set(entries)}"
        )


# ------------------------------------------------------------------ a chart with nothing on it


@pytest.mark.asyncio
async def test_a_chart_with_nothing_on_it_produces_no_invented_findings(auth_client):
    """The Triage Agent's constraint, seen from the output side: no data means no claims.

    An empty chart is the state every patient starts in, and it is the one where a confident
    answer is most dangerous — there is nothing on screen for the clinician to check it against.
    """
    patient = await create_patient(auth_client, full_name="Nothing Recorded")

    result = await _run_cds(auth_client, patient["id"], "generally unwell")

    assert result["case_state"]["drug_safety_flags"] == []
    assert result["case_state"]["hard_blocks"] == []
    for suggestion in result["suggestions"]:
        assert not suggestion["is_hard_block"]
        assert suggestion["confidence_band"] != "high", (
            f"a chart with no data yielded a high-confidence {suggestion['output_type']}: "
            f"{suggestion['title']}"
        )
    assert result["session"]["autonomy_tier"] != "informational", (
        "a case the engine could say nothing about was presented as settled reference material"
    )
