"""The visit an extraction described was never charted, so nothing could be recorded at one.

``encounter`` was an accepted extraction type everywhere except the one place it had to be. The
extraction schema listed it, the review API returned it, the upload screen drew it with a
tick-box reading "include in the record" — and ``GraphService.merge_entities`` had no arm for
it. A clinician who approved a discharge summary got its drugs and its diagnoses and no record
that the patient had been admitted, with nothing to tell them so: the type was not in the
approval's counts either, so the response reported on four sections and never mentioned the
fifth.

The loss was wider than one table. ``encounters`` is what the ``encounter_id`` foreign keys on
medications, labs, conditions and allergies point at, so every one of them was permanently NULL
— and the FHIR export, which is the whole reason those links matter to anyone outside this
system, had no visits to carry and no way to say which visit a drug was started at.

Alongside it, the same drift one step over: an entity of a type *nothing* charts. The prompt
asks for four types; a model that answers with a fifth ("vital_sign" off a discharge summary) is
not malformed, just unhelpful, and that entity passed every check, reached the review screen
with the same tick-box, was approved and was written nowhere. Worse, the closed ``Literal`` that
was supposed to prevent it was applied to *stored* extraction metadata, so a document that had
already recorded such a type made the review endpoint 500 forever — taking its correctly-read
prescriptions with it, unreviewable and therefore unapprovable.

The rule this file pins: what a clinician is offered for approval, what approval charts, and
what the extractor may emit are one set. Where they cannot be, approval refuses and says which
item — it never silently drops.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.exceptions import EntityNotMergeableError
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.document import Document
from app.models.encounter import Encounter
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.schemas.document import ExtractionApproval
from app.services.document_service import DocumentService
from app.services.extraction.claude_client import _to_entities
from app.services.graph_service import MERGEABLE_ENTITY_TYPES, GraphService

pytestmark = pytest.mark.asyncio


async def _patient(db) -> Patient:
    account = Account(email=f"enc-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Encounter Patient",
        sex="male",
        date_of_birth=date(1970, 1, 1),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


async def _document(db, patient: Patient) -> Document:
    document = Document(
        patient_id=patient.id,
        account_id=patient.account_id,
        file_name="discharge.pdf",
        file_type="pdf",
        file_size_bytes=2048,
        storage_path=f"/tmp/{uuid.uuid4().hex}",
        storage_hash_sha256=uuid.uuid4().hex * 2,
    )
    db.add(document)
    await db.flush()
    return document


def _entity(etype: str, **fields) -> dict:
    return {"entity_type": etype, "fields": fields, "confidence": {}}


async def _rows(db, model, patient) -> list:
    result = await db.execute(select(model).where(model.patient_id == patient.id))
    return list(result.scalars().all())


# --- The visit is charted ---------------------------------------------------------------------


async def test_an_approved_encounter_is_charted(db):
    """The whole defect in one assertion: this used to merge nothing and report nothing."""
    patient = await _patient(db)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity(
                "encounter",
                encounter_date="2026-03-04",
                encounter_type="emergency",
                presenting_complaint="Central chest pain, 2 hours",
                clinician_notes="ECG: ST elevation V2-V4. Referred.",
            )
        ],
    )

    assert counts["encounters"] == 1
    (row,) = await _rows(db, Encounter, patient)
    assert row.encounter_date == date(2026, 3, 4)
    assert row.encounter_type == "emergency"
    assert row.presenting_complaint == "Central chest pain, 2 hours"
    assert row.clinician_notes.startswith("ECG:")


async def test_an_unreadable_visit_type_does_not_reach_the_check_constraint(db):
    """``Encounter`` is behind a ``CHECK ... IN (...)`` like every other extracted enum, and it
    was the one model ``_enum`` did not know about — a value the constraint rejects would have
    failed at flush and taken the whole approval, including its correctly-read drugs, with it."""
    patient = await _patient(db)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("encounter", encounter_date="2026-03-04", encounter_type="day-surgery-ward")
        ],
    )
    await db.flush()

    assert counts["encounters"] == 1
    (row,) = await _rows(db, Encounter, patient)
    # Absent rather than invented. Nothing in the record now claims what kind of visit it was.
    assert row.encounter_type is None


async def test_a_visit_with_no_readable_date_is_not_charted(db):
    """``encounter_date`` is NOT NULL, and a visit at no particular time is not a visit — it is
    a claim that one happened, which is the sort of thing this system must not manufacture."""
    patient = await _patient(db)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[_entity("encounter", encounter_type="outpatient", presenting_complaint="Cough")],
    )

    assert counts["encounters"] == 0
    assert await _rows(db, Encounter, patient) == []


async def test_re_approving_one_document_does_not_chart_the_visit_twice(db):
    """Nothing stops a second approval — a double-clicked button, or a genuine re-approval after
    a correction — and every other section already survived one. A chart showing two emergency
    presentations where there was one is a different clinical picture."""
    patient = await _patient(db)
    document = await _document(db, patient)
    entities = [_entity("encounter", encounter_date="2026-03-04", encounter_type="emergency")]

    first = await GraphService(db).merge_entities(
        patient=patient, document=document, entities=entities
    )
    second = await GraphService(db).merge_entities(
        patient=patient, document=document, entities=entities
    )

    assert (first["encounters"], second["encounters"]) == (1, 0)
    assert len(await _rows(db, Encounter, patient)) == 1


async def test_two_genuinely_different_visits_on_one_date_both_survive(db):
    """The dedup must not collapse a morning clinic review and an evening presentation into one.
    They are two visits, and which one a drug was started at is the reason to record either."""
    patient = await _patient(db)
    document = await _document(db, patient)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=document,
        entities=[
            _entity("encounter", encounter_date="2026-03-04", encounter_type="follow_up"),
            _entity("encounter", encounter_date="2026-03-04", encounter_type="emergency"),
        ],
    )

    assert counts["encounters"] == 2
    assert {row.encounter_type for row in await _rows(db, Encounter, patient)} == {
        "follow_up",
        "emergency",
    }


# --- Findings are attached to the visit -------------------------------------------------------


async def test_a_documents_findings_are_recorded_at_its_visit(db):
    """A prescription written at a consultation describes one visit, and its drugs, results and
    diagnoses were all recorded there. Without this the exported encounter is an orphan: the
    file says a patient attended A&E, and separately that they are on a drug, and joins nothing.

    Order is deliberately visit-last in the payload — nothing guarantees a document lists the
    admission before the drugs started at it, and a medication merged first has nothing to point
    at unless encounters are merged in their own pass.
    """
    patient = await _patient(db)
    document = await _document(db, patient)

    await GraphService(db).merge_entities(
        patient=patient,
        document=document,
        entities=[
            _entity("medication", brand_name_raw="Crocin", dose="500"),
            _entity("lab_result", marker_name="HbA1c", value_numeric=7.4),
            _entity("condition", condition_name="Type 2 diabetes mellitus"),
            _entity("allergy", allergen_name="Sulfa"),
            _entity("encounter", encounter_date="2026-03-04", encounter_type="outpatient"),
        ],
    )

    (visit,) = await _rows(db, Encounter, patient)
    for model in (MedicationEvent, LabResult, Condition, Allergy):
        (row,) = await _rows(db, model, patient)
        assert row.encounter_id == visit.id, f"{model.__name__} was not attached to the visit"


async def test_a_document_carrying_two_visits_attaches_its_findings_to_neither(db):
    """No way to tell which of two visits a drug was started at, so nothing is claimed. A guess
    is indistinguishable at the far end from a fact, which is this system's whole failure mode —
    an unlinked row is what every row here was until now, and is the recoverable answer."""
    patient = await _patient(db)
    document = await _document(db, patient)

    await GraphService(db).merge_entities(
        patient=patient,
        document=document,
        entities=[
            _entity("medication", brand_name_raw="Crocin", dose="500"),
            _entity("encounter", encounter_date="2026-03-04", encounter_type="outpatient"),
            _entity("encounter", encounter_date="2026-03-11", encounter_type="follow_up"),
        ],
    )

    (med,) = await _rows(db, MedicationEvent, patient)
    assert med.encounter_id is None


async def test_a_re_approval_attaches_its_findings_to_the_visit_it_already_charted(db):
    """The second approval dedups the encounter away and creates nothing, so a rule built on
    "what did I just insert" would leave this pass's rows unlinked while the first pass's were
    linked — the same chart described two ways depending on how many times a button was pressed.
    """
    patient = await _patient(db)
    document = await _document(db, patient)
    entities = [
        _entity("encounter", encounter_date="2026-03-04", encounter_type="outpatient"),
        _entity("medication", brand_name_raw="Crocin", dose="500"),
    ]

    await GraphService(db).merge_entities(patient=patient, document=document, entities=entities)
    # A second document so the drug is not itself deduplicated away, carrying the same visit.
    second_doc = await _document(db, patient)
    await GraphService(db).merge_entities(
        patient=patient,
        document=second_doc,
        entities=[
            _entity("encounter", encounter_date="2026-03-04", encounter_type="outpatient"),
            _entity("medication", brand_name_raw="Dolo", dose="650"),
        ],
    )
    await GraphService(db).merge_entities(
        patient=patient, document=second_doc, entities=entities[:1]
    )

    visits = await _rows(db, Encounter, patient)
    assert len(visits) == 2  # one per document; the re-approval added none
    for med in await _rows(db, MedicationEvent, patient):
        assert med.encounter_id is not None


async def test_a_merge_with_no_visit_in_it_costs_no_encounter_lookup(db, engine):
    """The visit lookup is paid only by documents that describe one. A prescription with four
    drugs and no encounter line must not grow a query for a table it has nothing to put in."""
    from tests.test_graph_service_merge_dedup import _SelectCounter

    patient = await _patient(db)
    with _SelectCounter(engine, "encounters") as counter:
        await GraphService(db).merge_entities(
            patient=patient,
            document=None,
            entities=[_entity("medication", brand_name_raw="Crocin", dose="500")],
        )

    # One: the dedup-key prefetch, which is unconditional and constant-cost like the other four.
    # Not two — ``_visit_context`` is skipped entirely when no encounter was in the payload.
    assert counter.count == 1, (
        f"a merge with no encounter in it issued {counter.count} encounters SELECTs"
    )


# --- The three sets are one set ---------------------------------------------------------------


async def test_every_type_the_extractor_may_emit_is_one_the_merge_charts(db):
    """The invariant the defect was an instance of. ``encounter`` sat on the API's list and
    nowhere else for as long as it was dropped; this fails the moment a type is added to any one
    of the three places without the others."""
    patient = await _patient(db)

    for etype in sorted(MERGEABLE_ENTITY_TYPES):
        counts = await GraphService(db).merge_entities(
            patient=patient,
            document=None,
            entities=[
                _entity(
                    etype,
                    brand_name_raw="Crocin",
                    marker_name="HbA1c",
                    value_numeric=7.4,
                    condition_name="Type 2 diabetes mellitus",
                    allergen_name="Sulfa",
                    encounter_date="2026-03-04",
                )
            ],
        )
        assert sum(counts.values()) == 1, f"{etype!r} is offered for approval but charts nothing"


async def test_the_extractor_drops_a_type_nothing_charts():
    """Refused at the boundary where the model's output stops being a suggestion. Past it, an
    entity is something a clinician is asked to approve, and offering to record something that
    cannot be recorded is the defect."""
    entities, _ = _to_entities(
        {
            "entities": [
                {"entity_type": "vital_sign", "fields": {"bp": "180/110"}},
                {"entity_type": "medication", "fields": {"brand_name_raw": "Crocin"}},
            ]
        }
    )

    assert [e.entity_type for e in entities] == ["medication"]


async def test_the_extractor_keeps_a_type_whose_spelling_is_merely_untidy():
    """Dropping is for types nothing charts, not for capitalisation. A model that answers
    "Lab_Result" has given a usable entity and losing a lab value to a case difference would be
    the same silent loss in a new place."""
    entities, _ = _to_entities(
        {"entities": [{"entity_type": " Lab_Result ", "fields": {"marker_name": "HbA1c"}}]}
    )

    assert [e.entity_type for e in entities] == ["lab_result"]


# --- A stored unknown type is survivable, not silently approved -------------------------------


async def test_a_stored_unknown_entity_type_does_not_break_the_review(db):
    """The closed ``Literal`` was a validator on history: a document whose extraction had
    already recorded such a type raised inside ``build_extraction_result`` and 500'd the review
    endpoint forever. Not only for that entity — for the document, so its correctly-read
    prescriptions could not be reviewed, approved, or got at in any other way."""
    patient = await _patient(db)
    document = await _document(db, patient)
    document.extraction_metadata = {
        "entities": [
            {
                "entity_type": "vital_sign",
                "fields": [
                    {
                        "name": "bp",
                        "value": "180/110",
                        "confidence": 0.9,
                        "confidence_band": "high",
                        "needs_confirmation": False,
                    }
                ],
                "region": None,
            },
            {
                "entity_type": "medication",
                "fields": [
                    {
                        "name": "brand_name_raw",
                        "value": "Crocin",
                        "confidence": 0.95,
                        "confidence_band": "high",
                        "needs_confirmation": False,
                    }
                ],
                "region": None,
            },
        ]
    }
    await db.flush()

    result = DocumentService(db).build_extraction_result(document)

    # Both entities, at their original indexes: the corrections in an approval are addressed by
    # index into this list, so hiding one would silently re-target a clinician's amendment.
    assert [e.entity_type for e in result.entities] == ["vital_sign", "medication"]


async def test_approving_an_unchartable_entity_is_refused_rather_than_dropped(db):
    """Silent-skip-and-answer-200 is the failure ``CorrectionNotApplicableError`` was written
    for one level up. The clinician ticked "include in the record"; the record must either
    include it or say it will not."""
    patient = await _patient(db)
    document = await _document(db, patient)
    document.extraction_metadata = {
        "entities": [
            {
                "entity_type": "vital_sign",
                "fields": [
                    {
                        "name": "bp",
                        "value": "180/110",
                        "confidence": 0.9,
                        "confidence_band": "high",
                        "needs_confirmation": False,
                    }
                ],
                "region": None,
            }
        ]
    }
    await db.flush()
    await db.commit()

    with pytest.raises(EntityNotMergeableError) as raised:
        await DocumentService(db).approve(
            account_id=patient.account_id,
            patient_id=patient.id,
            doc_id=document.id,
            approval=ExtractionApproval(),
        )

    # Names the index and the type, so the client can reject exactly that item.
    assert "vital_sign" in str(raised.value.detail)
    assert "0" in str(raised.value.detail)


async def test_rejecting_the_unchartable_entity_lets_the_rest_of_the_document_through(db):
    """One unusable line must not cost a prescription its drugs — the same principle the
    extraction parser already applies to a malformed entity."""
    patient = await _patient(db)
    document = await _document(db, patient)
    document.extraction_metadata = {
        "entities": [
            {
                "entity_type": "vital_sign",
                "fields": [
                    {
                        "name": "bp",
                        "value": "180/110",
                        "confidence": 0.9,
                        "confidence_band": "high",
                        "needs_confirmation": False,
                    }
                ],
                "region": None,
            },
            {
                "entity_type": "medication",
                "fields": [
                    {
                        "name": "brand_name_raw",
                        "value": "Crocin",
                        "confidence": 0.95,
                        "confidence_band": "high",
                        "needs_confirmation": False,
                    }
                ],
                "region": None,
            },
        ]
    }
    await db.flush()
    await db.commit()

    counts = await DocumentService(db).approve(
        account_id=patient.account_id,
        patient_id=patient.id,
        doc_id=document.id,
        approval=ExtractionApproval(rejected_entity_indexes=[0]),
    )

    assert counts["medications"] == 1
    assert len(await _rows(db, MedicationEvent, patient)) == 1
