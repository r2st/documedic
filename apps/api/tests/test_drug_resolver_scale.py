"""Load verification for the resolver's targeted-query path against a large vocabulary.

The resolver tests in ``test_services_coverage.py`` pin the *shape* of each query against the
~50-row seeded corpus. At that size "loaded one row" and "loaded the entire vocabulary" cost
the same and finish in the same microsecond, so those assertions can only catch a statement
that looks wrong — not one that is merely expensive. The optimisation they describe exists
for a corpus this project actually reaches: Indian brand names number in the tens of
thousands, and every medication and every allergy of every deterministic safety check goes
through resolution.

So these tests grow the vocabulary and assert the two properties that have to survive that
growth:

* **Cost is bounded by the patient, not by the corpus.** Measured as ORM rows hydrated, not
  just statements issued — a single ``SELECT`` that returns 20,000 rows is one statement and
  the wrong answer. Hydration is what the module docstring cites as the cost that regressed.
* **Answers are identical.** An optimisation that changes which drug resolves is a clinical
  defect, not a performance win: resolution decides whether an allergy hard-block fires.

The corpus is grown with names deliberately far from any real drug (``Qzvfiller...``) so the
distractors cannot legitimately win a fuzzy match — if one does, the test is reporting a real
scoring problem rather than a coincidence of the fixture.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, insert, select

from app.models.allergy import Allergy
from app.models.drug_vocabulary import DrugVocabulary
from app.models.medication_event import MedicationEvent
from app.services.drug_resolver import DrugResolver
from app.services.safety_service import SafetyService
from tests.test_query_efficiency import (
    _account_and_patient,
    _vocab,
    counting_queries,
)
from tests.test_services_coverage import _corpus_reads

# Large enough that hydrating all of it is unmistakable next to a handful of candidate rows,
# small enough to bulk-insert into in-memory SQLite in well under a second.
CORPUS_GROWTH = 2000

# A prefix no INN or Indian brand name starts with, so filler rows cannot collide with a
# seeded name on the exact tier or plausibly outscore one on the fuzzy tier.
_FILLER_PREFIX = "Qzvfiller"


async def _grow_corpus(db, n: int = CORPUS_GROWTH) -> None:
    """Add ``n`` unrelated active drugs, the way market coverage grows the table.

    Core ``insert()`` with a parameter list rather than ``n`` ORM objects: this is fixture
    setup, and flushing 2000 hydrated instances would also seed the identity map that the
    hydration assertions below measure.
    """
    await db.execute(
        insert(DrugVocabulary),
        [
            {
                "id": uuid.uuid4(),
                "brand_name": f"{_FILLER_PREFIX}Brand{i:05d}",
                "generic_name": f"{_FILLER_PREFIX}Generic{i:05d}",
                "reference_id": f"{_FILLER_PREFIX}-{i:05d}",
                "is_active": True,
                "source": "curated",
            }
            for i in range(n)
        ],
    )
    await db.flush()


def _hydrated_vocabulary_rows(db) -> int:
    """DrugVocabulary instances currently in the session's identity map.

    The metric the targeted-query path exists to bound. Statement counts alone cannot see the
    difference between ``WHERE reference_id = ?`` and a full-corpus read.
    """
    return sum(
        1 for obj in db.sync_session.identity_map.values() if isinstance(obj, DrugVocabulary)
    )


async def _corpus_size(db) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(DrugVocabulary)
            .where(DrugVocabulary.is_active.is_(True))
        )
        or 0
    )


# ---------------------------------------------------------------- exact tier, at scale


async def test_an_exact_lookup_hydrates_only_its_own_candidate_rows_in_a_large_corpus(db):
    """Resolving one name must not pull the corpus into memory behind it.

    This is the regression the targeted query was introduced for, asserted at a size where
    the difference is visible: the answer is a handful of brand-family rows, and everything
    else in the table is irrelevant to it.
    """
    await _grow_corpus(db)
    total = await _corpus_size(db)
    assert total > CORPUS_GROWTH, "fixture did not grow the vocabulary"

    resolver = DrugResolver(db)
    db.expunge_all()
    resolved = await resolver.resolve("Paracetamol")

    assert resolved is not None and resolved.match_type == "exact_generic"
    hydrated = _hydrated_vocabulary_rows(db)
    # The bound that matters is "brand family", not "corpus" — one molecule's rows.
    assert hydrated < 50, (
        f"one exact lookup hydrated {hydrated} vocabulary rows out of {total}; the exact tier "
        "is reading more than the queried name's candidate rows"
    )


async def test_exact_lookup_cost_does_not_grow_when_the_corpus_does(db):
    """The same lookup, before and after 2000 unrelated drugs arrive, must cost the same.

    A single measurement cannot distinguish "bounded by the patient" from "small because the
    fixture is small". Two measurements across a 40x corpus change can.
    """

    async def _measure() -> tuple[int, int]:
        resolver = DrugResolver(db)
        db.expunge_all()
        with counting_queries(db.bind) as counter:
            assert await resolver.resolve("Crocin") is not None
        return counter["n"], _hydrated_vocabulary_rows(db)

    small_queries, small_rows = await _measure()
    await _grow_corpus(db)
    large_queries, large_rows = await _measure()

    assert (large_queries, large_rows) == (small_queries, small_rows), (
        f"resolving 'Crocin' cost {small_queries} queries / {small_rows} rows against the "
        f"seeded corpus but {large_queries} / {large_rows} after {CORPUS_GROWTH} unrelated "
        "drugs were added — resolution is scaling with the corpus, not the patient"
    )


async def test_reference_id_resolution_stays_a_single_row_lookup_in_a_large_corpus(db):
    """``reference_id`` is unique, so this must stay one row however big the table gets."""
    await _grow_corpus(db)

    resolver = DrugResolver(db)
    db.expunge_all()
    with counting_queries(db.bind) as counter:
        row = await resolver.resolve_reference_id(f"{_FILLER_PREFIX}-01999")

    assert row is not None and row.generic_name == f"{_FILLER_PREFIX}Generic01999"
    assert counter["n"] == 1, f"expected one targeted query, got {counter['n']}"
    assert _hydrated_vocabulary_rows(db) == 1, (
        f"a unique-key lookup hydrated {_hydrated_vocabulary_rows(db)} rows"
    )


# ---------------------------------------------------------------- answers must not move


async def test_a_large_corpus_does_not_change_what_a_name_resolves_to(db):
    """Growing the vocabulary must not move an existing answer.

    Resolution decides whether an allergy hard-block fires, so a corpus-size-dependent answer
    is a clinical defect. Covers all three exact tiers plus the fuzzy one, which is the tier
    where extra candidates genuinely could steal a match.
    """
    names = ["Paracetamol", "Crocin", "Metformin", "Glycomett", "not-a-drug-at-all"]

    before = [await DrugResolver(db).resolve(name) for name in names]
    await _grow_corpus(db)
    after = [await DrugResolver(db).resolve(name) for name in names]

    assert after == before, (
        f"{CORPUS_GROWTH} unrelated drugs changed resolution: {after} != {before}"
    )
    # Pin the two that matter, so a future fixture change cannot make the comparison vacuous.
    assert before[0] is not None and before[0].match_type == "exact_generic"
    assert before[3] is not None and before[3].match_type == "fuzzy"


async def test_brand_over_generic_precedence_survives_a_large_corpus(db):
    """Tier precedence is decided in ``_pick``, over whatever rows the query returned.

    With a large table the natural row order of an unordered query is much freer to vary, so
    this pins that the winner is chosen by tier and not by which row happened to come back
    first.
    """
    collision = f"zz{uuid.uuid4().hex[:8]}"
    ref_brand = f"ref-{uuid.uuid4().hex}"
    db.add(DrugVocabulary(brand_name=collision, generic_name="Amoxicillin", reference_id=ref_brand))
    db.add(DrugVocabulary(generic_name=collision, reference_id=f"ref-{uuid.uuid4().hex}"))
    await db.flush()
    await _grow_corpus(db)

    resolved = await DrugResolver(db).resolve(collision)

    assert resolved is not None
    assert resolved.match_type == "exact_brand"
    assert resolved.reference_id == ref_brand


# ---------------------------------------------------------------- batch paths, at scale


async def test_prefetch_of_a_large_name_batch_is_still_one_query(db):
    """A batch far larger than any prescription must stay a single round trip.

    Bounds the parameter list too: ``_exact_statement`` binds each name three times (reference
    id, brand, generic), so a batch of 400 names is 1200 bind parameters in one statement.
    That is the dimension that fails abruptly rather than gradually if it is ever exceeded.
    """
    await _grow_corpus(db)
    real = ["Paracetamol", "Crocin", "Metformin", "Aspirin", "Atorvastatin"]
    filler = [f"{_FILLER_PREFIX}Brand{i:05d}" for i in range(395)]
    names = real + filler

    resolver = DrugResolver(db)
    db.expunge_all()
    with counting_queries(db.bind) as counter:
        await resolver.prefetch(names)
        resolved = [await resolver.resolve(name) for name in names]

    vocabulary_reads = [s for s in counter["statements"] if "drug_vocabulary" in s]
    assert len(vocabulary_reads) == 1, (
        f"prefetch of {len(names)} names cost {len(vocabulary_reads)} vocabulary reads"
    )
    assert all(r is not None for r in resolved), "a prefetched name failed to resolve"
    assert {r.match_type for r in resolved if r} == {"exact_generic", "exact_brand"}


async def test_resolve_reference_ids_stays_one_query_for_a_large_id_set(db):
    """The batch form ``active_flags`` uses must not degrade into a query per id."""
    await _grow_corpus(db)
    ids = [f"{_FILLER_PREFIX}-{i:05d}" for i in range(0, 600, 2)]

    resolver = DrugResolver(db)
    db.expunge_all()
    with counting_queries(db.bind) as counter:
        found = await resolver.resolve_reference_ids(ids)

    assert set(found) == set(ids), "the batch lookup dropped ids"
    assert counter["n"] == 1, f"{len(ids)} reference ids cost {counter['n']} queries"


async def test_the_fuzzy_tier_loads_the_large_corpus_exactly_once(db):
    """Fuzzy matching needs every candidate in memory — but pays for it once per resolver.

    Also asserts the right drug still wins: 2000 distractors must not outscore Metformin for
    an OCR misspelling of its brand name.
    """
    await _grow_corpus(db)

    resolver = DrugResolver(db)
    db.expunge_all()
    with counting_queries(db.bind) as counter:
        results = [await resolver.resolve(n) for n in ("Glycomett", "Crocinn", "zzzznothing")]

    corpus_reads = _corpus_reads(counter)
    assert len(corpus_reads) == 1, (
        f"three fuzzy lookups loaded the corpus {len(corpus_reads)} times: {corpus_reads}"
    )
    assert results[0] is not None and results[0].generic_name == "Metformin"
    assert results[1] is not None and results[1].generic_name == "Paracetamol"
    assert results[2] is None, "a filler row won a fuzzy match it should not clear"


# ---------------------------------------------------------------- the hot safety path


@pytest.mark.parametrize("grow_first", [False, True], ids=["seeded_corpus", "large_corpus"])
async def test_active_flags_hydrates_the_same_rows_whatever_the_corpus_size(db, grow_first):
    """End-to-end guard on the deterministic safety path (Critical Safety Rule #8).

    ``active_flags`` runs on every load of the safety screen and re-resolves every current
    medication and allergy. Parametrised rather than measured twice in one test so a failure
    names which corpus size broke, and so each case gets a clean session.
    """
    account, patient = await _account_and_patient(db)
    for generic in ("Metformin", "Atorvastatin", "Warfarin", "Aspirin"):
        vocab = await _vocab(db, generic)
        db.add(
            MedicationEvent(
                patient_id=patient.id,
                drug_vocabulary_id=vocab.id,
                generic_name=vocab.generic_name,
                event_type="continue",
                is_current=True,
            )
        )
    # An allergy with no vocabulary link, which is the row that falls through to name
    # resolution — the path that used to read the whole corpus.
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name="Crocin",
            allergen_type="drug",
            status="active",
        )
    )
    await db.flush()

    if grow_first:
        await _grow_corpus(db)

    service = SafetyService(db)
    db.expunge_all()
    with counting_queries(db.bind) as counter:
        await service.active_flags(account_id=account.id, patient_id=patient.id)
    selects = len([s for s in counter["statements"] if s.lstrip().upper().startswith("SELECT")])
    hydrated = _hydrated_vocabulary_rows(db)

    # Five drugs across meds + allergy; the vocabulary rows hydrated should be that family and
    # not the table. Asserted as an absolute bound so both parametrisations state the same
    # expectation and the large-corpus case cannot pass by matching a bloated baseline.
    assert hydrated < 50, (
        f"active_flags hydrated {hydrated} vocabulary rows (corpus grown={grow_first}) — the "
        "safety path is reading the vocabulary rather than the patient's drugs"
    )
    assert selects < 30, f"active_flags issued {selects} SELECTs for a 4-drug patient"
