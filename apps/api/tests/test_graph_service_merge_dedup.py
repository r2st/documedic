"""Within-document deduplication and query-count behaviour of the graph merge.

The merge used to re-query the patient's medications/conditions/allergies once per entity,
which was both an N+1 (a 12-line prescription issued 12 full scans) and a correctness hole:
because the session runs with autoflush off, a row added earlier in the same batch was not
visible to the next entity's SELECT, so a document listing the same drug twice produced two
medication rows.

Lab results dedup on a different key from everything else, and the tests for that are at the
bottom of this file. A condition is identified by its name for as long as the patient has it; a
lab result is an *observation*, and the same marker recurring is the point of a longitudinal
record rather than a duplicate. So labs key on the observation — marker, value and draw date —
within one source document, which is the only place a repeat is unambiguously the same reading
arriving twice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.document import Document
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.graph_service import GraphService


async def _patient(db) -> Patient:
    account = Account(email=f"dedup-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Dedup Patient",
        sex="male",
        date_of_birth=datetime(1970, 1, 1).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


async def _document(db, patient: Patient, name: str = "report.pdf") -> Document:
    """A ``documents`` row to attribute merged entities to. Lab dedup is scoped to one of these,
    so a test that passes ``document=None`` is exercising the manual-entry path, not this one."""
    document = Document(
        patient_id=patient.id,
        account_id=patient.account_id,
        file_name=name,
        file_type="pdf",
        file_size_bytes=1024,
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


class _SelectCounter:
    """Counts SELECT statements issued against a given table while active."""

    def __init__(self, engine, table: str) -> None:
        self.sync_engine = engine.sync_engine
        self.table = table
        self.count = 0

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        normalised = " ".join(statement.split()).lower()
        if normalised.startswith("select") and f" {self.table}" in normalised:
            self.count += 1

    def __enter__(self):
        event.listen(self.sync_engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        event.remove(self.sync_engine, "before_cursor_execute", self._on_execute)
        return False


@pytest.mark.asyncio
async def test_a_drug_listed_twice_in_one_document_is_merged_once(db):
    """OCR frequently repeats a line; the record must not gain a phantom second course."""
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("medication", brand_name_raw="Crocin", dose="650"),
            _entity("medication", brand_name_raw="Crocin", dose="650"),
        ],
    )
    assert counts["medications"] == 1
    assert len(await _rows(db, MedicationEvent, patient)) == 1


@pytest.mark.asyncio
async def test_two_doses_of_the_same_drug_in_one_document_are_both_kept(db):
    """A titration written on one prescription is two distinct clinical facts."""
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("medication", brand_name_raw="Crocin", dose="650"),
            _entity("medication", brand_name_raw="Crocin", dose="500"),
        ],
    )
    assert counts["medications"] == 2
    assert len(await _rows(db, MedicationEvent, patient)) == 2


@pytest.mark.asyncio
async def test_a_brand_and_its_generic_in_one_document_collapse_to_one_medication(db):
    """Dedup keys off the resolved generic, so "Crocin" and "Paracetamol" are one drug."""
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("medication", brand_name_raw="Crocin", dose="650"),
            _entity("medication", brand_name_raw="Paracetamol", dose="650"),
        ],
    )
    assert counts["medications"] == 1


@pytest.mark.asyncio
async def test_a_condition_listed_twice_in_one_document_is_merged_once(db):
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("condition", condition_name="Hypertension"),
            _entity("condition", condition_name="HYPERTENSION"),
        ],
    )
    assert counts["conditions"] == 1
    assert len(await _rows(db, Condition, patient)) == 1


@pytest.mark.asyncio
async def test_an_allergy_listed_twice_in_one_document_is_merged_once(db):
    """A duplicated allergy line must not inflate the hard-block surface."""
    patient = await _patient(db)
    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[
            _entity("allergy", allergen_name="Penicillin", severity="severe"),
            _entity("allergy", allergen_name="penicillin", severity="mild"),
        ],
    )
    assert counts["allergies"] == 1
    assert len(await _rows(db, Allergy, patient)) == 1


@pytest.mark.asyncio
async def test_dedup_still_applies_across_separate_documents(db):
    """The in-batch key set must not weaken the existing cross-document dedup."""
    patient = await _patient(db)
    service = GraphService(db)
    first = [_entity("medication", brand_name_raw="Crocin", dose="650")]
    assert (await service.merge_entities(patient=patient, document=None, entities=first))[
        "medications"
    ] == 1
    assert (await service.merge_entities(patient=patient, document=None, entities=first))[
        "medications"
    ] == 0


@pytest.mark.asyncio
async def test_medication_lookups_do_not_scale_with_the_number_of_entities(db, engine):
    """Regression guard for the N+1: the dedup set is loaded once per merge, not per line."""
    patient = await _patient(db)
    entities = [
        _entity("medication", brand_name_raw=brand, dose="500")
        for brand in ("Crocin", "Glycomet", "Brufen", "Augmentin", "Amlodipine", "Atorvastatin")
    ]

    with _SelectCounter(engine, "medication_events") as counter:
        await GraphService(db).merge_entities(patient=patient, document=None, entities=entities)

    assert counter.count == 1, (
        f"{len(entities)} medications triggered {counter.count} medication_events SELECTs; "
        "the dedup key set must be loaded once per merge"
    )


@pytest.mark.asyncio
async def test_condition_and_allergy_lookups_are_also_loaded_once(db, engine):
    patient = await _patient(db)
    entities = [
        _entity("condition", condition_name=name)
        for name in ("Hypertension", "Type 2 Diabetes", "CKD stage 3", "Asthma")
    ] + [_entity("allergy", allergen_name=name) for name in ("Penicillin", "Sulfa", "Aspirin")]

    with _SelectCounter(engine, "conditions") as conditions:
        with _SelectCounter(engine, "allergies") as allergies:
            await GraphService(db).merge_entities(patient=patient, document=None, entities=entities)

    assert conditions.count == 1
    assert allergies.count == 1


# --------------------------------------------------------------------------- lab results

# Labs are the entity type where "the same thing twice" and "the same thing again" are different
# clinical facts, so each of these pins one side of that line.


@pytest.mark.asyncio
async def test_re_merging_one_document_does_not_duplicate_its_lab_results(db):
    """Approving an extraction twice must not turn one blood draw into two.

    Nothing prevents a second approval — ``extraction_metadata["approved"]`` is set but never
    read — and there are two ordinary ways to reach one: a double-clicked Approve button, and a
    clinician who approves, notices a mis-extracted drug name and re-approves with a correction.
    Medications, conditions and allergies already survived that. Labs did not, so the chart
    showed the same creatinine twice, which reads as two separate draws rather than one.
    """
    patient = await _patient(db)
    document = await _document(db, patient)
    service = GraphService(db)
    entities = [
        _entity("lab_result", marker_name="Creatinine", value_numeric="3.0"),
        _entity("lab_result", marker_name="HbA1c", value_numeric="9.2"),
    ]

    assert (await service.merge_entities(patient=patient, document=document, entities=entities))[
        "lab_results"
    ] == 2
    assert (await service.merge_entities(patient=patient, document=document, entities=entities))[
        "lab_results"
    ] == 0

    rows = await _rows(db, LabResult, patient)
    assert sorted(r.marker_name for r in rows) == ["Creatinine", "HbA1c"]


@pytest.mark.asyncio
async def test_a_re_merge_does_not_recompute_a_second_copy_of_a_derived_marker(db):
    """The duplicate compounds: eGFR is derived per newly merged creatinine.

    A doubled eGFR is not just clutter. It is the input the renal contraindication rules read,
    and a chart carrying two identical computed values invites the reader to believe two
    independent estimates agreed.
    """
    patient = await _patient(db)
    document = await _document(db, patient)
    service = GraphService(db)
    entities = [_entity("lab_result", marker_name="Creatinine", value_numeric="3.0")]

    await service.merge_entities(patient=patient, document=document, entities=entities)
    await service.merge_entities(patient=patient, document=document, entities=entities)

    markers = await _rows(db, DerivedMarker, patient)
    assert [m.marker_name for m in markers] == ["eGFR"]


@pytest.mark.asyncio
async def test_the_same_marker_from_a_later_report_is_a_new_result_not_a_duplicate(db):
    """The whole purpose of the longitudinal record: a creatinine trend over time."""
    patient = await _patient(db)
    service = GraphService(db)
    january = await _document(db, patient, "jan.pdf")
    april = await _document(db, patient, "apr.pdf")

    await service.merge_entities(
        patient=patient,
        document=january,
        entities=[_entity("lab_result", marker_name="Creatinine", value_numeric="1.4")],
    )
    counts = await service.merge_entities(
        patient=patient,
        document=april,
        entities=[_entity("lab_result", marker_name="Creatinine", value_numeric="2.9")],
    )

    assert counts["lab_results"] == 1
    rows = await _rows(db, LabResult, patient)
    assert sorted(str(r.value_numeric) for r in rows) == ["1.400000", "2.900000"]


@pytest.mark.asyncio
async def test_an_unchanged_repeat_value_from_a_later_report_is_still_kept(db):
    """A stable creatinine is a finding. Keying on the document is what preserves it —
    marker-and-value alone would erase the follow-up that showed no deterioration."""
    patient = await _patient(db)
    service = GraphService(db)
    entity = [_entity("lab_result", marker_name="Creatinine", value_numeric="1.4")]

    for name in ("jan.pdf", "apr.pdf", "jul.pdf"):
        document = await _document(db, patient, name)
        counts = await service.merge_entities(patient=patient, document=document, entities=entity)
        assert counts["lab_results"] == 1

    assert len(await _rows(db, LabResult, patient)) == 3


@pytest.mark.asyncio
async def test_one_marker_reported_twice_in_a_document_with_different_values_keeps_both(db):
    """Pre- and post-dialysis creatinine on one report are two readings, not a duplicated line."""
    patient = await _patient(db)
    document = await _document(db, patient)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=document,
        entities=[
            _entity("lab_result", marker_name="Creatinine", value_numeric="6.2"),
            _entity("lab_result", marker_name="Creatinine", value_numeric="2.1"),
        ],
    )

    assert counts["lab_results"] == 2
    assert len(await _rows(db, LabResult, patient)) == 2


@pytest.mark.asyncio
async def test_a_lab_line_repeated_within_one_document_is_merged_once(db):
    """The OCR failure the medication rule already covers, on the lab section."""
    patient = await _patient(db)
    document = await _document(db, patient)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=document,
        entities=[
            _entity("lab_result", marker_name="Potassium", value_numeric="6.8", unit="mmol/L"),
            _entity("lab_result", marker_name="potassium", value_numeric="6.8", unit="mmol/L"),
        ],
    )

    assert counts["lab_results"] == 1
    assert len(await _rows(db, LabResult, patient)) == 1


@pytest.mark.asyncio
async def test_the_same_marker_and_value_drawn_on_two_dates_in_one_report_are_both_kept(db):
    """A cumulative report listing a marker per day is one document holding several draws."""
    patient = await _patient(db)
    document = await _document(db, patient)
    monday = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=document,
        entities=[
            _entity(
                "lab_result",
                marker_name="Sodium",
                value_numeric="128",
                sample_date=monday.isoformat(),
            ),
            _entity(
                "lab_result",
                marker_name="Sodium",
                value_numeric="128",
                sample_date=(monday + timedelta(days=1)).isoformat(),
            ),
        ],
    )

    assert counts["lab_results"] == 2


@pytest.mark.asyncio
async def test_a_dated_lab_re_merged_from_the_same_document_is_still_a_duplicate(db):
    """The dated path has to dedup too, and it is the one a naive key silently breaks.

    ``_parse_datetime`` returns an aware datetime; SQLite hands the stored one back naive. As
    ``datetime`` objects those compare unequal, so a key built from them would never match and
    this test would find two rows while the undated case passed.
    """
    patient = await _patient(db)
    document = await _document(db, patient)
    service = GraphService(db)
    entities = [
        _entity(
            "lab_result",
            marker_name="Sodium",
            value_numeric="128",
            sample_date="2026-03-02T08:00:00+00:00",
        )
    ]

    await service.merge_entities(patient=patient, document=document, entities=entities)
    counts = await service.merge_entities(patient=patient, document=document, entities=entities)

    assert counts["lab_results"] == 0
    assert len(await _rows(db, LabResult, patient)) == 1


@pytest.mark.asyncio
async def test_lab_dedup_keys_are_loaded_once_per_merge_not_once_per_lab(db, engine):
    """The N+1 guard the other entity types already carry, extended to the lab key set."""
    patient = await _patient(db)
    document = await _document(db, patient)
    entities = [
        _entity("lab_result", marker_name=marker, value_numeric="1.0")
        for marker in ("Creatinine", "HbA1c", "Sodium", "Potassium", "Haemoglobin", "TSH")
    ]

    with _SelectCounter(engine, "lab_results") as counter:
        await GraphService(db).merge_entities(patient=patient, document=document, entities=entities)

    assert counter.count == 1, (
        f"{len(entities)} lab results triggered {counter.count} lab_results SELECTs; "
        "the dedup key set must be loaded once per merge"
    )


@pytest.mark.asyncio
async def test_a_document_scoped_key_set_does_not_load_another_documents_labs(db, engine):
    """Cost stays flat as the chart grows. Patient-scoped, this read would load a key for every
    lab the patient has ever had — the set that grows fastest in a long-running record."""
    patient = await _patient(db)
    history = await _document(db, patient, "history.pdf")
    service = GraphService(db)
    await service.merge_entities(
        patient=patient,
        document=history,
        entities=[
            _entity("lab_result", marker_name=f"Marker{i}", value_numeric=str(i)) for i in range(20)
        ],
    )

    fresh = await _document(db, patient, "today.pdf")
    with _SelectCounter(engine, "lab_results") as counter:
        counts = await service.merge_entities(
            patient=patient,
            document=fresh,
            entities=[_entity("lab_result", marker_name="Marker0", value_numeric="0")],
        )

    assert counter.count == 1
    # And the older document's copy of Marker0 does not suppress today's reading.
    assert counts["lab_results"] == 1
