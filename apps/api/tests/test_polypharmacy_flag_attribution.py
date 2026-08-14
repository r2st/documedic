"""``GET ../flags`` computed which drug each finding was about, then threw it away.

``SafetyService.active_flags`` evaluates every current medication against every other and returns
``list[tuple[DrugVocabulary, list[SafetyFlag]]]`` — grouped by the drug each finding is about.
The router flattened that into a bare list and dropped the vocabulary.

Two things follow, and only together are they a defect. A pairwise finding is raised from both
sides: warfarin interacting with aspirin is a fact about warfarin *and* a fact about aspirin, so
it appears under each. That is correct in a per-drug view and is why the service groups. Flatten
the grouping away and the same clinical fact becomes two identical-looking rows with nothing to
distinguish them — and the duplication grows with the square of the medication list, which is
exactly the chart this product is built for. Four current medications with four interacting pairs
produced eight rows for four findings.

So the fix is not to deduplicate — that would discard the per-drug view a clinician reviewing
polypharmacy actually wants — but to stop discarding the attribution that makes the duplication
readable.

A null attribution is meaningful too: it marks the chart-level flags, which are served once for
the whole chart rather than under any medication.
"""

from __future__ import annotations

import pytest

from app.routers.safety import _flag_to_response
from app.services.safety_service import SafetyService
from tests.test_safety_active_flags import _account_and_patient, _add_current_med, _vocab

pytestmark = pytest.mark.asyncio

_POLYPHARMACY = ("Warfarin", "Aspirin", "Diclofenac", "Ibuprofen")


async def _chart(db):
    account, patient = await _account_and_patient(db)
    for name in _POLYPHARMACY:
        if await _vocab(db, name) is None:
            pytest.skip(f"seed corpus lacks {name}")
        await _add_current_med(db, patient, name)
    return account, patient


async def test_every_interaction_flag_names_the_drug_it_was_raised_about(db) -> None:
    account, patient = await _chart(db)

    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    responses = [_flag_to_response(f, None, vocab) for vocab, flags in results for f in flags]

    interactions = [r for r in responses if r.check_type == "drug_interaction"]
    assert interactions, "seed corpus produced no interactions on this chart"
    assert all(r.drug_reference_id and r.drug_name for r in interactions)


async def test_a_pair_is_reported_under_both_of_its_drugs_and_says_so(db) -> None:
    """The duplication is real and is kept — this pins that it is now legible rather than
    silent. The same interaction rule appears twice, attributed to a different drug each time."""
    account, patient = await _chart(db)

    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    responses = [_flag_to_response(f, None, vocab) for vocab, flags in results for f in flags]

    by_rule: dict[str, set[str]] = {}
    for r in responses:
        if r.check_type == "drug_interaction" and r.drug_interaction_id:
            by_rule.setdefault(str(r.drug_interaction_id), set()).add(r.drug_name or "")

    # Each pairwise rule fires once from each side, so it carries two distinct drug names.
    assert by_rule
    assert all(len(names) == 2 for names in by_rule.values()), by_rule


async def test_the_flag_count_is_twice_the_number_of_distinct_findings(db) -> None:
    """The scaling this exists for. Before the attribution these were eight indistinguishable
    rows; a clinician could not tell four duplicated findings from eight separate ones."""
    account, patient = await _chart(db)

    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    responses = [_flag_to_response(f, None, vocab) for vocab, flags in results for f in flags]

    interactions = [r for r in responses if r.check_type == "drug_interaction"]
    distinct = {str(r.drug_interaction_id) for r in interactions}
    assert len(interactions) == 2 * len(distinct)


async def test_a_chart_level_flag_carries_no_drug_attribution(db) -> None:
    """Null is the signal that a flag is about the record rather than about a medication —
    an unreadable problem-list row or a Child-Pugh window belongs to no one drug."""
    _account, patient = await _chart(db)

    chart_flags = await SafetyService(db).chart_completeness_flags(patient.id)
    responses = [_flag_to_response(f) for f in chart_flags]

    assert all(r.drug_reference_id is None and r.drug_name is None for r in responses)


async def test_the_proposed_drug_check_leaves_the_attribution_null(db) -> None:
    """``POST ../check`` evaluates one drug and names it at the top level of the response, so
    repeating it on every flag would be noise rather than information."""
    account, patient = await _chart(db)

    _vocab_row, _ctx, flags, check_ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Warfarin",
    )
    responses = [_flag_to_response(f, cid) for f, cid in zip(flags, check_ids, strict=True)]

    assert responses
    assert all(r.drug_reference_id is None for r in responses)
