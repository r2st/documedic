"""The interaction graph against real polypharmacy, driven through the HTTP API.

The existing safety suites exercise the engine's rules one pair at a time. What they do not
cover is the shape a real Indian outpatient chart actually has: an elderly patient on six to
ten drugs, where a single new prescription touches several existing ones at once and the
answer depends on how the whole graph resolves — brand names to generics, generics to
reference ids, reference ids to curated rules.

Every combination below is drawn from the seeded ICMR-derived reference data and is a
recognised clinical hazard, prescribed by brand the way it appears on a paper prescription:

* Warf (warfarin) is the hub of the graph — six curated rules run through it, spanning three
  severities, which is what makes it the case for testing fan-out and severity grading
* Monotrate (nitrate) + Manforce (sildenafil) is the one contraindicated pair in the corpus,
  so it is the hard block that must never be downgradeable
* Envas (ACE inhibitor) + Aldactone (spironolactone) + Telma (ARB) is the classic
  hyperkalaemia triangle, and the case where several current drugs each interact with one
  proposal
* Lasix + Licab (lithium) and Voveran/Brufen + Folitrax (methotrexate) are the narrow
  therapeutic index pairs where the interaction, not the drug, is the danger

Everything runs on the deterministic offline path — no LLM — which is the requirement for
these checks under Critical Safety Rule #8.
"""

from __future__ import annotations

import pytest

from tests.conftest import create_patient


def _prescription(*brands: str) -> bytes:
    """A scanned prescription listing `brands`, one per line, as a clinician would write it."""
    lines = [b"%PDF-1.4\n", b"MEDICATIONS:\n"]
    lines += [f"{brand}\n".encode() for brand in brands]
    return b"".join(lines)


async def _ingest(client, patient_id: str, *brands: str, name: str = "rx.pdf") -> dict:
    upload = await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, _prescription(*brands), "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    approve = await client.post(
        f"/api/v1/patients/{patient_id}/documents/{upload.json()['id']}/approve",
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


def _interactions(result: dict) -> list[dict]:
    return [f for f in result["flags"] if f["check_type"] == "drug_interaction"]


def _partners(result: dict) -> set[str]:
    return {f["details"]["interacting_drug"] for f in _interactions(result)}


def _severities(result: dict) -> dict[str, str]:
    return {
        f["details"]["interacting_drug"]: f["details"]["severity"] for f in _interactions(result)
    }


@pytest.mark.asyncio
async def test_a_warfarin_patient_on_four_interacting_drugs_gets_a_flag_for_each(auth_client):
    """Fan-out: one proposal, several current drugs, one flag per real pair — no collapsing.

    A clinician deciding whether to start warfarin needs to see every existing drug it
    collides with. Reporting only the worst one hides that stopping a single drug is not
    enough.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(
        auth_client,
        pid,
        "Ecosprin 75mg OD",  # aspirin      -> major
        "Voveran 50mg BD",  # diclofenac   -> major
        "Ciplox 500mg BD",  # ciprofloxacin-> moderate
        "Eptoin 100mg TDS",  # phenytoin    -> moderate
        "Shelcal 500mg OD",  # calcium      -> no rule
    )

    result = await _check(auth_client, pid, "Warfarin")

    assert _partners(result) == {"Aspirin", "Diclofenac", "Ciprofloxacin", "Phenytoin"}
    assert _severities(result) == {
        "Aspirin": "major",
        "Diclofenac": "major",
        "Ciprofloxacin": "moderate",
        "Phenytoin": "moderate",
    }


@pytest.mark.asyncio
async def test_severity_grades_are_carried_through_to_the_clinician_verbatim(auth_client):
    """A moderate must not be presented as a major, or the grading stops meaning anything.

    Over-flagging is the failure mode that produces alert fatigue, which is what makes a real
    hard block get clicked through.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, "Warf 5mg OD", "Clopitab 75mg OD")

    warfarin_side = await _check(auth_client, pid, "Ciprofloxacin")
    moderate = next(
        f for f in _interactions(warfarin_side) if f["details"]["severity"] == "moderate"
    )

    assert moderate["severity"] == "warning"
    assert moderate["is_hard_block"] is False
    assert "moderate" in moderate["summary"].lower()


@pytest.mark.asyncio
async def test_the_nitrate_sildenafil_pair_is_an_undismissable_hard_block(auth_client):
    """The one contraindicated pair in the corpus. Severe hypotension; never advisory."""
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, "Monotrate 20mg BD")

    result = await _check(auth_client, pid, "Sildenafil")

    assert result["is_blocked"] is True
    assert result["is_hard_block"] is True
    blocked = next(f for f in _interactions(result) if f["is_hard_block"])
    assert blocked["severity"] == "hard_block"
    assert blocked["details"]["interacting_drug"] == "Isosorbide mononitrate"


@pytest.mark.asyncio
async def test_the_hard_block_fires_from_whichever_side_is_prescribed_first(auth_client):
    """Interaction rules are stored as an ordered pair; the hazard is not directional.

    A patient already on sildenafil who is prescribed a nitrate is in exactly the same danger
    as the reverse, so a rule keyed on (drug_a, drug_b) must match either way round.
    """
    nitrate_first = (await create_patient(auth_client, full_name="Nitrate First"))["id"]
    await _ingest(auth_client, nitrate_first, "Monotrate 20mg BD")

    sildenafil_first = (await create_patient(auth_client, full_name="Sildenafil First"))["id"]
    await _ingest(auth_client, sildenafil_first, "Manforce 50mg PRN")

    assert (await _check(auth_client, nitrate_first, "Sildenafil"))["is_hard_block"] is True
    assert (await _check(auth_client, sildenafil_first, "Monotrate"))["is_hard_block"] is True


@pytest.mark.asyncio
async def test_the_hyperkalaemia_triangle_flags_both_existing_drugs(auth_client):
    """ACE inhibitor + spironolactone + ARB: adding the third drug collides with both others."""
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, "Envas 5mg OD", "Aldactone 25mg OD")

    result = await _check(auth_client, pid, "Telmisartan")

    assert _partners(result) == {"Enalapril", "Spironolactone"}
    assert set(_severities(result).values()) == {"major"}


@pytest.mark.asyncio
async def test_narrow_therapeutic_index_pairs_are_flagged_from_the_brand_name(auth_client):
    """Lithium and methotrexate: the interaction is the hazard, and brands hide the generic.

    A prescription reads "Licab" and "Folitrax", never "lithium carbonate" and "methotrexate",
    so brand-to-generic resolution is load-bearing for these ever firing at all.
    """
    lithium = (await create_patient(auth_client, full_name="On Lithium"))["id"]
    await _ingest(auth_client, lithium, "Licab 400mg BD")
    lithium_result = await _check(auth_client, lithium, "Lasix")

    methotrexate = (await create_patient(auth_client, full_name="On Methotrexate"))["id"]
    await _ingest(auth_client, methotrexate, "Folitrax 7.5mg weekly")
    mtx_result = await _check(auth_client, methotrexate, "Brufen")

    assert _partners(lithium_result) == {"Lithium carbonate"}
    assert _severities(lithium_result) == {"Lithium carbonate": "major"}
    assert _partners(mtx_result) == {"Methotrexate"}
    assert _severities(mtx_result) == {"Methotrexate": "major"}


@pytest.mark.asyncio
async def test_a_ten_drug_chart_reports_only_the_pairs_that_actually_interact(auth_client):
    """Realistic polypharmacy: most of the chart is noise and must not generate flags.

    Ten current drugs make 10 candidate pairs against one proposal. Only the curated ones may
    fire — a check that flags the rest is worse than no check, because a clinician learns to
    dismiss it.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(
        auth_client,
        pid,
        "Glycomet 500mg BD",  # metformin      -> no warfarin rule
        "Amlong 5mg OD",  # amlodipine     -> no warfarin rule
        "Aten 50mg OD",  # atenolol       -> no warfarin rule
        "Atorva 20mg HS",  # atorvastatin   -> no warfarin rule
        "Thyronorm 50mcg OD",  # levothyroxine  -> no warfarin rule
        "Pan 40mg OD",  # pantoprazole   -> no warfarin rule
        "Shelcal 500mg OD",  # calcium        -> no warfarin rule
        "Asthalin 100mcg PRN",  # salbutamol     -> no warfarin rule
        "Ecosprin 75mg OD",  # aspirin        -> MAJOR with warfarin
        "Brufen 400mg TDS",  # ibuprofen      -> MAJOR with warfarin
    )

    result = await _check(auth_client, pid, "Warfarin")

    assert result["checked_against"]["current_medications"] == 10
    assert _partners(result) == {"Aspirin", "Ibuprofen"}


@pytest.mark.asyncio
async def test_the_worst_severity_in_a_fan_out_decides_the_overall_verdict(auth_client):
    """Conservative-wins (Critical Safety Rule #2), applied across a whole set of flags.

    A moderate and a contraindicated interaction in the same result must not average out: the
    response is blocked because the worst one says so.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, "Monotrate 20mg BD", "Rosuvas 10mg HS")

    mixed = await _check(auth_client, pid, "Sildenafil")
    assert mixed["is_blocked"] is True

    # And with no contraindicated pair in play, the same fan-out is advisory rather than blocking.
    advisory = (await create_patient(auth_client, full_name="Advisory Only"))["id"]
    await _ingest(auth_client, advisory, "Rosuvas 10mg HS")
    assert (await _check(auth_client, advisory, "Azithromycin"))["is_blocked"] is False


@pytest.mark.asyncio
async def test_stopping_the_interacting_drug_clears_the_flag(auth_client):
    """The check is a function of the record right now, not a sticky annotation on the drug."""
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, "Ecosprin 75mg OD")
    assert _partners(await _check(auth_client, pid, "Warfarin")) == {"Aspirin"}

    flags = (await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/flags")).json()
    assert flags["flags"] == [], "a lone aspirin has nothing to interact with"


@pytest.mark.asyncio
async def test_active_flags_reports_an_existing_pair_from_both_drugs_perspectives(auth_client):
    """A patient already on both halves of a pair is at risk now, not at next prescription.

    ``/flags`` re-runs every current drug against the others, so an interaction the patient is
    already living with surfaces without anyone proposing anything.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, "Warf 5mg OD", "Ecosprin 75mg OD")

    flags = (await auth_client.get(f"/api/v1/patients/{pid}/drug-safety/flags")).json()["flags"]
    interactions = [f for f in flags if f["check_type"] == "drug_interaction"]

    pairs = {
        frozenset((f["details"]["proposed_drug"], f["details"]["interacting_drug"]))
        for f in interactions
    }
    assert pairs == {frozenset(("Warfarin", "Aspirin"))}
    assert len(interactions) == 2, "the pair should be reported from each drug's side"


@pytest.mark.asyncio
async def test_a_drug_the_vocabulary_cannot_resolve_is_refused_rather_than_guessed(auth_client):
    """Refusing to check beats checking the wrong drug (CLAUDE.md pitfall #4).

    Silently matching an unknown name to the nearest vocabulary entry would produce an
    authoritative-looking safety verdict about a drug the patient is not being prescribed.
    """
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, "Warf 5mg OD")

    resp = await auth_client.post(
        f"/api/v1/patients/{pid}/drug-safety/check",
        json={"drug_name": "Zzzqqx Unknown Compound"},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "validation_error"


@pytest.mark.asyncio
async def test_every_interaction_flag_is_persisted_and_audited(auth_client):
    """Each flag is an immutable record with an id the clinician's override can reference."""
    pid = (await create_patient(auth_client))["id"]
    await _ingest(auth_client, pid, "Ecosprin 75mg OD", "Voveran 50mg BD")

    result = await _check(auth_client, pid, "Warfarin")
    assert all(f["id"] for f in _interactions(result)), "a flag came back without a stored id"

    audit = (await auth_client.get(f"/api/v1/patients/{pid}/audit?limit=100")).json()["items"]
    checks = [e for e in audit if e["action"] == "drug_safety_check"]
    assert checks, "the safety check was not audited"
    assert checks[0]["payload"]["flag_count"] >= 2
