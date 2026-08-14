"""Guideline RAG: what grounds a management option, and what the corpus loader will accept.

Two areas, both about the claim the product actually makes — that every management suggestion
is traceable to a cited guideline section.

**Grounding.** ``guideline_rag`` filters a model's citations down to section_ids that were
really retrieved, which is right. But the option's ``sufficient_support`` flag was taken from
the model even when that filter had removed every citation behind it, and ``synthesis`` reads
that flag to decide whether the option is shown at the case tier or escalated — so an option
with nothing behind it could be presented as adequately supported. And ``citation_faithfulness``
was computed over the citations that *survived* the filter, which makes it 1.0 by construction:
it could not detect the invented citation it exists to detect.

**Ingestion.** ``load_corpus`` reads hand-curated JSON and promises that one bad entry cannot
block the rest of the corpus. It enforced that by presence, not by type, so a file whose top
level was an object rather than an array, or a section whose ``content`` was written as a list
of paragraphs, raised instead of being skipped — and ``ingest`` runs from ``app.db.seed``, so
that failed startup seeding rather than one document.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agents import guideline_rag
from app.agents.context import ReasoningContext
from app.agents.guideline_rag import _faithfulness
from app.agents.llm import LLMClient
from app.agents.state import CaseState
from app.agents.synthesis import build_suggestions
from app.services.guideline_ingest import load_corpus


class _CannedLLM(LLMClient):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload

    def available(self) -> bool:
        return True

    def complete_json(self, system: str, user: str, *, retries: int = 2) -> dict[str, Any]:
        return self.payload


def _chunk(section_id: str = "ICMR-HTN-DX", **overrides: Any) -> dict[str, Any]:
    base = {
        "section_id": section_id,
        "source": "icmr",
        "document_title": "ICMR STW: Hypertension",
        "heading": "Diagnosis and first-line management",
        "content": "Confirm the diagnosis with repeat readings before starting therapy.",
        "score": 0.95,
        "corpus_version": "v1",
        "page_range": "12-13",
    }
    base.update(overrides)
    return base


async def _run(payload: dict[str, Any], chunks: list[dict] | None = None) -> CaseState:
    state = CaseState(patient_id="p1", presenting_complaint="High blood pressure")
    client = _CannedLLM(payload)
    ctx = ReasoningContext(llm=client, verifier_llm=client)
    ctx.retrieve = lambda q, k: list(chunks if chunks is not None else [_chunk()])
    await guideline_rag.run(state, ctx)
    return state


# ------------------------------------------------------------------- citation faithfulness


def test_faithfulness_falls_when_the_model_cites_a_section_that_was_not_retrieved():
    assert _faithfulness(["A", "B"], {"A", "B"}) == 1.0
    assert _faithfulness(["A", "INVENTED"], {"A", "B"}) == 0.5
    assert _faithfulness(["INVENTED"], {"A", "B"}) == 0.0


def test_faithfulness_of_no_claims_is_one_rather_than_a_division_by_zero():
    """The deterministic and demo paths cite by construction, claiming nothing to be unfaithful."""
    assert _faithfulness([], {"A"}) == 1.0
    assert _faithfulness([], set()) == 1.0


async def test_a_run_whose_every_citation_was_invented_does_not_report_perfect_faithfulness():
    """The metric was computed over post-filter citations, so it could only ever be 1.0.

    That is the one measurement that would show a model inventing guideline sections, and it
    reported perfect grounding for a run in which nothing was grounded at all.
    """
    state = await _run(
        {
            "options": [
                {
                    "text": "Guidelines support considering amlodipine 5mg once daily.",
                    "citation_section_ids": ["ICMR-FABRICATED-1", "ICMR-FABRICATED-2"],
                    "sufficient_support": True,
                }
            ]
        }
    )

    assert state.citation_faithfulness == 0.0


async def test_faithfulness_is_measured_across_every_option_not_just_the_last():
    state = await _run(
        {
            "options": [
                {"text": "Option A", "citation_section_ids": ["ICMR-HTN-DX"]},
                {"text": "Option B", "citation_section_ids": ["ICMR-FABRICATED"]},
            ]
        }
    )

    assert state.citation_faithfulness == 0.5


async def test_a_faithful_run_still_reports_one():
    state = await _run({"options": [{"text": "Option A", "citation_section_ids": ["ICMR-HTN-DX"]}]})

    assert state.citation_faithfulness == 1.0
    assert [c.section_id for c in state.management_options[0].citations] == ["ICMR-HTN-DX"]


# ------------------------------------------------------------------- grounding of an option


async def test_an_option_whose_citations_were_all_invented_is_not_sufficiently_supported():
    """The model may call its own option under-supported; it may not call an uncited one supported.

    Every citation the model named can be one it invented, leaving the option with no grounding.
    ``synthesis`` reads this flag to decide the option's autonomy tier, so asserting it True here
    put an ungrounded management suggestion in front of the clinician at the ordinary tier.
    """
    state = await _run(
        {
            "options": [
                {
                    "text": "Guidelines support considering amlodipine 5mg once daily.",
                    "citation_section_ids": ["ICMR-FABRICATED-1"],
                    "sufficient_support": True,
                }
            ]
        }
    )

    option = state.management_options[0]
    assert option.citations == []
    assert option.sufficient_support is False


async def test_an_ungrounded_option_is_escalated_to_flag_for_review_in_the_output():
    """End of the chain: the flag has to actually change what the clinician is shown."""
    state = await _run(
        {
            "options": [
                {
                    "text": "Guidelines support considering amlodipine 5mg once daily.",
                    "citation_section_ids": ["ICMR-FABRICATED-1"],
                    "sufficient_support": True,
                }
            ]
        }
    )
    state.autonomy_tier = "suggestive"

    management = [s for s in build_suggestions(state) if s["output_type"] == "management"]
    assert len(management) == 1
    assert management[0]["autonomy_tier"] == "flag_for_review"
    assert management[0]["citations"] == []


async def test_a_model_may_still_declare_its_own_cited_option_under_supported():
    """The flag is a floor, not an override — a downgrade from the model is respected."""
    state = await _run(
        {
            "options": [
                {
                    "text": "Guidelines support considering a thiazide.",
                    "citation_section_ids": ["ICMR-HTN-DX"],
                    "sufficient_support": False,
                }
            ]
        }
    )

    option = state.management_options[0]
    assert [c.section_id for c in option.citations] == ["ICMR-HTN-DX"]
    assert option.sufficient_support is False


async def test_a_cited_option_with_no_explicit_flag_is_supported():
    state = await _run(
        {
            "options": [
                {
                    "text": "Guidelines support considering a thiazide.",
                    "citation_section_ids": ["ICMR-HTN-DX"],
                }
            ]
        }
    )

    assert state.management_options[0].sufficient_support is True


async def test_only_the_invented_half_of_a_citation_list_is_dropped():
    """A clinician must never see a citation to a section the corpus did not return."""
    state = await _run(
        {
            "options": [
                {
                    "text": "Guidelines support considering lifestyle modification.",
                    "citation_section_ids": ["ICMR-FABRICATED", "ICMR-HTN-DX"],
                }
            ]
        }
    )

    option = state.management_options[0]
    assert [c.section_id for c in option.citations] == ["ICMR-HTN-DX"]
    assert option.sufficient_support is True
    assert state.citation_faithfulness == 0.5


async def test_a_chunk_below_the_retrieval_threshold_cannot_be_cited():
    """The threshold is what makes a citation mean something; a weak match is not grounding."""
    state = await _run(
        {"options": [{"text": "Option A", "citation_section_ids": ["ICMR-WEAK"]}]},
        chunks=[_chunk("ICMR-WEAK", score=0.10)],
    )

    assert state.management_options == []
    assert state.citation_faithfulness == 1.0


# ------------------------------------------------------------------- corpus ingestion


def _corpus_file(tmp_path, payload: object):
    path = tmp_path / "guidelines.json"
    path.write_text(json.dumps(payload))
    return path


_GOOD_DOC = {
    "source": "icmr",
    "document_title": "ICMR STW: Hypertension",
    "sections": [{"section_id": "ICMR-HTN-DX", "content": "Confirm with repeat readings."}],
}


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "icmr", "document_title": "T", "sections": []},  # object, not an array
        42,
        "a string",
        None,
    ],
)
def test_a_corpus_file_that_is_not_a_list_of_documents_is_skipped_not_fatal(tmp_path, payload):
    """``ingest`` runs from ``app.db.seed``, so raising here failed startup seeding."""
    assert load_corpus(_corpus_file(tmp_path, payload)) == []


def test_a_non_document_entry_is_skipped_and_the_documents_beside_it_survive(tmp_path):
    chunks = load_corpus(_corpus_file(tmp_path, ["icmr", None, 7, _GOOD_DOC]))

    assert [c["section_id"] for c in chunks] == ["ICMR-HTN-DX"]


@pytest.mark.parametrize(
    "sections",
    [
        {"a": {"section_id": "S1", "content": "x"}},  # object where a list belongs
        ["S1"],
        [None],
        "S1",
        None,
    ],
)
def test_malformed_sections_are_skipped_not_fatal(tmp_path, sections):
    doc = {"source": "icmr", "document_title": "T", "sections": sections}
    assert load_corpus(_corpus_file(tmp_path, [doc])) == []


@pytest.mark.parametrize("content", [["para one", "para two"], 123, {"text": "x"}, None, "", "   "])
def test_a_section_whose_content_is_not_text_is_skipped(tmp_path, content):
    """``(value or "").strip()`` raised on every one of these rather than rejecting it."""
    doc = {
        "source": "icmr",
        "document_title": "T",
        "sections": [{"section_id": "S1", "content": content}],
    }
    assert load_corpus(_corpus_file(tmp_path, [doc])) == []


@pytest.mark.parametrize("section_id", [{"x": 1}, 42, None, "", ["S1"]])
def test_a_section_id_that_is_not_a_string_is_skipped(tmp_path, section_id):
    """The section_id is the citation, and it reaches a String column and every cited option."""
    doc = {
        "source": "icmr",
        "document_title": "T",
        "sections": [{"section_id": section_id, "content": "Some guidance."}],
    }
    assert load_corpus(_corpus_file(tmp_path, [doc])) == []


def test_a_bad_section_does_not_cost_the_good_sections_in_the_same_document(tmp_path):
    doc = {
        "source": "icmr",
        "document_title": "T",
        "sections": [
            {"section_id": "S1", "content": ["a list of paragraphs"]},
            "not a section",
            {"section_id": "S2", "content": "Real guidance text."},
        ],
    }

    chunks = load_corpus(_corpus_file(tmp_path, [doc]))

    assert [c["section_id"] for c in chunks] == ["S2"]


def test_keywords_written_as_a_bare_string_become_one_keyword_not_a_list_of_letters(tmp_path):
    """``RetrievableChunk.of`` iterates this field, so a string became one keyword per character.

    Nothing raised — every lexical match against the section just quietly got worse.
    """
    doc = {
        "source": "icmr",
        "document_title": "T",
        "sections": [
            {"section_id": "S1", "content": "Guidance.", "keywords": "hypertension"},
        ],
    }

    chunks = load_corpus(_corpus_file(tmp_path, [doc]))

    assert chunks[0]["keywords"] == ["hypertension"]


@pytest.mark.parametrize(
    ("keywords", "expected"),
    [
        (["bp", "hypertension"], ["bp", "hypertension"]),
        (["bp", None, 7, "  ", "htn"], ["bp", "htn"]),
        (None, []),
        ({"a": "b"}, []),
        (42, []),
    ],
)
def test_keywords_are_normalised_to_a_list_of_non_empty_strings(tmp_path, keywords, expected):
    doc = {
        "source": "icmr",
        "document_title": "T",
        "sections": [{"section_id": "S1", "content": "Guidance.", "keywords": keywords}],
    }

    assert load_corpus(_corpus_file(tmp_path, [doc]))[0]["keywords"] == expected


def test_a_non_string_document_title_is_skipped_like_a_missing_one(tmp_path):
    doc = {"source": "icmr", "document_title": {"en": "T"}, "sections": _GOOD_DOC["sections"]}
    assert load_corpus(_corpus_file(tmp_path, [doc])) == []


def test_a_well_formed_document_is_still_ingested_unchanged(tmp_path):
    """Guard against the validation rejecting the corpus this actually ships with."""
    doc = {
        "source": "icmr",
        "document_title": "ICMR STW: Hypertension",
        "page_range": "12-13",
        "sections": [
            {
                "section_id": "ICMR-HTN-DX",
                "heading": "Diagnosis",
                "content": "Confirm the diagnosis with repeat readings.",
                "keywords": ["hypertension", "bp"],
            }
        ],
    }

    chunk = load_corpus(_corpus_file(tmp_path, [doc]))[0]

    assert chunk["section_id"] == "ICMR-HTN-DX"
    assert chunk["source"] == "icmr"
    assert chunk["document_title"] == "ICMR STW: Hypertension"
    assert chunk["heading"] == "Diagnosis"
    assert chunk["page_range"] == "12-13"
    assert chunk["content"] == "Confirm the diagnosis with repeat readings."
    assert chunk["keywords"] == ["hypertension", "bp"]
    assert chunk["token_estimate"] == 6  # whitespace-separated words in the content


def test_the_corpus_that_ships_with_the_repo_still_loads():
    """The real ``data/guidelines/`` files must survive the same validation."""
    chunks = load_corpus()

    assert chunks, "the shipped guideline corpus no longer loads"
    assert all(isinstance(c["section_id"], str) and c["section_id"] for c in chunks)
    assert all(isinstance(c["content"], str) and c["content"].strip() for c in chunks)
    assert all(isinstance(c["keywords"], list) for c in chunks)
    assert len({c["section_id"] for c in chunks}) == len(chunks), "duplicate citation ids"
