"""Merging the same extraction twice must not chart the same fact twice.

``DocumentService.approve`` is re-runnable by design and nothing stops it: a double-clicked
button, or a clinician genuinely re-approving after correcting a drug name, re-merges every
entity in the stored extraction. ``GraphService`` is what absorbs that, and it did so for
continuing medications, conditions, allergies and labs — but two shapes fell through the
deduplication entirely and re-inserted on every approval.

**Discontinuations.** The dedup key set was loaded from the patient's *current* medications, and
a ``stop`` row is not current, so no stop event could ever be recognised as already charted.
Re-approving a prescription reading "STOP Warfarin" appended a second discontinuation, then a
third, one per approval — all on the same date, all from the same page.

**Drugs the vocabulary does not know.** Deduplication keyed on ``generic_name`` and skipped the
check outright when there was none (``if generic and key in seen``), so an unresolved brand had
no identity to compare and every line inserted. One prescription listing the same unrecognised
brand twice charted it twice; approving that prescription again charted it again. These are
precisely the drugs the deterministic engine cannot evaluate — they resolve to no reference id,
so they reach the clinician as the "could not be evaluated" note naming what the safety engine
had no view of. Duplicated, one unreadable drug is listed as two and counted twice by every
per-row check reading off the record.

The fix gives a medication row an identity — the generic when it resolved, the brand text as
written when it did not — and keys discontinuations separately, scoped to their source document.
The document scoping is load-bearing rather than tidy, and
``test_a_later_document_may_stop_the_same_drug_again`` is why: a drug can legitimately be stopped,
restarted, and stopped again, and a patient-wide stop key would read the second stop as a repeat
of the first and skip the retirement — leaving the drug current on a patient just taken off it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.document import Document
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.graph_service import GraphService


async def _patient(db) -> Patient:
    account = Account(email=f"idem-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Idempotent Merge",
        sex="male",
        date_of_birth=datetime(1970, 1, 1).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


async def _document(db, patient: Patient, name: str = "rx.pdf") -> Document:
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


def _med(**fields) -> dict:
    fields.setdefault("event_type", "continue")
    return {"entity_type": "medication", "fields": fields, "confidence": {}}


async def _meds(db, patient: Patient) -> list[MedicationEvent]:
    result = await db.execute(
        select(MedicationEvent).where(MedicationEvent.patient_id == patient.id)
    )
    return list(result.scalars().all())


async def _current(db, patient: Patient) -> list[MedicationEvent]:
    return [m for m in await _meds(db, patient) if m.is_current]


# --------------------------------------------------------------- discontinuations


@pytest.mark.asyncio
async def test_re_approving_a_stop_does_not_append_a_second_discontinuation(db):
    """The same page approved three times is one discontinuation, not three."""
    patient = await _patient(db)
    doc = await _document(db, patient)
    service = GraphService(db)

    await service.merge_entities(
        patient=patient,
        document=doc,
        entities=[_med(generic_name="Warfarin", dose="5")],
    )
    stop = [_med(generic_name="Warfarin", dose="5", event_type="stop")]
    for _ in range(3):
        await service.merge_entities(patient=patient, document=doc, entities=stop)

    stops = [m for m in await _meds(db, patient) if m.event_type == "stop"]
    assert len(stops) == 1
    # And the drug is still off the chart — deduplicating the row must not resurrect the drug.
    assert await _current(db, patient) == []


@pytest.mark.asyncio
async def test_the_merge_still_reports_a_repeated_stop_as_nothing_merged(db):
    """The count ``approve`` audits and returns has to agree with what was written."""
    patient = await _patient(db)
    doc = await _document(db, patient)
    service = GraphService(db)
    stop = [_med(generic_name="Warfarin", dose="5", event_type="stop")]

    first = await service.merge_entities(patient=patient, document=doc, entities=stop)
    second = await service.merge_entities(patient=patient, document=doc, entities=stop)

    assert first["medications"] == 1
    assert second["medications"] == 0


@pytest.mark.asyncio
async def test_one_document_stopping_the_same_drug_twice_writes_it_once(db):
    """Within-document, not only across approvals: OCR repeats lines from a two-column page."""
    patient = await _patient(db)
    doc = await _document(db, patient)
    await GraphService(db).merge_entities(
        patient=patient,
        document=doc,
        entities=[
            _med(generic_name="Warfarin", dose="5", event_type="stop"),
            _med(generic_name="Warfarin", dose="5", event_type="stop"),
        ],
    )
    assert len([m for m in await _meds(db, patient) if m.event_type == "stop"]) == 1


@pytest.mark.asyncio
async def test_a_later_document_may_stop_the_same_drug_again(db):
    """Why the stop key is scoped to its document, and what a patient-wide key would cost.

    Stopped, restarted, stopped again is an ordinary course. If the second stop were read as a
    duplicate of the first, the merge would return early and never reach ``_retire_current`` —
    and the drug would stay ``is_current`` on a patient who has just been taken off it, with a
    ``stop`` row on the same chart saying the opposite.
    """
    patient = await _patient(db)
    service = GraphService(db)
    first_stop = await _document(db, patient, "2023-stop.pdf")
    restart = await _document(db, patient, "2024-restart.pdf")
    second_stop = await _document(db, patient, "2025-stop.pdf")

    await service.merge_entities(
        patient=patient, document=first_stop, entities=[_med(generic_name="Warfarin", dose="5")]
    )
    await service.merge_entities(
        patient=patient,
        document=first_stop,
        entities=[_med(generic_name="Warfarin", dose="5", event_type="stop")],
    )
    assert await _current(db, patient) == []

    await service.merge_entities(
        patient=patient, document=restart, entities=[_med(generic_name="Warfarin", dose="5")]
    )
    assert [m.generic_name for m in await _current(db, patient)] == ["Warfarin"]

    await service.merge_entities(
        patient=patient,
        document=second_stop,
        entities=[_med(generic_name="Warfarin", dose="5", event_type="stop")],
    )
    assert await _current(db, patient) == []
    assert len([m for m in await _meds(db, patient) if m.event_type == "stop"]) == 2


# ------------------------------------------------ drugs the vocabulary cannot resolve


@pytest.mark.asyncio
async def test_an_unresolved_brand_listed_twice_in_one_document_is_charted_once(db):
    patient = await _patient(db)
    doc = await _document(db, patient)
    line = _med(brand_name_raw="Zyxomet-XR", dose="500")
    await GraphService(db).merge_entities(
        patient=patient, document=doc, entities=[line, dict(line)]
    )
    assert len(await _meds(db, patient)) == 1


@pytest.mark.asyncio
async def test_re_approving_an_unresolved_brand_does_not_chart_it_again(db):
    patient = await _patient(db)
    doc = await _document(db, patient)
    service = GraphService(db)
    payload = [_med(brand_name_raw="Zyxomet-XR", dose="500")]

    await service.merge_entities(patient=patient, document=doc, entities=payload)
    await service.merge_entities(patient=patient, document=doc, entities=payload)

    assert len(await _meds(db, patient)) == 1


@pytest.mark.asyncio
async def test_two_different_unresolved_brands_at_one_dose_stay_two_drugs(db):
    """Why the fallback is the brand text and not the empty string.

    Keying every unresolvable drug as ``""`` would collide them at a shared dose, and the second
    would be dropped from the chart — losing a drug is far worse than listing one twice, which is
    why the old code declined to deduplicate these at all rather than key them that way.
    """
    patient = await _patient(db)
    doc = await _document(db, patient)
    await GraphService(db).merge_entities(
        patient=patient,
        document=doc,
        entities=[
            _med(brand_name_raw="Zyxomet-XR", dose="500"),
            _med(brand_name_raw="Qorbanil", dose="500"),
        ],
    )
    charted = sorted(m.brand_name_raw or "" for m in await _meds(db, patient))
    assert charted == ["Qorbanil", "Zyxomet-XR"]


@pytest.mark.asyncio
async def test_a_line_with_no_name_at_all_is_still_never_deduplicated(db):
    """A row carrying neither generic nor brand has no identity, so it cannot claim another's."""
    patient = await _patient(db)
    doc = await _document(db, patient)
    await GraphService(db).merge_entities(
        patient=patient,
        document=doc,
        entities=[_med(dose="500"), _med(dose="500")],
    )
    assert len(await _meds(db, patient)) == 2


@pytest.mark.asyncio
async def test_stopping_an_unresolved_brand_actually_takes_the_patient_off_it(db):
    """The retirement matched ``generic_name``, which is NULL for a drug with no vocabulary row.

    So "STOP Zyxomet-XR" wrote its discontinuation and left Zyxomet-XR ``is_current`` beside it:
    the FHIR export reads ``event_type`` and says stopped, ``GET /records`` and the safety engine
    read ``is_current`` and say the patient is still on it.
    """
    patient = await _patient(db)
    service = GraphService(db)
    started = await _document(db, patient, "start.pdf")
    stopped = await _document(db, patient, "stop.pdf")

    await service.merge_entities(
        patient=patient, document=started, entities=[_med(brand_name_raw="Zyxomet-XR", dose="500")]
    )
    assert len(await _current(db, patient)) == 1

    await service.merge_entities(
        patient=patient,
        document=stopped,
        entities=[_med(brand_name_raw="Zyxomet-XR", dose="500", event_type="stop")],
    )
    assert await _current(db, patient) == []


@pytest.mark.asyncio
async def test_stopping_one_unresolved_brand_does_not_retire_another(db):
    """Exact equality on folded text: the fallback must stay as narrow as the vocabulary match."""
    patient = await _patient(db)
    doc = await _document(db, patient)
    service = GraphService(db)

    await service.merge_entities(
        patient=patient,
        document=doc,
        entities=[
            _med(brand_name_raw="Zyxomet-XR", dose="500"),
            _med(brand_name_raw="Qorbanil", dose="250"),
        ],
    )
    await service.merge_entities(
        patient=patient,
        document=await _document(db, patient, "stop.pdf"),
        entities=[_med(brand_name_raw="Zyxomet-XR", dose="500", event_type="stop")],
    )

    assert [m.brand_name_raw for m in await _current(db, patient)] == ["Qorbanil"]


@pytest.mark.asyncio
async def test_an_unnamed_stop_line_still_retires_nothing(db):
    """An unreadable line must not be able to empty a medication list."""
    patient = await _patient(db)
    doc = await _document(db, patient)
    service = GraphService(db)

    await service.merge_entities(
        patient=patient, document=doc, entities=[_med(generic_name="Warfarin", dose="5")]
    )
    await service.merge_entities(
        patient=patient,
        document=await _document(db, patient, "smudged.pdf"),
        entities=[_med(dose="5", event_type="stop")],
    )

    assert [m.generic_name for m in await _current(db, patient)] == ["Warfarin"]


@pytest.mark.asyncio
async def test_brand_identity_is_case_and_whitespace_insensitive(db):
    """OCR of the same brand on two lines differs in case and padding, not in drug."""
    patient = await _patient(db)
    doc = await _document(db, patient)
    await GraphService(db).merge_entities(
        patient=patient,
        document=doc,
        entities=[
            _med(brand_name_raw="Zyxomet-XR", dose="500"),
            _med(brand_name_raw="  zyxomet-xr ", dose="500"),
        ],
    )
    assert len(await _meds(db, patient)) == 1


@pytest.mark.asyncio
async def test_a_padded_brand_charted_first_is_still_recognised_next_time(db):
    """The stored row keeps OCR's padding, so the *database* side of the key must fold it too.

    Lowercasing without trimming would leave the charted identity as ``"  zyxomet-xr "`` and the
    incoming one as ``"zyxomet-xr"``, which never match — the duplicate would come back for any
    brand whose first appearance happened to be the padded one.
    """
    patient = await _patient(db)
    doc = await _document(db, patient)
    service = GraphService(db)

    await service.merge_entities(
        patient=patient, document=doc, entities=[_med(brand_name_raw="  Zyxomet-XR ", dose="500")]
    )
    await service.merge_entities(
        patient=patient, document=doc, entities=[_med(brand_name_raw="Zyxomet-XR", dose="500")]
    )

    assert len(await _meds(db, patient)) == 1
