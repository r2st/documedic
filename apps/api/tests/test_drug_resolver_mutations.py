"""Decisions in the drug resolver that no test constrained.

Found by mutation testing ``app/services/drug_resolver.py``: each mutant below was applied to
the module one at a time and the whole resolver/safety test subset still passed, meaning
nothing anywhere depended on the decision being made the way it is written.

Three themes come out of the sweep.

**What counts as "this text names that drug".** ``_FuzzyIndex.rows_named_in`` is the matcher
behind both the ambiguity refusal at the clinician-proposal boundary and the screening of every
guideline management option against the patient's chart. Requiring the candidate's *whole*
token sequence, keeping digits as part of a token, and keying the result by reference id are
each load-bearing, and each survived being changed: a first-token-only match makes
"Amoxicillin 500" name a combination product the text never mentioned, dropping digits makes
"Vitamin D2" name the D3 row, and keying by generic name collapses two brands of one molecule
into a single hit -- which is exactly the double-dosing proposal the ambiguity refusal exists
to catch.

**``is_active`` on the three lookup paths.** A vocabulary row is deactivated when the product
should stop resolving -- a withdrawn or recalled brand, a superseded entry. All four of the
filters on the exact, single-reference-id and batch-reference-id paths survived removal: the
whole-corpus load is the only one anything tested. A deactivated row that still resolves is a
drug the safety engine evaluates rules against under a name the vocabulary has retired.

**The memo caches, and ``drug_class``.** The per-instance memos are the module's stated reason
for being cheap on the hot deterministic safety path, and every one of them could be deleted
without a test noticing. ``drug_class`` is worse than a performance decision: it is what the
allergy cross-class hard block, the cross-reactivity check and same-class duplicate therapy all
key on, and ``ResolvedDrug`` could drop it silently.

Six survivors are *equivalent* mutants -- the mutated code cannot behave differently through
any caller, so no test can kill them. They are documented at the bottom rather than chased.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.drug_vocabulary import DrugVocabulary
from app.services import drug_resolver as module
from app.services.drug_resolver import FUZZY_THRESHOLD, DrugResolver, _tokens
from tests.test_query_efficiency import counting_queries

# --- helpers -------------------------------------------------------------------------------


async def _add(
    db,
    *,
    generic: str,
    brand: str | None = None,
    reference_id: str | None = None,
    drug_class: str | None = None,
    is_active: bool = True,
) -> DrugVocabulary:
    row = DrugVocabulary(
        brand_name=brand,
        generic_name=generic,
        reference_id=reference_id or f"REF-{uuid.uuid4().hex[:10]}",
        drug_class=drug_class,
        is_active=is_active,
        source="curated",
    )
    db.add(row)
    await db.flush()
    return row


async def _seeded(db, generic: str) -> DrugVocabulary:
    row = (
        (await db.execute(select(DrugVocabulary).where(DrugVocabulary.generic_name == generic)))
        .scalars()
        .first()
    )
    assert row is not None, f"seed data is missing {generic}"
    return row


# --- what counts as naming a drug ----------------------------------------------------------


def test_a_token_keeps_its_digits() -> None:
    """Mutant: ``_TOKEN_RE`` ``[a-z0-9]+`` -> ``[a-z]+``, which survived.

    ``_tokens`` owns the tokenising contract for both sides of the whole-name match, so a
    change there applies to the candidate and the query alike and mostly cancels out -- which
    is why it survived. It stops cancelling when the digit is the whole distinction between two
    drugs, and in this corpus it is: "Calcium + Vitamin D3" (cholecalciferol) and vitamin D2
    (ergocalciferol) differ by exactly the character the mutant discards.
    """
    assert _tokens("Calcium + Vitamin D3") == ("calcium", "vitamin", "d3")
    assert _tokens("Calcium + Vitamin D2") != _tokens("Calcium + Vitamin D3")
    # The case-folding half of the same helper. Every caller happens to lower-case its input
    # first, so the redundant outer calls cannot pin this; the helper itself can.
    assert _tokens("CROCIN") == _tokens("crocin") == ("crocin",)


async def test_a_drug_is_not_named_by_a_text_that_only_shares_its_first_word(db) -> None:
    """Mutant: ``if span == candidate_tokens`` -> comparing only the first token, which
    survived.

    The corpus is full of Indian combination products whose names begin with a single-molecule
    drug's name -- "Amoxicillin + Clavulanic acid", "Calcium + Vitamin D3". Matching on the
    first token alone makes a guideline sentence recommending plain amoxicillin *name* the
    combination, and ``screen_text`` then evaluates the patient's allergy and renal rules
    against a drug the guideline never mentioned. A spurious hard block on a correct
    recommendation is the failure the whole-name rule exists to avoid.
    """
    combination = await _seeded(db, "Amoxicillin + Clavulanic acid")
    resolver = DrugResolver(db)

    named = await resolver.rows_named_in("Amoxicillin 500 mg three times daily")

    assert combination.reference_id not in named
    # The full name still matches, across whatever punctuation separates its words.
    whole = await resolver.rows_named_in("Start Amoxicillin + Clavulanic acid 625 BD")
    assert combination.reference_id in whole


async def test_two_brands_of_one_molecule_are_named_as_two_distinct_drugs(db) -> None:
    """Mutant: ``found[row.reference_id] = row`` -> keyed by ``row.generic_name``, which
    survived.

    Two products sharing a generic name are two rows, and naming both of them in one proposal
    is the unintentional-double-dosing case ``_reject_if_multiple_drugs`` refuses. Keyed by
    generic name they collapse to one hit, the proposal reads as naming a single drug, and the
    check runs on one of the two brands as though that were the whole proposal.
    """
    crocin = await _seeded(db, "Paracetamol")  # seeded as Crocin
    dolo = await _add(db, generic="Paracetamol", brand="Dolo", reference_id="PCM-DOL-650")
    resolver = DrugResolver(db)

    named = await resolver.rows_named_in("Crocin 500 and Dolo 650 as needed")

    assert set(named) == {crocin.reference_id, dolo.reference_id}
    assert {row.generic_name for row in named.values()} == {"Paracetamol"}


# --- is_active on every lookup path ---------------------------------------------------------


async def test_a_deactivated_row_does_not_answer_an_exact_lookup(db) -> None:
    """Mutant: the ``is_active`` filter dropped from ``_exact_statement``, which survived.

    Deactivation is how a withdrawn or superseded product is taken out of use without deleting
    the row the historical records point at. Without the filter the retired entry answers by
    brand, generic *and* reference id, so the safety engine evaluates its curated rules under a
    name the vocabulary has retired.
    """
    await _add(
        db,
        generic="Withdrawnamol",
        brand="Retiredbrand",
        reference_id="WDN-001",
        is_active=False,
    )
    resolver = DrugResolver(db)

    assert await resolver.resolve("Retiredbrand") is None
    assert await DrugResolver(db).resolve("Withdrawnamol") is None
    assert await DrugResolver(db).resolve("WDN-001") is None


async def test_a_deactivated_row_does_not_answer_a_reference_id_lookup(db) -> None:
    """Mutants: the ``is_active`` filter dropped from ``resolve_reference_id`` and from the
    batch ``resolve_reference_ids``. Both survived.

    These are the two paths ``active_flags`` and ``check_medication`` take to turn a reference
    id back into a vocabulary row, and a retired row returned here is prescribed against.
    """
    await _add(db, generic="Withdrawnamol", reference_id="WDN-002", is_active=False)
    active = await _seeded(db, "Metformin")

    assert await DrugResolver(db).resolve_reference_id("WDN-002") is None

    batch = await DrugResolver(db).resolve_reference_ids(["WDN-002", active.reference_id])
    assert set(batch) == {active.reference_id}


# --- reference id is the canonical key ------------------------------------------------------


async def test_a_reference_id_outranks_another_rows_brand_name(db) -> None:
    """Mutant: ``_EXACT_TIERS`` reordered so ``reference_id`` is checked last, which survived.

    Real vocabularies collide: an Indian brand name is free to be spelled the same as another
    product's reference id, and the two rows are different drugs with different curated rules.
    The reference id is the canonical key, so it answers first -- otherwise which drug a
    caller gets back depends on which collision the corpus happens to contain.
    """
    canonical = await _add(db, generic="Canonicalol", reference_id="COLLIDE-1")
    impostor = await _add(db, generic="Impostorol", brand="COLLIDE-1", reference_id="OTHER-1")

    resolved = await DrugResolver(db).resolve("COLLIDE-1")

    assert resolved is not None
    assert (resolved.reference_id, resolved.match_type) == (
        canonical.reference_id,
        "exact_reference",
    )
    assert resolved.reference_id != impostor.reference_id


# --- what a resolved drug carries -----------------------------------------------------------


async def test_a_resolved_drug_carries_its_drug_class(db) -> None:
    """Mutant: ``drug_class=row.drug_class`` -> ``None`` in ``_make``, which survived.

    Not a cosmetic field. ``drug_class`` is the sole input to the same-class allergy hard block,
    to the curated cross-reactivity families (penicillin <-> cephalosporin), and to same-class
    duplicate therapy. Dropped here, all three stop firing for every drug -- and each of them
    stops by finding nothing, which is indistinguishable from a chart with nothing to find.
    """
    resolved = await DrugResolver(db).resolve("Glycomet")

    assert resolved is not None
    assert resolved.drug_class == (await _seeded(db, "Metformin")).drug_class == "Biguanide"


async def test_a_score_exactly_at_the_threshold_is_accepted(db, monkeypatch) -> None:
    """Mutant: ``best[1] >= FUZZY_THRESHOLD`` -> ``>``, which survived.

    The boundary itself is the decision: 86.0 is the lowest score the resolver will act on, not
    the highest it refuses. No real name pair scores exactly 86.0 against this corpus, so the
    scorer is stubbed to return the boundary -- what is under test is the comparison, not
    rapidfuzz.
    """
    calls: list[float] = []

    def _at_threshold(query, choices, **kwargs):
        key = next(k for k in choices if k == "metformin")
        calls.append(FUZZY_THRESHOLD)
        return (key, FUZZY_THRESHOLD, 0)

    monkeypatch.setattr(module.process, "extractOne", _at_threshold)

    resolved = await DrugResolver(db).resolve("mtfrmn")

    assert calls, "the fuzzy tier was never reached"
    assert resolved is not None
    assert resolved.match_type == "fuzzy"
    assert resolved.score == FUZZY_THRESHOLD


# --- the memo caches ------------------------------------------------------------------------


async def test_a_blank_name_costs_no_sql(db, engine) -> None:
    """Mutant: ``resolve``'s ``if not name or not name.strip()`` guard removed, which survived.

    Both spellings answer ``None``, which is why nothing noticed: an empty query matches no row
    exactly and scores nothing on the fuzzy tier. What changes is the cost of getting there --
    an exact query plus a full-corpus load and a rapidfuzz scan of every candidate name, per
    blank. Blanks are ordinary here: ``_current_meds`` and ``_allergies`` resolve whatever name
    the OCR produced for every row on the chart.
    """
    resolver = DrugResolver(db)

    with counting_queries(engine) as counter:
        for blank in (None, "", "   ", "\n\t"):
            assert await resolver.resolve(blank) is None

    assert counter["n"] == 0, counter["statements"]


async def test_repeated_resolution_of_one_name_costs_no_sql(db, engine) -> None:
    """The two memos in front of a repeated lookup, together.

    Neither ``resolve``'s ``self._by_name`` write nor ``_exact_rows``'s ``self._exact`` write
    was pinned by anything, and neither is killed by *this* test on its own: they mask each
    other, because a repeated name that skips the top memo still finds its rows in the lower
    one and vice versa. The two tests below isolate them. This one pins what the pair is for.
    """
    resolver = DrugResolver(db)
    await resolver.resolve("Metformin")

    with counting_queries(engine) as counter:
        for _ in range(5):
            assert (await resolver.resolve("Metformin")) is not None

    assert counter["n"] == 0, counter["statements"]


async def test_a_name_resolved_singly_is_not_queried_again_by_a_later_prefetch(db, engine):
    """Mutant: ``_exact_rows``'s ``self._exact[query] = rows`` write removed, which survived.

    ``_by_name`` hides that write from every repeat of the same ``resolve``, so the only thing
    that can see it is the other reader of the same dict: ``prefetch`` subtracts
    ``self._exact.keys()`` from the batch it is about to ask for. Resolve-then-prefetch is the
    ordinary order — a single lookup while assembling a proposal, then a whole medication list
    prefetched to build the safety context around it — and without the write the name is
    queried twice.
    """
    resolver = DrugResolver(db)
    await resolver.resolve("Metformin")

    with counting_queries(engine) as counter:
        await resolver.prefetch(["Metformin"])

    assert counter["n"] == 0, counter["statements"]


async def test_a_repeated_unresolvable_name_is_scored_against_the_corpus_once(db, monkeypatch):
    """Mutant: ``resolve``'s ``self._by_name[query] = resolved`` write removed, which survived.

    The lower memo hides this one from a query count: the rows are already held, so the repeat
    costs no SQL either way. What it costs is the fuzzy scan — rapidfuzz scores the query
    against every brand and generic name in the corpus, which is the one part of resolution
    whose cost grows with the vocabulary rather than with the patient. A name that resolves to
    nothing is the case that reaches it every time, and unrecognised names arrive in batches:
    a scanned prescription whose brand is not yet seeded produces the same unmatched string on
    every safety check of that chart.
    """
    scans: list[str] = []
    real = module.process.extractOne

    def _counted(query, choices, **kwargs):
        scans.append(query)
        return real(query, choices, **kwargs)

    monkeypatch.setattr(module.process, "extractOne", _counted)
    resolver = DrugResolver(db)

    for _ in range(4):
        assert await resolver.resolve("Qzvfiller-not-a-drug") is None

    assert scans == ["qzvfiller-not-a-drug"]


async def test_prefetch_does_not_requery_names_it_already_holds(db, engine) -> None:
    """Mutant: ``- self._exact.keys()`` dropped from ``prefetch``'s query set, which survived.

    ``prefetch`` exists to collapse a whole medication list into one query. Re-issuing it for
    names already memoised undoes that for the ordinary case -- a chart re-screened once per
    management option a run produced, which is what ``_patient_facts`` memoises around.
    """
    resolver = DrugResolver(db)
    await resolver.prefetch(["Metformin", "Amoxicillin"])

    with counting_queries(engine) as counter:
        await resolver.prefetch(["Metformin", "Amoxicillin"])

    assert counter["n"] == 0, counter["statements"]

    # A genuinely new name still costs exactly one query, for the whole batch.
    with counting_queries(engine) as counter:
        await resolver.prefetch(["Metformin", "Amoxicillin", "Warfarin", "Aspirin"])

    assert counter["n"] == 1, counter["statements"]


async def test_a_reference_id_miss_is_remembered_as_a_miss(db, engine) -> None:
    """Mutants: ``resolve_reference_ids``'s negative memo (``self._by_reference_id[ref] =
    found.get(ref)``) replaced by a positive-only update, and its ``wanted - keys`` diff
    dropped. Both survived.

    A reference id with no active row is the normal result of a retired or not-yet-seeded
    entry. Remembering only the hits means every later lookup of the same id re-queries, so the
    ids that cost the most are the ones that are never there.
    """
    resolver = DrugResolver(db)
    metformin = await _seeded(db, "Metformin")
    await resolver.resolve_reference_ids([metformin.reference_id, "NOPE-1", "NOPE-2"])

    with counting_queries(engine) as counter:
        again = await resolver.resolve_reference_ids([metformin.reference_id, "NOPE-1", "NOPE-2"])
        assert await resolver.resolve_reference_id("NOPE-1") is None

    assert counter["n"] == 0, counter["statements"]
    assert set(again) == {metformin.reference_id}


async def test_the_batch_lookup_returns_only_rows_it_found(db) -> None:
    """Mutant: ``resolve_reference_ids`` returning ``{ref: self._by_reference_id.get(ref)}``,
    which survived -- so misses came back as ``ref -> None`` entries.

    ``active_flags`` happens to skip falsy values, which is why nothing noticed. The contract is
    a mapping of rows, and a caller reading ``.generic_name`` off one of those ``None``s is one
    ``AttributeError`` away on the deterministic safety path.
    """
    metformin = await _seeded(db, "Metformin")

    found = await DrugResolver(db).resolve_reference_ids([metformin.reference_id, "NOPE-3"])

    assert set(found) == {metformin.reference_id}
    assert all(row is not None for row in found.values())


@pytest.mark.parametrize("empty", [[], [None, ""]])
async def test_the_batch_lookup_asks_nothing_when_there_is_nothing_to_ask(db, engine, empty):
    """The falsy-id filter and the ``if missing`` guard, pinned together.

    Only falsy ids are filtered — a whitespace-only reference id is queried for. That is the
    right reading here and not the gap it looks like: reference ids are the vocabulary's own
    keys, produced by the seeding pipeline, never a name a clinician typed or OCR lifted. The
    blank-and-whitespace guard belongs on ``resolve``, which is the path that does take those,
    and it is there.
    """
    with counting_queries(engine) as counter:
        assert await DrugResolver(db).resolve_reference_ids(empty) == {}

    assert counter["n"] == 0, counter["statements"]


# --- equivalent mutants ---------------------------------------------------------------------
#
# Four survivors from the sweep cannot be killed, because the mutated code is incapable of
# behaving differently through any caller. They are recorded here so a later sweep does not
# spend the effort again:
#
#   * ``rows_named_in``/``drugs_named_in``: ``text.strip().lower()`` -> ``text.strip()``.
#     ``_tokens`` lower-cases whatever it is handed, so the outer call cannot change an answer.
#     It is a redundant second defence, and it masks the ``_tokens`` mutant in the other
#     direction -- which is why that one is pinned on the helper directly, by
#     ``test_a_token_keeps_its_digits``, rather than through a caller.
#   * ``_pick``: ``if value and value.lower() == query`` -> ``if (value or "").lower() ==
#     query``. The two differ only when ``query`` is the empty string, and both ways into
#     ``_pick`` reject blanks before it is reached (``resolve`` guards, ``prefetch`` filters).
#   * ``prefetch``: the ``if row not in grouped[folded]`` guard removed. A row matching one
#     name on two tiers is appended twice, and ``_pick`` scans tiers in precedence order and
#     returns the first match either way -- so the duplicate changes the list's length and
#     nothing else.
#   * ``_resolve_uncached``: ``if not index.keys: return None`` made unreachable. It is a fast
#     path, not a decision: ``process.extractOne`` over an empty candidate list returns
#     ``None``, which the ``if best and ...`` below already handles the same way.
