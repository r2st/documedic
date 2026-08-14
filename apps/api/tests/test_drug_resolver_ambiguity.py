"""A fuzzy match that two different drugs could satisfy must resolve to neither.

``DrugResolver`` scores with ``fuzz.WRatio``, which blends in ``partial_ratio`` — a candidate
contained inside the query scores as a perfect hit. That leniency is load-bearing: OCR delivers
"Tab. Glycomet GP 1", "Cap Omez 20", "Inj Lasix", and the vocabulary holds the bare brand. It also
means a *shorter* name can beat the true one on a query the true one merely differs from, and the
resolver accepted whatever came out on top as long as it cleared 86.

One character was enough. "Glycomet GP" scanned with the P read as an R — the commonest confusion
on a printed strip — scored 95.0 against "Glycomet" and 90.9 against "Glycomet GP", and resolved
to plain metformin. Glimepiride, the sulfonylurea half carrying the hypoglycaemia risk and its own
interactions, left the chart. Every rule keyed on it then found nothing, which is indistinguishable
from a clean safety report.

Three things are pinned here:

* the misresolutions themselves are refused rather than answered wrongly (``_fuzzy``'s guards);
* the OCR boilerplate the leniency exists for still resolves, so the fix did not buy safety by
  disabling fuzzy matching;
* text that genuinely *lists* several drugs is exempt, which is a different ambiguity with its own
  deliberate policy — see ``test_drug_name_ambiguity``.

The corpus here is the seeded one, so these are the product pairs the deployment actually ships.
"""

from __future__ import annotations

import pytest

from app.services.drug_resolver import (
    FUZZY_THRESHOLD,
    FUZZY_TIE_MARGIN,
    MIN_FUZZY_QUERY_CHARS,
    DrugResolver,
)

pytestmark = pytest.mark.asyncio


# --- the misresolutions this guard exists for ------------------------------------------------


@pytest.mark.parametrize(
    ("garbled", "would_have_been", "truth"),
    [
        # P -> R and P -> Q: the two ways a printed "GP" degrades. Both scored the *shorter*
        # "Glycomet" above the true "Glycomet GP" through WRatio's substring path.
        ("glycomet gr", "MET-500", "MET-GLM-1-500"),
        ("glycomet qp", "MET-500", "MET-GLM-1-500"),
    ],
)
async def test_a_one_character_scan_error_does_not_drop_a_combinations_second_ingredient(
    db, garbled, would_have_been, truth
):
    """The finding, stated as the clinical fact it is.

    ``would_have_been`` is asserted only to keep the test honest about what it is preventing: the
    misresolution is not hypothetical, it is what the previous code returned. What matters is that
    the answer now is *neither* — resolving to the truth by some other route would be nice but is
    not available from a garbled string, and claiming it would be the same guess in the other
    direction.
    """
    resolver = DrugResolver(db)

    assert await resolver.resolve(garbled) is None, (
        f"{garbled!r} resolved rather than being refused; before the guard it resolved to "
        f"{would_have_been} when the drug on the page was {truth}"
    )

    # The correctly-spelled names both still resolve, and to different drugs — i.e. the guard did
    # not achieve its result by making this pair unresolvable in general.
    plain = await resolver.resolve("Glycomet")
    combination = await resolver.resolve("Glycomet GP")
    assert plain is not None and combination is not None
    assert plain.reference_id == would_have_been
    assert combination.reference_id == truth


async def test_a_dead_heat_between_a_drug_and_the_same_drug_plus_a_thiazide_is_refused(db):
    """ "Telma" and "Telma H" score 90.0 each; the winner was decided by table order.

    Telmisartan alone and telmisartan with hydrochlorothiazide are different products, and the
    difference is a thiazide — potassium, sodium, gout, and sulfonamide cross-reactivity all hang
    on it. A tie broken by which row the vocabulary happened to yield first is not an answer.
    """
    assert await DrugResolver(db).resolve("t. telma h 40") is None


async def test_a_fragment_too_short_to_identify_a_drug_is_refused(db):
    """ "ran" scored 90.0 against "Voveran" purely by being contained in it.

    Diclofenac is not pantoprazole, and three characters is what OCR leaves of a "Pan 40" strip
    often enough to matter. Below ``MIN_FUZZY_QUERY_CHARS`` every name is close to every other.
    """
    resolver = DrugResolver(db)
    assert await resolver.resolve("ran") is None

    # The bar is on *guessing* from a fragment, not on short names: a short brand spelled right
    # resolves on the exact tier, which never reaches the fuzzy guards.
    short = await resolver.resolve("Omez")
    assert short is not None
    assert len("omez") < MIN_FUZZY_QUERY_CHARS
    assert short.match_type == "exact_brand"


# --- what must keep working ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected_reference_id"),
    [
        # Prescription boilerplate: the reason WRatio's leniency is kept rather than swapped for
        # a length-sensitive scorer. Every one of these scores below 86 under fuzz.ratio alone.
        ("tab glycomet gp", "MET-GLM-1-500"),
        ("tab. glycomet", "MET-500"),
        ("cap omez 20", "OME-20"),
        ("glycomet 500 mg", "MET-500"),
        ("syp crocin", "PCM-500"),
        ("inj lasix", "FUR-40"),
        ("glycomet-gp", "MET-GLM-1-500"),
        # Ordinary single-character OCR misspellings, which are the whole point of the tier.
        ("glycomett", "MET-500"),
        ("metf0rmin", "MET-500"),
    ],
)
async def test_the_ocr_forms_the_fuzzy_tier_exists_for_still_resolve(
    db, query, expected_reference_id
):
    """A guard that bought its safety by refusing everything would pass the tests above."""
    resolved = await DrugResolver(db).resolve(query)
    assert resolved is not None, f"{query!r} was refused; the fuzzy tier exists for exactly this"
    assert resolved.reference_id == expected_reference_id


async def test_text_listing_two_separate_drugs_is_exempt_from_the_guard(db):
    """The other ambiguity, which has the opposite answer on purpose.

    "Amlodipine and Atenolol" is not unclear about what it says — it says two things, at disjoint
    places in the text. Stored as one unlinked chart row, resolving it to one molecule is a partial
    safety evaluation, and partial beats absent for data already in the record. That trade-off
    lives in ``test_drug_name_ambiguity`` and is asserted here only from the resolver's side, so
    that narrowing the exemption fails a test next to the guard it belongs to.
    """
    resolved = await DrugResolver(db).resolve("Amlodipine and Atenolol")
    assert resolved is not None
    assert resolved.match_type == "fuzzy"


async def test_a_brand_and_its_own_generic_are_one_candidate_not_two_rivals(db):
    """Rivalry is by reference id, not by name.

    "Telma" and "Telmisartan" are two entries in the candidate map and one drug. Comparing the
    runner-up by *name* would read every brand-plus-generic pair as a tie and refuse the whole
    vocabulary.
    """
    resolved = await DrugResolver(db).resolve("telmisartn")
    assert resolved is not None
    assert resolved.generic_name == "Telmisartan"


# --- the guards as properties rather than as examples -----------------------------------------


async def test_no_single_character_scan_error_resolves_to_the_wrong_drug(db):
    """The finding generalised: sweep every one-character OCR confusion over every seeded name.

    The examples above are two of the three misresolutions this sweep found; pinning the sweep
    itself is what keeps a vocabulary addition from quietly introducing a fourth. Names that
    refuse are not counted — refusing is the safe outcome and ``_current_meds`` surfaces it
    through ``check_unevaluated_medications``. Only *answering wrongly* fails.
    """
    resolver = DrugResolver(db)
    index = await resolver._load()

    # Confusions a printed Indian prescription actually produces: adjacent glyphs, and the
    # digit/letter pairs a scanner conflates.
    confusions = {
        "p": "r", "r": "n", "m": "rn", "n": "m", "l": "1", "1": "l", "0": "o", "o": "0",
        "i": "l", "g": "q", "c": "e", "e": "c", "s": "5", "b": "6", "t": "f", "u": "v",
        "y": "v", "d": "cl", "a": "o",
    }  # fmt: skip

    misresolved: list[tuple[str, str, str, str]] = []
    resolved_count = 0
    for name, row in index.candidates.items():
        for position, character in enumerate(name):
            replacement = confusions.get(character)
            if replacement is None:
                continue
            garbled = name[:position] + replacement + name[position + 1 :]
            got = await resolver.resolve(garbled)
            if got is None:
                continue
            resolved_count += 1
            if got.reference_id != row.reference_id:
                misresolved.append((name, garbled, got.reference_id, row.reference_id))

    assert not misresolved, (
        f"{len(misresolved)} scan error(s) resolved to a different drug: "
        + "; ".join(
            f"{n!r} scanned as {g!r} -> {bad} (should be {truth})"
            for n, g, bad, truth in misresolved[:5]
        )
    )
    # Guard against the vacuous pass: if the sweep stops resolving anything, it stops testing
    # anything. The measured figure at the time of writing is 614 of 782 garbled forms.
    assert resolved_count > 400, (
        f"only {resolved_count} garbled names resolved at all — the fuzzy tier has become so "
        "conservative that this sweep no longer exercises it"
    )


async def test_the_tie_margin_and_threshold_are_ordered_sensibly():
    """The cutoff the ranking pass uses must sit below the acceptance threshold by the margin.

    ``_fuzzy`` asks rapidfuzz for candidates at or above ``FUZZY_THRESHOLD - FUZZY_TIE_MARGIN`` so
    that every rival close enough to matter is in the result. Raising the margin past that gap
    without widening the cutoff would silently stop the tie check from seeing its rivals — the
    guard would still be there and would no longer guard anything.
    """
    assert 0 < FUZZY_TIE_MARGIN < FUZZY_THRESHOLD
    assert MIN_FUZZY_QUERY_CHARS >= 1
