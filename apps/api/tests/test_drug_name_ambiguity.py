"""A proposal naming two drugs must not be answered as a check on one of them.

``DrugResolver`` scores with ``fuzz.WRatio``, whose partial-ratio path returns 90 — above the
86 acceptance threshold — for any query that merely *contains* a candidate name. That leniency
is wanted: "Augmentin Duo 625 Tablet" and "Tab. Crocin 500 BD x 5 days" are what clinicians type
and what OCR lifts off a prescription. It also meant "Warfarin, Aspirin" resolved to Aspirin and
`POST /drug-safety/check` replied `is_blocked: false` — for the textbook major interaction, with
the warfarin never evaluated and nothing in the verdict saying so.

Two halves, tested separately: the index rule (which names count as naming a drug) and the
endpoint boundary (what the clinician is told). The record-derived resolution path is asserted
*unchanged*, because refusing ambiguity there would drop combination products from the safety
evaluation entirely rather than checking them partially.
"""

from __future__ import annotations

import pytest

from app.models.medication_event import MedicationEvent
from app.services.drug_resolver import DrugResolver
from app.services.safety_service import SafetyService
from tests.conftest import create_patient
from tests.test_query_efficiency import _account_and_patient

# --- Which strings name more than one drug ---------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Crocin + Augmentin",
        "Crocin\nAugmentin",  # OCR of a two-line prescription
        "Warfarin, Aspirin",
        "Amlodipine and Atenolol",
        "warfarin aspirin",  # no punctuation at all
        "Crocin / Augmentin",
        "give Crocin then Augmentin",
    ],
)
@pytest.mark.asyncio
async def test_multi_drug_strings_are_recognised_as_multi_drug(db, name):
    named = await DrugResolver(db).drugs_named_in(name)
    assert len(named) >= 2, f"{name!r} should name several drugs, got {named}"


@pytest.mark.parametrize(
    "name",
    [
        "Crocin",
        "Augmentin Duo 625 Tablet",  # trade dressing around one drug
        "Tab. Crocin 500 BD x 5 days",  # a whole prescription sig for one drug
        "Crocin 500mg",
        "Crocine",  # OCR typo -- names nothing exactly, left to the fuzzy tier
        "Crosin",
        "xyzzyplughnotadrug",
        "",
        "   ",
    ],
)
@pytest.mark.asyncio
async def test_single_drug_and_unknown_strings_are_not_flagged(db, name):
    named = await DrugResolver(db).drugs_named_in(name)
    assert len(named) < 2, f"{name!r} should not look like several drugs, got {named}"


@pytest.mark.asyncio
async def test_a_brand_beside_its_own_generic_is_one_drug_not_two(db):
    """ "Crocin (Paracetamol) 500" names one molecule twice, and must stay checkable.

    Counting by reference id rather than by matched name is what makes this work — the brand and
    the generic resolve to the same row.
    """
    named = await DrugResolver(db).drugs_named_in("Crocin (Paracetamol) 500")
    assert len(named) == 1
    assert "Paracetamol" in named.values()


@pytest.mark.asyncio
async def test_matching_is_on_whole_tokens_not_raw_substrings(db):
    """A candidate name buried inside a longer word does not count as naming that drug.

    Substring matching would find "Crocin" inside "Crocinex" — a different product — and a
    made-up name that happens to contain two real ones would be refused for the wrong reason.
    """
    resolver = DrugResolver(db)
    assert await resolver.drugs_named_in("Crocinex") == {}
    assert await resolver.drugs_named_in("aspirinaspirinaspirin") == {}


@pytest.mark.asyncio
async def test_a_multiword_generic_matches_across_its_own_punctuation(db):
    """ "Amoxicillin + Clavulanic acid" is one drug whose *name* contains a separator.

    Tokenising both sides is what keeps this from being read as two drugs because of the "+".
    """
    named = await DrugResolver(db).drugs_named_in("Amoxicillin + Clavulanic acid")
    assert len(named) == 1


# --- The endpoint boundary -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_dangerous_case_is_refused_not_answered(auth_client):
    """Warfarin + aspirin: the pair the deterministic checker exists to catch.

    Before, this resolved to Aspirin alone and returned 200 with `is_blocked: false`. A clinician
    reading that verdict has been told the combination is clear, when the combination was never
    what was evaluated.
    """
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Warfarin, Aspirin"},
    )

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "validation_error"
    # Names both drugs back, so the clinician can see what to check separately.
    assert "Warfarin" in body["message"]
    assert "Aspirin" in body["message"]
    # And says plainly that this is not a clean result.
    assert "not the same as" in body["message"]


@pytest.mark.asyncio
async def test_the_refusal_never_reports_a_verdict(auth_client):
    """No 2xx, and therefore no `is_blocked` field for a client to misread as clearance."""
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Crocin + Augmentin"},
    )
    assert resp.status_code == 422
    assert "is_blocked" not in resp.json()


@pytest.mark.asyncio
async def test_ambiguous_and_unknown_give_different_advice(auth_client):
    """The two failures need opposite next steps from the clinician.

    An unrecognised name is a spelling or vocabulary-coverage problem — try the INN, check the
    spelling. A name with two drugs in it is neither; telling that clinician to check their
    spelling sends them looking for a fault that is not there, and they may well "fix" it by
    retyping the same two drugs.
    """
    patient = await create_patient(auth_client)
    url = f"/api/v1/patients/{patient['id']}/drug-safety/check"

    unknown = await auth_client.post(url, json={"drug_name": "xyzzyplughnotadrug"})
    ambiguous = await auth_client.post(url, json={"drug_name": "Warfarin, Aspirin"})

    assert unknown.status_code == ambiguous.status_code == 422
    assert "could not be matched" in unknown.json()["message"]
    assert "spelling" in unknown.json()["message"]
    assert "names more than one drug" in ambiguous.json()["message"]
    assert "spelling" not in ambiguous.json()["message"]


@pytest.mark.asyncio
async def test_realistic_single_drug_input_still_resolves(auth_client):
    """The regression guard on the fix: the leniency that made the bug is still wanted.

    Every one of these is a normal thing to type or to lift off a scan, and each names exactly
    one drug. If the ambiguity check ever starts refusing these, the endpoint has become useless
    for its actual market — Indian brand names arrive wrapped in strengths and dosage forms.
    """
    patient = await create_patient(auth_client)
    url = f"/api/v1/patients/{patient['id']}/drug-safety/check"

    for name in (
        "Crocin",
        "crocin",
        "Crocin 500mg",
        "Tab. Crocin 500 BD x 5 days",
        "Augmentin Duo 625 Tablet",
        "Paracetamol",
        "Crocine",  # OCR typo, resolved by the fuzzy tier
    ):
        resp = await auth_client.post(url, json={"drug_name": name})
        assert resp.status_code == 200, f"{name!r} should still resolve: {resp.text}"
        assert resp.json()["proposed_drug_reference_id"]


@pytest.mark.asyncio
async def test_a_reference_id_bypasses_the_check_entirely(auth_client):
    """A canonical reference id is unambiguous by construction — it identifies one row.

    Worth pinning: the guard runs only on the free-text path, so a client that has already
    resolved the drug pays nothing for it.
    """
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_reference_id": "PCM-500"},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_the_refusal_is_deterministic_and_needs_no_llm(auth_client, monkeypatch):
    """This is the offline deterministic path (CLAUDE.md rule 8) and must not have gained a call.

    Any LLM provider being reachable is irrelevant to it; asserted by making every provider
    unusable and checking both the refusal and the ordinary check still work.
    """
    monkeypatch.setattr("app.config.settings.openai_api_key", "")
    monkeypatch.setattr("app.config.settings.anthropic_api_key", "")
    monkeypatch.setattr("app.config.settings.openrouter_api_key", "")

    patient = await create_patient(auth_client)
    url = f"/api/v1/patients/{patient['id']}/drug-safety/check"
    assert (await auth_client.post(url, json={"drug_name": "Warfarin, Aspirin"})).status_code == 422
    assert (await auth_client.post(url, json={"drug_name": "Crocin"})).status_code == 200


# --- What the fix deliberately leaves alone --------------------------------------------------


@pytest.mark.asyncio
async def test_record_derived_resolution_is_unchanged(db):
    """``resolve`` still resolves a multi-drug string to one drug, on purpose.

    It also runs over names already stored in the chart, where an Indian combination product
    saved as one row ("Amlodipine + Atenolol") reaching the checker as amlodipine is a *partial*
    safety evaluation — and ``SafetyService._current_meds`` silently skips whatever it cannot
    resolve, so refusing here would drop the product from the evaluation altogether. Partial
    beats absent for stored data; for a clinician's live proposal, neither is acceptable and the
    answer is to refuse and say so.

    ``resolve`` has since grown an ambiguity check of its own (``DrugResolver._fuzzy``), and this
    case is exempt from it by construction: the guard is skipped for text that *lists* several
    drugs at disjoint positions, which is what this is. What the guard refuses is the different
    case where the text names one drug and which one is a guess — see
    ``test_drug_resolver_ambiguity``. If this test ever starts failing because that exemption was
    narrowed, read ``_current_meds`` before deciding that is an improvement.
    """
    resolved = await DrugResolver(db).resolve("Amlodipine and Atenolol")
    assert resolved is not None
    assert resolved.match_type == "fuzzy"


@pytest.mark.asyncio
async def test_a_stored_combination_product_is_still_evaluated(db):
    """The regression this change could plausibly have caused, asserted directly.

    A medication row whose name lists two molecules and carries no vocabulary link is the exact
    shape that ``_current_meds`` resolves by name. Before the fix it reached the checker as
    warfarin (partial). If the ambiguity guard ever leaks into ``resolve``, it reaches the
    checker as nothing at all — the patient's warfarin stops being considered, silently, which is
    strictly worse than considering half of it.

    Paired with a proposed aspirin, so the assertion is about a flag that must fire rather than
    about a 200.
    """
    account, patient = await _account_and_patient(db)
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            drug_vocabulary_id=None,  # unlinked, so it falls through to name resolution
            generic_name="Warfarin and Aspirin",
            event_type="continue",
            is_current=True,
        )
    )
    await db.commit()

    _vocab, ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Warfarin",
    )
    # The stored combination still contributes a drug to the safety context rather than being
    # dropped, and the warfarin/aspirin pair is still reachable from it.
    assert ctx.current_meds, "a stored combination product was dropped from the safety context"
    assert flags, "no flag raised against a chart holding a warfarin/aspirin combination"


@pytest.mark.asyncio
async def test_ambiguity_lookup_costs_no_query_when_the_name_is_blank(db):
    """An empty proposal must not load the vocabulary corpus to decide it names nothing."""
    resolver = DrugResolver(db)
    assert await resolver.drugs_named_in(None) == {}
    assert await resolver.drugs_named_in("   ") == {}
    # The fuzzy index -- the thing that costs a full-corpus read -- was never built.
    assert resolver._index is None


@pytest.mark.asyncio
async def test_service_helper_raises_for_two_drugs_and_returns_for_one(db):
    """Direct unit coverage of the guard, independent of the route wiring."""
    from app.exceptions import ValidationError

    service = SafetyService(db)
    await service._reject_if_multiple_drugs("Crocin")  # no raise
    with pytest.raises(ValidationError, match="names more than one drug"):
        await service._reject_if_multiple_drugs("Crocin + Augmentin")
