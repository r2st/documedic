"""The last uncovered branches: parser guards, retired vocabulary rows, and optional embedding.

These are the residual lines from the full-suite coverage report after the other gap files
landed. Several are defence-in-depth guards that ``parse_text`` cannot currently reach from the
outside — they are unit-tested against the guarded function directly rather than left unasserted,
because the guard is the contract each parser owes its caller.
"""

from __future__ import annotations

import sys
import types
import uuid

import pytest
from sqlalchemy import select

from app.models.medication_event import MedicationEvent
from app.models.user import Account
from app.services import dense_retrieval, guideline_ingest
from app.services.extraction import text_parser
from app.services.extraction.text_parser import (
    _parse_allergy,
    _parse_condition,
    _parse_lab,
    _parse_medication,
    parse_text,
)
from app.services.safety_service import SafetyService
from app.services.validation_service import ValidationService
from tests.conftest import create_patient


async def _account_id(db) -> uuid.UUID:
    account_id = (await db.execute(select(Account.id))).scalars().first()
    assert account_id is not None
    return account_id


# --------------------------------------------------------------- parser alias guards
# Every parser rejects a line whose payload is really a section header. Lab printouts repeat
# their headers per page and mid-table, so without these guards a document yields entities named
# "Labs", "Rx" or "Diagnosis" that then flow into the patient graph as real clinical data.


@pytest.mark.parametrize("alias", ["Rx", "medications", "Prescription", "Drugs"])
def test_medication_parser_rejects_a_line_whose_name_is_a_section_alias(alias):
    assert _parse_medication(f"{alias} 500mg BD") is None


def test_medication_parser_still_accepts_a_real_prescription_line():
    parsed = _parse_medication("Glycomet 500mg BD")
    assert parsed is not None
    assert parsed.entity_type == "medication"
    assert parsed.fields[0].value == "Glycomet"


@pytest.mark.parametrize("alias", ["Results", "Investigations", "Haematology"])
def test_lab_parser_rejects_a_line_whose_marker_is_a_section_alias(alias):
    assert _parse_lab(f"{alias}: 5.2 mg/dL") is None


def test_lab_parser_still_accepts_a_real_result_line():
    parsed = _parse_lab("Creatinine: 3.0 mg/dL (0.6-1.2)")
    assert parsed is not None
    assert parsed.fields[0].value == "Creatinine"


@pytest.mark.parametrize("alias", ["conditions", "Diagnosis", "problems"])
def test_condition_parser_rejects_a_bare_section_alias(alias):
    assert _parse_condition(alias) is None
    assert _parse_condition(f"- {alias}") is None


def test_condition_parser_still_accepts_a_real_diagnosis():
    parsed = _parse_condition("- Type 2 Diabetes Mellitus")
    assert parsed is not None
    assert parsed.fields[0].value == "Type 2 Diabetes Mellitus"


@pytest.mark.parametrize("alias", ["allergies", "Allergy"])
def test_allergy_parser_rejects_a_bare_section_alias(alias):
    assert _parse_allergy(alias) is None
    assert _parse_allergy(f"• {alias}") is None


def test_allergy_parser_still_accepts_a_real_allergy():
    parsed = _parse_allergy("Penicillin - rash")
    assert parsed is not None
    assert parsed.fields[0].value == "Penicillin"


def test_a_recognised_section_with_no_parser_skips_its_lines_instead_of_crashing(monkeypatch):
    """Adding a section alias without a matching parser must degrade, not raise.

    ``_SECTION_ALIASES`` and ``_PARSERS`` are two tables that have to stay in step; this asserts
    the mismatch is survivable, because the failure mode otherwise is a 500 on document upload.
    """
    monkeypatch.setitem(text_parser._SECTION_ALIASES, "vitals", "vitals")

    entities = parse_text("Vitals\nBP 130/80\nPulse 78\nMedications\nGlycomet 500mg BD")

    # The vitals lines are skipped; parsing resumes correctly at the next known section.
    assert [e.entity_type for e in entities] == ["medication"]
    assert entities[0].fields[0].value == "Glycomet"


# --------------------------------------------------------------- retired vocabulary rows


async def test_active_flags_skips_a_medication_whose_vocabulary_entry_was_retired(db, auth_client):
    """A deactivated DrugVocabulary row must drop out of the flag sweep, not raise.

    ``_build_context`` links medications by primary key (no ``is_active`` filter), while the
    resolver only returns active rows. Retiring a vocabulary entry therefore leaves live
    medication events pointing at a reference id the resolver will not return — that mismatch
    has to be skipped rather than crash the safety endpoint.
    """
    patient = await create_patient(auth_client)
    patient_id = uuid.UUID(patient["id"])
    account_id = await _account_id(db)

    from app.models.drug_vocabulary import DrugVocabulary

    retired = DrugVocabulary(
        brand_name="Retiredbrand",
        generic_name="Retiredgeneric",
        reference_id=f"ref-{uuid.uuid4().hex}",
        is_active=False,
    )
    db.add(retired)
    await db.flush()
    db.add(
        MedicationEvent(
            patient_id=patient_id,
            drug_vocabulary_id=retired.id,
            generic_name="Retiredgeneric",
            event_type="continue",
            is_current=True,
        )
    )
    await db.flush()

    results = await SafetyService(db).active_flags(account_id=account_id, patient_id=patient_id)

    assert all(vocab.reference_id != retired.reference_id for vocab, _ in results)


# --------------------------------------------------------------- validation harness


async def test_a_vignette_with_no_intake_questions_proceeds_straight_to_reasoning(
    db, auth_client, monkeypatch
):
    """When triage asks nothing, the harness must stop polling and run the pipeline.

    Left looping it would re-submit empty answer sets four times per vignette, which both slows
    the run and writes meaningless audit rows.
    """
    account_id = await _account_id(db)
    service = ValidationService(db)

    async def _no_questions(_session_id):
        return []

    submitted: list[object] = []

    async def _record_submit(*args, **kwargs):  # pragma: no cover — asserted not to run
        submitted.append(args)
        raise AssertionError("submit_answers must not be called when nothing is pending")

    monkeypatch.setattr(service.reasoning, "pending_questions", _no_questions)
    monkeypatch.setattr(service.reasoning, "submit_answers", _record_submit)

    from app.services.validation_service import load_vignettes

    vignette = load_vignettes()[0]
    result = await service._run_vignette(account_id, vignette)

    assert submitted == []
    assert isinstance(result, dict)
    assert result  # the vignette was scored despite the empty intake


# --------------------------------------------------------------- optional embedding model


def test_corpus_chunks_are_embedded_by_the_same_model_the_query_side_uses(monkeypatch):
    """Corpus and query vectors are only comparable if one model produced both.

    ``guideline_ingest`` used to build its own ``SentenceTransformer`` with its own hardcoded
    name, which is precisely how the two drift apart -- and a mismatch is silent, because
    ``cosine`` scores differing lengths 0 rather than raising. Both sides go through
    ``dense_retrieval._load_model`` now, so this asserts the constant that names it.
    """
    dense_retrieval.reset()
    encoded: dict = {}

    class _FakeModel:
        def __init__(self, name):
            encoded["model"] = name

        @staticmethod
        def encode(texts, normalize_embeddings=False):
            encoded["texts"] = list(texts)
            encoded["normalised"] = normalize_embeddings
            return [[0.3, 0.4] for _ in texts]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=_FakeModel),
    )

    out = dense_retrieval.embed_documents(["hydration guidance", "platelet monitoring"])

    assert encoded["model"] == dense_retrieval.EMBEDDING_MODEL
    assert encoded["texts"] == ["hydration guidance", "platelet monitoring"]
    # Unit-normalised on the way out, so ``cosine`` is a plain dot product on both sides.
    assert out == [(0.6, 0.8), (0.6, 0.8)]
    dense_retrieval.reset()


def test_a_batch_that_cannot_be_normalised_is_dropped_whole(monkeypatch):
    """All or nothing: a partial batch would leave some chunks un-rerankable with no way for the
    caller to tell which, and ``ingest`` would write NULL for them without saying so."""
    dense_retrieval.reset()

    class _FakeModel:
        def __init__(self, name):
            pass

        @staticmethod
        def encode(texts, normalize_embeddings=False):
            return [[1.0, 0.0], [0.0, 0.0]]  # the second has no direction to normalise

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=_FakeModel),
    )

    assert dense_retrieval.embed_documents(["good", "degenerate"]) is None
    dense_retrieval.reset()


async def test_ingest_pushes_to_qdrant_only_when_embeddings_were_produced(db, monkeypatch):
    """No embedding model means no vector push — ingestion must still persist to PostgreSQL."""
    pushed: list[str] = []
    monkeypatch.setattr(
        guideline_ingest,
        "_push_qdrant",
        lambda version, records, vectors: pushed.append(version),
    )
    monkeypatch.setattr(dense_retrieval, "embed_documents", lambda texts: None)

    added = await guideline_ingest.ingest(db, push_qdrant=True)

    assert added >= 0
    assert pushed == [], "a vector push was attempted with no embeddings"


async def test_ingest_pushes_to_qdrant_when_embeddings_are_available(db, monkeypatch):
    pushed: list[tuple[str, int]] = []

    def _fake_push(version, records, vectors):
        pushed.append((version, len(vectors)))

    monkeypatch.setattr(guideline_ingest, "_push_qdrant", _fake_push)
    monkeypatch.setattr(dense_retrieval, "embed_documents", lambda texts: [(0.1,)] * len(texts))

    await guideline_ingest.ingest(db, push_qdrant=True)

    assert len(pushed) == 1
    version, count = pushed[0]
    assert version
    assert count > 0
