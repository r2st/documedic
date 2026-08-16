"""A drug whose vocabulary row is deactivated must not take its hard block with it.

``drug_vocabulary.is_active`` is turned off by ordinary curation, not by anything exotic:
``app.db.seed`` deactivates a drug dropped from ``drug_vocabulary.json``, and collapses
pre-existing duplicates by deactivating all but the first. Every route into the vocabulary
honoured that — ``DrugResolver``'s name tiers, its fuzzy corpus, ``resolve_reference_id`` and
the batch form all filter on it — except one. ``SafetyService._vocabulary_by_id``, which
hydrates the rows a chart's medications and allergies link to *by id*, did not.

That single disagreement was enough, because the two halves of the chart-wide view sat on
opposite sides of it:

* ``build_context`` used the unfiltered lookup, so a medication linked to a deactivated row
  still became a ``DrugRef`` in ``current_meds`` — counted as evaluated, absent from
  ``check_unevaluated_medications``;
* ``active_flags`` then re-resolved each of those reference ids through the *filtered* batch
  form, found nothing, and skipped the drug with a bare ``continue``.

So the drug stayed listed on the chart, raised no flag, and appeared in no unevaluated note. A
documented aspirin allergy on a patient taking aspirin hard-blocked on the safety screen right
up until the vocabulary row was deactivated, after which the screen went quiet — which is the
response a clinician reads as "checked, nothing found". The same method feeds the reasoning
engine's mid-run hard-block recheck (``ReasoningService``), so the block went missing there too.

The fix is to make the id lookup agree with every other one. A withdrawn drug then stops
resolving and reaches the clinician as the visible gap ``app.db.seed`` documents as this
project's standing preference; a merely duplicated row recovers outright, because the surviving
row still answers to the same name.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from app.db.seed import seed_drug_vocabulary
from app.models.allergy import Allergy
from app.models.drug_vocabulary import DrugVocabulary
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

# Aspirin, because "patient is on it and is documented allergic to it" is the least ambiguous
# hard block in the corpus and the least ambiguous thing to watch disappear.
ASPIRIN = "Aspirin"


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"withdrawn-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Withdrawn Vocabulary Patient",
        sex="male",
        date_of_birth=datetime(1959, 3, 14).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _vocab(db, generic: str) -> DrugVocabulary:
    row = (
        (await db.execute(select(DrugVocabulary).where(DrugVocabulary.generic_name.ilike(generic))))
        .scalars()
        .first()
    )
    assert row is not None, f"seed corpus is missing {generic}"
    return row


async def _on_and_allergic_to(db, patient: Patient, vocab: DrugVocabulary) -> None:
    """The chart both linked by id — which is what approving an extraction writes."""
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            drug_vocabulary_id=vocab.id,
            generic_name=vocab.generic_name,
            event_type="continue",
            is_current=True,
        )
    )
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name=vocab.generic_name,
            allergen_type="drug",
            drug_vocabulary_id=vocab.id,
            status="active",
        )
    )
    await db.flush()


async def _withdraw(db, generic: str) -> None:
    """What the seed loader does to a drug dropped from ``drug_vocabulary.json``.

    Every row for the molecule, not just the one the chart happens to link to: a withdrawal
    removes the drug, and leaving another strength active would let name resolution recover it
    and make this test about something else.
    """
    await db.execute(
        update(DrugVocabulary)
        .where(DrugVocabulary.generic_name.ilike(generic))
        .values(is_active=False)
    )
    await db.flush()


def _types(results) -> set[str]:
    return {flag.check_type for _drug, flags in results for flag in flags}


# --- What the silence cost -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_withdrawing_the_drug_does_not_turn_a_hard_block_into_a_clean_chart(db):
    """The defect, stated as the difference between two calls.

    Nothing about the patient changes between them — same medication, same documented allergy,
    same status. Only the curated vocabulary moves, and the hard block a clinician was relying
    on is gone. Not downgraded, not reported as unevaluated: absent, from a response whose
    shape says the chart was checked.
    """
    account, patient = await _account_and_patient(db)
    aspirin = await _vocab(db, ASPIRIN)
    await _on_and_allergic_to(db, patient, aspirin)

    before = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    assert any(f.is_hard_block for _d, flags in before for f in flags), (
        "fixture check — an aspirin allergy on a patient taking aspirin must hard-block"
    )

    await _withdraw(db, ASPIRIN)

    service = SafetyService(db)
    after = await service.active_flags(account_id=account.id, patient_id=patient.id)
    completeness = await service.chart_completeness_flags(patient.id)
    assert after == [] and _types(before) == {"allergy_conflict"}, (
        "the drug can no longer be evaluated, so it raises no flag of its own — the assertion "
        "that matters is the next one"
    )
    assert {f.check_type for f in completeness} == {
        "unevaluated_medication",
        "unevaluated_allergy",
    }, (
        "a chart carrying a drug and an allergen nothing can identify came back with no notes "
        "at all — indistinguishable from a chart with nothing to find"
    )


@pytest.mark.asyncio
async def test_the_unevaluated_notes_name_the_drug_that_went_dark(db):
    """A gap nobody can act on is barely better than no gap.

    Both notes have to carry the charted name, because the clinician's move from here is to
    re-enter it or have the brand added to the vocabulary, and neither is possible against
    "one medication could not be read".
    """
    _account, patient = await _account_and_patient(db)
    await _on_and_allergic_to(db, patient, await _vocab(db, ASPIRIN))
    await _withdraw(db, ASPIRIN)

    notes = await SafetyService(db).chart_completeness_flags(patient.id)
    by_type = {flag.check_type: flag.details for flag in notes}
    assert by_type["unevaluated_medication"]["unresolved_medications"] == [ASPIRIN]
    assert by_type["unevaluated_allergy"]["unresolved_allergies"] == [ASPIRIN]


@pytest.mark.asyncio
async def test_the_medication_is_no_longer_counted_among_the_evaluated_ones(db):
    """The root cause, pinned where it lived.

    The two halves disagreed: the context said this drug was evaluable and the chart-wide view
    said it was not. Whichever way that is resolved, it must be resolved the *same* way in both
    — an entry in ``current_meds`` is a claim that the drug was checked against every rule.
    """
    _account, patient = await _account_and_patient(db)
    await _on_and_allergic_to(db, patient, await _vocab(db, ASPIRIN))
    await _withdraw(db, ASPIRIN)

    ctx = await SafetyService(db).build_context(patient.id)
    assert [m.reference_id for m in ctx.current_meds] == []
    assert ctx.unresolved_current_meds == [ASPIRIN]
    assert ctx.unresolved_allergies == [ASPIRIN]


@pytest.mark.asyncio
async def test_an_allergen_on_a_withdrawn_row_is_not_left_silently_toothless(db):
    """The allergy side had its own way of going quiet, one step earlier.

    ``_allergies`` took the name-resolution fallback only for an allergen with *no* link at
    all. An allergen linked to a deactivated row therefore fell past both arms: no reference id,
    no drug class — so ``check_allergies`` was reduced to comparing raw charted text — and no
    place in the unidentified list either, so ``check_unevaluated_allergies`` had nothing to
    report. Asserted with no medication on the chart so the only thing under test is the
    allergy.
    """
    _account, patient = await _account_and_patient(db)
    aspirin = await _vocab(db, ASPIRIN)
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name=aspirin.generic_name,
            allergen_type="drug",
            drug_vocabulary_id=aspirin.id,
            status="active",
        )
    )
    await db.flush()
    await _withdraw(db, ASPIRIN)

    ctx = await SafetyService(db).build_context(patient.id)
    assert ctx.unresolved_allergies == [ASPIRIN]
    assert [a.drug_reference_id for a in ctx.allergies] == [None]


# --- Recovery, where recovery is available -------------------------------------------------


@pytest.mark.asyncio
async def test_a_duplicate_row_collapsed_by_the_seeder_recovers_through_the_survivor(db):
    """The other way a row is deactivated, and the one that must not cost a check at all.

    Reference ids are compared case-folded when the loader decides what counts as the same
    entry, and the column is unique case-*sensitively* — so a deployed database can hold
    ``ASP-75`` and ``asp-75`` as two rows for one drug, and the loader collapses them by
    deactivating all but the first. A medication linked to the row that lost is not a withdrawn
    drug: the drug is still curated, still named the same, and the surviving row is right there.
    Falling through to name resolution is what re-points it, so the hard block survives.
    """
    account, patient = await _account_and_patient(db)
    aspirin = await _vocab(db, ASPIRIN)
    db.add(
        DrugVocabulary(
            brand_name=aspirin.brand_name,
            generic_name=aspirin.generic_name,
            reference_id=aspirin.reference_id.lower(),
            drug_class=aspirin.drug_class,
            components=aspirin.components,
        )
    )
    await db.flush()

    await seed_drug_vocabulary(db)
    await db.flush()
    collapsed = (
        (
            await db.execute(
                select(DrugVocabulary).where(
                    DrugVocabulary.generic_name.ilike(ASPIRIN),
                    DrugVocabulary.is_active.is_(False),
                )
            )
        )
        .scalars()
        .first()
    )
    assert collapsed is not None, "the loader did not collapse the duplicate — fixture has moved"

    await _on_and_allergic_to(db, patient, collapsed)

    service = SafetyService(db)
    results = await service.active_flags(account_id=account.id, patient_id=patient.id)
    assert any(f.is_hard_block for _d, flags in results for f in flags)
    assert await service.chart_completeness_flags(patient.id) == [], (
        "nothing was lost, so nothing should be reported as a gap"
    )


@pytest.mark.asyncio
async def test_an_unaffected_chart_is_untouched_by_the_filter(db):
    """The filter must cost nothing to every chart that is not in this situation.

    A regression here would be quiet in the same way the defect was — a hard block that stops
    firing — so it gets its own assertion rather than riding on the tests above.
    """
    account, patient = await _account_and_patient(db)
    await _on_and_allergic_to(db, patient, await _vocab(db, ASPIRIN))

    service = SafetyService(db)
    results = await service.active_flags(account_id=account.id, patient_id=patient.id)
    assert _types(results) == {"allergy_conflict"}
    assert all(f.is_hard_block for _d, flags in results for f in flags)
    assert await service.chart_completeness_flags(patient.id) == []


@pytest.mark.asyncio
async def test_a_product_charted_twice_is_evaluated_once(db):
    """Pins the deduplication the reference-id round-trip used to provide incidentally.

    The chart-wide view drops the drug under test from its own comparison set by reference id,
    so a second row for the same product is dropped with it — evaluating both would report
    every one of that drug's flags twice on the safety screen.
    """
    account, patient = await _account_and_patient(db)
    aspirin = await _vocab(db, ASPIRIN)
    await _on_and_allergic_to(db, patient, aspirin)
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            drug_vocabulary_id=aspirin.id,
            generic_name=aspirin.generic_name,
            event_type="continue",
            is_current=True,
        )
    )
    await db.flush()

    results = await SafetyService(db).active_flags(account_id=account.id, patient_id=patient.id)
    assert [drug.reference_id for drug, _flags in results] == [aspirin.reference_id]
    assert sum(len(flags) for _drug, flags in results) == 1
