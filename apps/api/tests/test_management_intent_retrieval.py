"""Section-intent metadata and query-intent matching in the lexical retriever.

``guideline_rag`` asks exactly one question — "management of {leading diagnoses} | {complaint}" —
and until now the retriever could not hear the "management" part of it. "management" is a
stopword in scoring (it has to be: it is also a curated keyword on nearly every management
section, so scoring it as a plain token lifted all of them at once), which left the query as
diagnosis names plus the presenting complaint. That is symptom vocabulary, and symptom vocabulary
is what *diagnosis* sections are keyed on; management sections are keyed on therapy vocabulary
the query never contains. The ranking within the correct document therefore came out backwards,
and management options ended up grounded in diagnosis text.

The retriever now scores intent as evidence, under two gates that are what make it safe:
the section must already have lexical evidence for the query, and its *document* must already be
citable without any intent credit. ``test_retrieval_quality_benchmark`` measures the effect on
the shipped corpus; these tests pin the rules that produce it.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.guideline import GuidelineChunk
from app.services.guideline_service import (
    RetrievableChunk,
    is_management_intent,
    lexical_score,
)

THRESHOLD = settings.guideline_retrieval_threshold
VERSION = "v-intent-test"


def _chunk(
    section_id: str,
    *,
    heading: str,
    content: str,
    keywords: list[str],
    document_title: str = "Standard Treatment Workflow: Dengue Fever",
    source: str = "icmr",
) -> RetrievableChunk:
    return RetrievableChunk.of(
        GuidelineChunk(
            corpus_version=VERSION,
            source=source,
            document_title=document_title,
            section_id=section_id,
            heading=heading,
            content=content,
            page_range="1-2",
            keywords=keywords,
        )
    )


def _scores(query: str, chunks: list[RetrievableChunk]) -> dict[str, float]:
    return {r["section_id"]: r["score"] for r in lexical_score(query, chunks, len(chunks))}


# --- what makes a query a management query -------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "management of dengue fever",
        "Management of acute gastroenteritis",
        "treatment of uncomplicated malaria",
        "which antibiotic to prescribe for pneumonia",  # "prescribing" family
        "first-line therapy in type 2 diabetes",
        "antihypertensive regimen for stage 2 hypertension",
    ],
)
def test_a_query_asking_what_to_do_is_management_intent(query):
    assert is_management_intent(query)


@pytest.mark.parametrize(
    "query",
    [
        "diagnosis of malaria",
        "recognition of acute coronary syndrome",
        "warning signs in dengue",
        "fever with rigors and sweating after travel to a forested district",
    ],
)
def test_a_query_asking_what_this_is_is_not_management_intent(query):
    assert not is_management_intent(query)


def test_a_therapy_the_clinician_recorded_as_absent_does_not_declare_management_intent():
    """ "He takes no regular therapy" is a documented negative, not a request for options.

    Intent is read from the same un-negated text scoring reads, so the one place clinicians
    routinely write treatment vocabulary *about an absence* cannot flip a diagnostic query into a
    management one.
    """
    assert not is_management_intent("fever for two days, he takes no regular therapy")
    assert is_management_intent("fever for two days, review his regular therapy")


# --- what makes a section a management section ---------------------------------------------


def test_the_heading_names_the_section_intent():
    mgmt = _chunk("X-1", heading="Fluid management in dengue", content="c", keywords=[])
    dx = _chunk("X-2", heading="Warning signs and monitoring in dengue", content="c", keywords=[])

    assert mgmt.is_management
    assert not dx.is_management


def test_the_section_id_convention_survives_a_reworded_heading():
    """A curator who renames the heading must not silently drop the section's intent."""
    chunk = _chunk("ICMR-DENGUE-MGMT", heading="Fluids and antipyretics", content="c", keywords=[])

    assert chunk.is_management


def test_a_curated_keyword_names_the_section_intent():
    chunk = _chunk("X-3", heading="Second line options", content="c", keywords=["management"])

    assert chunk.is_management


def test_treatment_discussed_inside_a_diagnosis_section_does_not_make_it_one():
    """Guideline prose mentions therapy everywhere; only the section's own metadata decides.

    Reading the body would classify most of the corpus as management sections, which would make
    the intent signal worthless — it would no longer distinguish anything.
    """
    chunk = _chunk(
        "ICMR-DENGUE-WARN",
        heading="Warning signs and monitoring in dengue",
        content=(
            "Admit if warning signs are present. Treatment is supportive; intravenous fluid "
            "therapy is indicated once haematocrit rises."
        ),
        keywords=["dengue", "warning signs"],
    )

    assert not chunk.is_management


# --- the promotion itself, and the gates on it ---------------------------------------------

DX = _chunk(
    "DOC-A-DX",
    heading="Diagnosis and staging of hypertension",
    content="Confirm hypertension with readings on two separate occasions. Look for headache.",
    keywords=["hypertension", "high blood pressure", "headache"],
    document_title="STW: Hypertension",
)
MGMT = _chunk(
    "DOC-A-MGMT",
    heading="Pharmacological management of hypertension",
    content="Start amlodipine or enalapril. Step up to a thiazide if uncontrolled.",
    keywords=["hypertension", "amlodipine", "enalapril", "thiazide", "management"],
    document_title="STW: Hypertension",
)
# A different condition, whose own document nothing in the query confirms.
OTHER_MGMT = _chunk(
    "DOC-B-MGMT",
    heading="Glycaemic management of type 2 diabetes",
    content="Start metformin. Add a sulfonylurea such as glimepiride if HbA1c remains high.",
    keywords=["diabetes", "metformin", "glimepiride", "management"],
    document_title="STW: Type 2 Diabetes",
)
CORPUS = [DX, MGMT, OTHER_MGMT]

_MANAGEMENT_QUERY = "management of Hypertension | headache with high blood pressure readings"
# The same query with the intent word taken off. "management" is a stopword in scoring, so these
# two carry *identical* topical evidence and differ only in the intent they declare -- which is
# what makes them a controlled pair.
_NEUTRAL_QUERY = "Hypertension | headache with high blood pressure readings"


def test_a_management_query_can_cite_the_management_section_of_its_own_condition():
    """The R34 defect, at unit scale: the section that answers the question must be citable."""
    scores = _scores(_MANAGEMENT_QUERY, CORPUS)

    assert scores["DOC-A-MGMT"] >= THRESHOLD


def test_the_same_section_is_not_promoted_for_a_query_that_did_not_ask_for_management():
    """Intent evidence is evidence *of a match*, so it may only count when the intents match."""
    managed = _scores(_MANAGEMENT_QUERY, CORPUS)
    neutral = _scores(_NEUTRAL_QUERY, CORPUS)

    assert managed["DOC-A-MGMT"] > neutral["DOC-A-MGMT"]
    # The diagnosis section is scored on topical evidence alone either way.
    assert managed["DOC-A-DX"] == neutral["DOC-A-DX"]


def test_a_management_section_of_an_unconfirmed_document_is_not_promoted():
    """The gate that keeps citation precision intact.

    ``DOC-B-MGMT`` is a management section, and the query is a management query, so it clears
    every test but the one that matters: nothing in the query put *its document* over the
    citation threshold. Being about the right kind of question is not being about the case.
    """
    scores = _scores(
        "management of Hypertension | headache, glimepiride reviewed and stopped last year",
        CORPUS,
    )

    assert scores.get("DOC-B-MGMT", 0.0) < THRESHOLD


def test_promotion_cannot_make_a_new_document_citable():
    """Stated as the invariant, over the whole shipped corpus and every benchmark query.

    Citation precision is judged per document — any section of the right condition grounds a
    management option defensibly, no section of a different condition does — so this is the
    property that says the intent weight is a recall knob and nothing else. It holds by
    construction (promotion is gated on the document already being confirmed); the test is here
    so that a later change to the gate cannot quietly turn it into a precision knob.
    """
    from app.services.guideline_ingest import load_corpus

    corpus = [
        RetrievableChunk.of(GuidelineChunk(corpus_version=VERSION, **rec))
        for rec in ({k: v for k, v in r.items() if k != "token_estimate"} for r in load_corpus())
    ]
    assert corpus, "the shipped guideline corpus no longer loads"

    queries = [
        "management of Dengue fever | high fever for four days with a falling platelet count",
        "management of Hypertension | headache with high bp readings",
        "management of Malaria | fever with rigors after travel to a forested district",
        "management of Acute gastroenteritis | loose stools and vomiting for two days",
        "management of Community-acquired pneumonia | fever and cough with sputum",
    ]
    for query in queries:
        promoted = lexical_score(query, corpus, len(corpus))
        # Re-scored with the promotion made unreachable: no document is confirmed, so no section
        # can be promoted, leaving pure topical scores.
        topical = lexical_score(query, corpus, len(corpus), threshold=1.1)

        def _docs(results):
            return {(r["source"], r["document_title"]) for r in results if r["score"] >= THRESHOLD}

        assert _docs(promoted) == _docs(topical), query


def test_promotion_does_not_reorder_two_management_sections_of_the_same_document():
    """It is a constant, so it moves every promotable section by the same amount.

    A weaker management section cannot overtake a stronger one within a document it shares, which
    is what keeps the excerpt order ``guideline_rag`` hands the model meaningful.
    """
    strong = _chunk(
        "DOC-A-MGMT1",
        heading="Pharmacological management of hypertension",
        content="Start amlodipine, enalapril or a thiazide for hypertension.",
        keywords=["hypertension", "high blood pressure", "amlodipine", "management"],
        document_title="STW: Hypertension",
    )
    weak = _chunk(
        "DOC-A-MGMT2",
        heading="Management of resistant hypertension",
        content="Refer for specialist review.",
        keywords=["hypertension", "management"],
        document_title="STW: Hypertension",
    )
    corpus = [DX, strong, weak]

    topical = _scores(_NEUTRAL_QUERY, corpus)
    promoted = _scores(_MANAGEMENT_QUERY, corpus)

    assert topical["DOC-A-MGMT1"] > topical["DOC-A-MGMT2"]
    assert promoted["DOC-A-MGMT1"] > promoted["DOC-A-MGMT2"]


def test_a_management_section_with_no_lexical_overlap_at_all_stays_uncited():
    """Intent alone is not evidence: the section still has to be about something in the query."""
    unrelated = _chunk(
        "DOC-A-MGMT3",
        heading="Management of scorpion envenomation",
        content="Prazosin is the treatment of choice.",
        keywords=["scorpion", "prazosin"],
        document_title="STW: Hypertension",  # same document, deliberately
    )
    scores = _scores(_MANAGEMENT_QUERY, [DX, MGMT, unrelated])

    assert "DOC-A-MGMT3" not in scores
