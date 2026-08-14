"""Precision/recall of the lexical retriever, measured against the corpus that ships.

The unit tests beside this one pin individual scoring rules. This module measures the thing
those rules exist to produce: given a realistic clinical query, does the retriever put the right
guideline document above the citation threshold, and does it keep the wrong ones below it.

That threshold is not a ranking nicety. ``guideline_rag`` retrieves k chunks, drops everything
under ``guideline_retrieval_threshold`` (0.75), and hands what survives to the model as the only
excerpts it may cite. A section above the line can ground a management option shown to a
clinician; a section below it cannot exist as far as the case is concerned. So the two numbers
worth measuring are:

  * **recall** — the query's own document is above the line at all, and ranked first;
  * **citation precision** — every section above the line belongs to a document the query is
    actually about.

Relevance is judged by document, not by section: the corpus is organised one document per
condition, and any section of the right condition is a defensible grounding for a query about
it. A section from a *different* condition is not, and that is what these tests count.

The benchmark queries are built the way ``guideline_rag._query`` builds the real one --
``"management of {leading diagnoses} | {presenting complaint}"`` -- so the measurement is of the
query shape the engine actually issues, not a tidied-up one.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.guideline import GuidelineChunk
from app.services.guideline_ingest import load_corpus
from app.services.guideline_service import lexical_score

THRESHOLD = settings.guideline_retrieval_threshold

# section_id prefix -> the condition its document covers. The retriever never sees this; it is
# the answer key. Prefixes rather than ids so a corpus that splits a section into ``-1``/``-2``
# pieces (see ``guideline_ingest.chunk_text``) stays labelled.
DOCUMENTS = {
    "ICMR-HTN": "hypertension",
    "ICMR-T2DM": "type 2 diabetes",
    "ICMR-CAP": "pneumonia",
    "ICMR-DENGUE": "dengue",
    "ICMR-ACS": "acute coronary syndrome",
    "WHO-MAL": "malaria",
    "NICE-AGE": "gastroenteritis",
}


def _document_of(section_id: str) -> str:
    for prefix, condition in DOCUMENTS.items():
        if section_id.startswith(prefix):
            return condition
    raise AssertionError(f"{section_id} belongs to no known document; update DOCUMENTS")


def _query(diagnoses: str, complaint: str) -> str:
    """The query shape ``guideline_rag._query`` produces for a case."""
    return f"management of {diagnoses} | {complaint}"


# (label, leading diagnoses, presenting complaint, the condition the case is about).
# Complaints are written the way a clinician writes them -- age, sex, duration, associated
# symptoms -- rather than as bare disease names, because that is what reaches the retriever.
CASES = [
    (
        "pneumonia",
        "Community-acquired pneumonia, Pulmonary tuberculosis",
        "45 year old man with fever and cough with sputum for five days, now breathless",
        "pneumonia",
    ),
    (
        "acs",
        "Acute coronary syndrome, Stable/unstable angina",
        "58 year old man with central chest pain radiating to the left arm for two hours",
        "acute coronary syndrome",
    ),
    (
        "dengue",
        "Dengue fever, Acute febrile illness",
        "high fever for four days with body ache and a falling platelet count",
        "dengue",
    ),
    (
        "diabetes",
        "Type 2 diabetes mellitus",
        "polyuria, polydipsia and weight loss over three months",
        "type 2 diabetes",
    ),
    (
        "hypertension",
        "Hypertension",
        "headache with high bp readings on two separate occasions",
        "hypertension",
    ),
    (
        "gastroenteritis",
        "Acute gastroenteritis",
        "loose stools and vomiting for two days",
        "gastroenteritis",
    ),
    (
        "malaria",
        "Malaria, Acute febrile illness",
        "fever with rigors and sweating after travel to a forested district",
        "malaria",
    ),
]


@pytest.fixture(scope="module")
def corpus():
    chunks = [
        GuidelineChunk(
            corpus_version="benchmark",
            source=rec["source"],
            document_title=rec["document_title"],
            section_id=rec["section_id"],
            heading=rec["heading"],
            content=rec["content"],
            page_range=rec["page_range"],
            keywords=rec["keywords"],
        )
        for rec in load_corpus()
    ]
    assert chunks, "the shipped guideline corpus no longer loads"
    from app.services.guideline_service import RetrievableChunk

    return [RetrievableChunk.of(c) for c in chunks]


def _cited(query: str, corpus, k: int = 6) -> list[dict]:
    """What ``guideline_rag`` would actually be allowed to cite for this query."""
    return [r for r in lexical_score(query, corpus, k) if r["score"] >= THRESHOLD]


# --- recall: the right document has to be retrievable at all -------------------------------


@pytest.mark.parametrize(
    ("label", "diagnoses", "complaint", "condition"),
    CASES,
    ids=[c[0] for c in CASES],
)
def test_the_case_document_is_above_the_citation_threshold(
    corpus, label, diagnoses, complaint, condition
):
    """Every case must be able to ground a management option in its own guideline."""
    cited = _cited(_query(diagnoses, complaint), corpus)

    assert cited, f"{label}: nothing cleared the citation threshold"
    assert condition in {_document_of(r["section_id"]) for r in cited}


@pytest.mark.parametrize(
    ("label", "diagnoses", "complaint", "condition"),
    CASES,
    ids=[c[0] for c in CASES],
)
def test_the_case_document_ranks_first(corpus, label, diagnoses, complaint, condition):
    """Being above the line is not enough — the model reads the excerpts in the order given."""
    results = lexical_score(_query(diagnoses, complaint), corpus, 6)

    assert results, f"{label}: retrieved nothing"
    assert _document_of(results[0]["section_id"]) == condition


# --- precision: nothing from another condition may be cited --------------------------------


@pytest.mark.parametrize(
    ("label", "diagnoses", "complaint", "condition"),
    CASES,
    ids=[c[0] for c in CASES],
)
def test_no_section_from_another_condition_is_citable(
    corpus, label, diagnoses, complaint, condition
):
    """A citation to the wrong condition's guideline is the failure this threshold exists for.

    This held before phrase-aware keyword weighting too, but only just: the gastroenteritis case
    scored both acute-coronary-syndrome sections at 0.625 — on the word "acute" and nothing else,
    since a single word of the keyword "acute coronary syndrome" was a full keyword hit. Two more
    incidental tokens would have carried them over. They now score 0.500, so what this asserts is
    a margin as well as a result; ``test_a_routine_note_...`` below is the case where those two
    extra tokens do turn up.
    """
    cited = _cited(_query(diagnoses, complaint), corpus)
    wrong = {r["section_id"]: _document_of(r["section_id"]) for r in cited}
    wrong = {sid: doc for sid, doc in wrong.items() if doc != condition}

    assert not wrong, f"{label} (about {condition}) could cite {wrong}"


# --- the query asks for management, so the management section has to be reachable -----------


@pytest.mark.parametrize(
    ("label", "diagnoses", "complaint", "condition"),
    CASES,
    ids=[c[0] for c in CASES],
)
def test_the_case_can_cite_its_own_management_section(
    corpus, label, diagnoses, complaint, condition
):
    """R34's known limitation, asserted as a requirement.

    ``guideline_rag`` exists to produce *management* options, and the excerpts it may cite are
    whatever cleared the threshold. Before section-intent scoring, the diagnosis section outscored
    the management section in every document that has both, and three of these seven cases --
    dengue, hypertension, malaria -- could not put their own management section over the line at
    all. The agent still emitted options, grounded in the only thing available: diagnosis text.
    "Guidelines support considering: Diagnosis and staging of hypertension..." is a citation to
    the wrong kind of section, and no threshold setting fixes it, because the section that should
    have won was ranked below one that should not have.
    """
    cited = {r["section_id"] for r in _cited(_query(diagnoses, complaint), corpus)}
    management = {sid for sid in cited if "MGMT" in sid}

    assert management, f"{label}: nothing that answers a management question is citable"
    assert all(_document_of(sid) == condition for sid in management)


def test_no_case_grounds_management_only_in_diagnosis_text(corpus):
    """The aggregate: every case, not most of them."""
    without = [
        label
        for label, diagnoses, complaint, _condition in CASES
        if not any("MGMT" in r["section_id"] for r in _cited(_query(diagnoses, complaint), corpus))
    ]

    assert not without, f"cases with no citable management section: {without}"


def test_citation_precision_and_recall_over_the_whole_benchmark(corpus):
    """The aggregate the individual cases add up to, asserted as one number each.

    Kept alongside the per-case tests because a scoring change that trades one case for another
    shows up here as a flat line and there as two moved tests.
    """
    cited_total = relevant_cited = cases_with_a_citation = 0
    for _label, diagnoses, complaint, condition in CASES:
        cited = _cited(_query(diagnoses, complaint), corpus)
        cited_total += len(cited)
        relevant_cited += sum(1 for r in cited if _document_of(r["section_id"]) == condition)
        cases_with_a_citation += bool({_document_of(r["section_id"]) for r in cited} & {condition})

    precision = relevant_cited / cited_total
    recall = cases_with_a_citation / len(CASES)

    assert precision == 1.0, f"citation precision fell to {precision:.2f}"
    assert recall == 1.0, f"citation recall fell to {recall:.2f}"


# --- a noisy but entirely ordinary note ----------------------------------------------------

# Everything a clinician routinely records that is not the complaint: pertinent negatives,
# family history, vitals, prior normal investigations. None of it is about the presenting
# problem, and all of it reaches the retriever, because ``_query`` passes the complaint through
# whole.
ROUTINE_NOTE = (
    " Patient is a 34 year old man, non-smoker, no known drug allergy. Family history of "
    "diabetes in mother and hypertension in father. He works as a driver and travels often. "
    "No chest pain, no breathlessness, no fever, no bleeding. Blood pressure 118/76, pulse 88 "
    "regular. Previous review noted a normal ECG and a normal chest radiograph. He takes no "
    "regular therapy. Assess risk and severity, review adherence, consider first-line options "
    "and target organ damage."
)

_NOISY_QUERY = _query("Acute gastroenteritis", "loose stools and vomiting for two days") + (
    ROUTINE_NOTE
)


def test_a_documented_negative_does_not_retrieve_its_own_guideline(corpus):
    """ "No chest pain" must not be evidence *for* the chest-pain guideline.

    The clinician wrote that the finding is absent. Scored as plain tokens it was a full keyword
    hit on "chest pain", which is a keyword of both the acute-coronary-syndrome sections and the
    hypertensive-emergency referral section.
    """
    absent = lexical_score("fever with rigors, no chest pain and no breathlessness", corpus, 10)
    present = lexical_score("fever with rigors, chest pain and breathlessness", corpus, 10)

    cardiac = {"acute coronary syndrome"}
    assert cardiac & {_document_of(r["section_id"]) for r in present}
    assert not cardiac & {_document_of(r["section_id"]) for r in absent}


def test_a_routine_note_does_not_make_most_of_the_corpus_citable(corpus):
    """Query length must not, on its own, buy a section its way past the threshold.

    ``raw`` grows with the number of query tokens that match anything, and the threshold is a
    fixed cut on it, so a longer note lifts every chunk at once. Measured on the shipped corpus,
    the note below used to put 12 of 15 sections over the line — hypertension, diabetes and
    coronary-syndrome sections all citable for a gastroenteritis case — with the correct section
    only fifth. Phrase-aware keywords and dropping the documented negatives bring that to 7 of
    15, third.

    Seven is still too many, and this asserts a bound rather than a clean result. What is left is
    the mother's diabetes and the father's hypertension read as though they were the patient's
    own, plus the generic vocabulary any long note shares with any guideline ("assess", "risk",
    "review", "first-line"). Neither is something a bag-of-words retriever can see; both are what
    the dense retriever this one stands in for is meant to handle.
    """
    results = lexical_score(_NOISY_QUERY, corpus, len(corpus))
    cited = [r for r in results if r["score"] >= THRESHOLD]

    assert len(cited) <= len(corpus) // 2, (
        f"{len(cited)} of {len(corpus)} sections citable from one routine note"
    )
    # The case's own document must survive all that noise near the top.
    top3 = {_document_of(r["section_id"]) for r in results[:3]}
    assert "gastroenteritis" in top3


def test_the_noisy_note_still_ranks_its_own_condition_above_the_distractors(corpus):
    """Whatever else the noise drags over the line, it must not be a rival *management* option.

    ``guideline_rag`` turns retrieved chunks into management options, so the section that decides
    what the clinician is offered is the one that outranks the other conditions' management
    sections. The distractors the noise pulls in are diagnosis and monitoring sections; this pins
    that the case's own management section stays ahead of every competing one.
    """
    results = lexical_score(_NOISY_QUERY, corpus, len(corpus))
    by_id = {r["section_id"]: r["score"] for r in results}

    assert by_id["NICE-AGE-MGMT"] >= THRESHOLD
    for other in ("ICMR-T2DM-MGMT", "ICMR-CAP-MGMT", "WHO-MAL-MGMT", "ICMR-DENGUE-MGMT"):
        assert by_id.get(other, 0.0) < by_id["NICE-AGE-MGMT"], other
