"""What counts as a keyword hit in the lexical retriever.

The lexical retriever is not a fallback in practice — it is what runs whenever Qdrant and the
embedding model are absent, which is every offline deployment and every test. It scores a query
against a chunk as ``1.5 * keyword_hits + body_overlap``, saturated to 0..1, and the reasoning
engine only cites a chunk that clears ``guideline_retrieval_threshold`` (0.75). So a keyword hit
is worth roughly three body-token hits, and three of them alone put a section into a management
option's citations.

Keyword matching was a substring test: ``any(t in k2 for k2 in keywords)``, true for any run of
characters anywhere inside a keyword. "ten" matched "hypertension"; "men" matched "management",
which is a keyword on nearly every management section in the corpus; "tension" matched
"hypertension" and every antihypertensive keyword beside it. A query token that means nothing
clinically could therefore ground a management option in an unrelated guideline section.

A hit now means the query named the concept: the token is a word of the keyword, or that word
give or take a short inflectional suffix ("platelet" ~ "platelets", "monitor" ~ "monitoring").
The tests below pin both directions — the spurious matches are gone, and the real ones, including
the shipped corpus's own retrievals, still work.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.guideline import GuidelineChunk
from app.services.guideline_ingest import load_corpus
from app.services.guideline_service import (
    RetrievableChunk,
    _is_inflection,
    lexical_score,
)

VERSION = "v-lexical-test"


def _chunk(
    section_id: str, *, content: str, keywords: list[str], heading: str = "Management"
) -> RetrievableChunk:
    return RetrievableChunk.of(
        GuidelineChunk(
            corpus_version=VERSION,
            source="icmr",
            document_title="Standard Treatment Workflow",
            section_id=section_id,
            heading=heading,
            content=content,
            page_range="1-2",
            keywords=keywords,
        )
    )


def _ids(results: list[dict]) -> list[str]:
    return [r["section_id"] for r in results]


# --- spurious substrings are not hits -----------------------------------------------------


def test_a_substring_of_a_keyword_is_not_a_keyword_hit():
    """The R32 example: "ten" is not a claim about hypertension."""
    htn = _chunk(
        "htn",
        content="Confirm the diagnosis with repeat readings on two separate visits.",
        keywords=["hypertension"],
    )

    assert lexical_score("ten", [htn], 5) == []


def test_a_spurious_hit_can_no_longer_clear_the_retrieval_threshold_on_its_own():
    """Keywords cluster around one concept, so one bad token used to hit all of them.

    "tension" was a substring of all three keywords below — three hits, raw 4.5, score 0.75,
    exactly the threshold at which the reasoning engine will cite a section.
    """
    htn = _chunk(
        "htn-mgmt",
        content="Start amlodipine and review the response after four weeks.",
        keywords=["hypertension", "hypertensive urgency", "antihypertensive"],
    )

    assert settings.guideline_retrieval_threshold <= 0.75  # what the old score reached
    assert lexical_score("tension", [htn], 5) == []


def test_a_word_that_merely_starts_with_a_keyword_is_not_a_hit():
    """A shared prefix is not an inflection — these are different concepts."""
    cardio = _chunk(
        "cmp",
        content="Refer for echocardiography before starting therapy.",
        keywords=["cardiomyopathy"],
    )
    hypo = _chunk("hypo", content="Give oral glucose.", keywords=["hypoglycaemia"])

    assert lexical_score("cardio", [cardio], 5) == []
    assert lexical_score("hypo", [hypo], 5) == []


@pytest.mark.parametrize(
    ("token", "keyword"),
    [
        ("ten", "hypertension"),  # too short to carry a concept
        ("art", "arthritis"),
        ("ana", "anaemia"),
        ("men", "management"),  # a keyword on nearly every management section
        ("tension", "hypertension"),  # not a prefix — a word inside another word
        ("emia", "anaemia"),
        ("thyroid", "thyroidectomy"),  # six characters past the stem — a different concept
    ],
)
def test_known_spurious_pairs_are_rejected(token, keyword):
    chunk = _chunk("s", content="Unrelated clinical guidance text.", keywords=[keyword])
    assert lexical_score(token, [chunk], 5) == []


# --- real matches still hit ---------------------------------------------------------------


def test_the_exact_keyword_still_hits():
    htn = _chunk("htn", content="Repeat readings on two visits.", keywords=["hypertension"])

    (result,) = lexical_score("hypertension", [htn], 5)

    assert result["section_id"] == "htn"
    assert result["score"] > 0


def test_a_word_of_a_multi_word_keyword_phrase_still_hits():
    """Keywords are curated as phrases; the query is scored token by token."""
    dengue = _chunk(
        "dengue",
        content="Monitor for warning signs and admit if they appear.",
        keywords=["dengue fever", "warning signs"],
    )

    assert _ids(lexical_score("dengue", [dengue], 5)) == ["dengue"]
    assert _ids(lexical_score("warning", [dengue], 5)) == ["dengue"]


@pytest.mark.parametrize(
    ("query", "keyword"),
    [
        ("platelet", "platelets"),  # singular query, plural keyword
        ("platelets", "platelet"),  # and the other way round
        ("fevers", "fever"),
        ("monitoring", "monitor"),
        ("referral", "referrals"),
    ],
)
def test_an_inflection_of_a_keyword_still_hits(query, keyword):
    chunk = _chunk("s", content="Unrelated clinical guidance text.", keywords=[keyword])
    assert _ids(lexical_score(query, [chunk], 5)) == ["s"]


def test_the_section_that_names_the_concept_outranks_one_that_merely_contains_the_letters():
    named = _chunk(
        "named",
        content="Tension-type headache is managed with simple analgesia.",
        keywords=["tension-type headache"],
    )
    lettered = _chunk(
        "lettered",
        content="Start amlodipine and review after four weeks.",
        keywords=["hypertension", "amlodipine"],
    )

    assert _ids(lexical_score("tension headache", [lettered, named], 5)) == ["named"]


# --- the derived token set ----------------------------------------------------------------


def test_keyword_phrases_are_split_into_words_when_a_chunk_is_read():
    chunk = _chunk("s", content="Body.", keywords=["Dengue Fever", "warning-signs", "bp"])

    assert chunk.keyword_tokens == frozenset({"dengue", "fever", "warning", "signs"})
    assert chunk.keywords == ("dengue fever", "warning-signs", "bp")  # kept, lower-cased


def test_a_directly_constructed_chunk_derives_its_own_keyword_tokens():
    """Nothing that builds one of these by hand should silently lose every keyword hit."""
    chunk = RetrievableChunk(
        section_id="s",
        source="icmr",
        document_title="doc",
        heading=None,
        content="Body.",
        page_range=None,
        corpus_version=VERSION,
        keywords=("dengue fever",),
        body_tokens=frozenset(),
    )

    assert chunk.keyword_tokens == frozenset({"dengue", "fever"})
    assert _ids(lexical_score("dengue", [chunk], 5)) == ["s"]


def test_a_chunk_with_no_keywords_scores_on_body_overlap_alone():
    chunk = _chunk("s", content="Amlodipine is an appropriate first-line agent.", keywords=[])

    assert chunk.keyword_tokens == frozenset()
    assert _ids(lexical_score("amlodipine", [chunk], 5)) == ["s"]


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("fever", "fevers", True),
        ("monitor", "monitoring", True),
        ("act", "acts", False),  # below the minimum stem length
        ("ten", "tens", False),
        ("cardio", "cardiomyopathy", False),  # four characters past the stem
        ("tension", "hypertension", False),  # not a prefix at all
        ("fever", "fever", True),
    ],
)
def test_the_inflection_rule_is_bounded_at_both_ends(a, b, expected):
    assert _is_inflection(a, b) is expected
    assert _is_inflection(b, a) is expected  # order must not matter


# --- against the corpus that actually ships ------------------------------------------------


@pytest.fixture(scope="module")
def shipped_corpus() -> list[RetrievableChunk]:
    chunks = [
        _chunk(
            rec["section_id"],
            content=rec["content"],
            keywords=rec["keywords"],
            heading=rec["heading"] or "",
        )
        for rec in load_corpus()
    ]
    assert chunks, "the shipped guideline corpus no longer loads"
    return chunks


def test_a_meaningless_token_no_longer_changes_what_the_corpus_returns(shipped_corpus):
    """The token "men" was a substring of "management" — a keyword on nearly every section.

    Adding it to a query used to pull in every one of them; it must now be inert.
    """
    without = lexical_score("chest pain", shipped_corpus, 10)
    with_noise = lexical_score("chest pain in men", shipped_corpus, 10)

    assert _ids(with_noise) == _ids(without)


@pytest.mark.parametrize(
    ("query", "expected_prefix"),
    [
        ("dengue fever with falling platelets", "ICMR-DENGUE"),
        ("metformin for type 2 diabetes", "ICMR-T2DM"),
        ("amoxicillin for community acquired pneumonia", "ICMR-CAP"),
        ("artemisinin for malaria", "WHO-MAL"),
        ("oral rehydration for gastroenteritis", "NICE-AGE"),
        ("amlodipine for high blood pressure", "ICMR-HTN"),
    ],
)
def test_realistic_clinical_queries_still_retrieve_their_own_section(
    shipped_corpus, query, expected_prefix
):
    """The precision fix must not cost recall on the retrievals the product depends on."""
    results = lexical_score(query, shipped_corpus, 5)

    assert results, f"{query!r} retrieved nothing"
    assert results[0]["section_id"].startswith(expected_prefix)
    assert results[0]["score"] >= settings.guideline_retrieval_threshold
