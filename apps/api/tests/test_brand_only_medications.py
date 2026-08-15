"""A medication charted under a brand the vocabulary does not carry, and nothing said so.

``check_unevaluated_medications`` exists because a current medication the resolver cannot read
is dropped from every rule keyed on ``current_meds``, and an empty flag list is the same
response as a chart with nothing to find. It closed that door for a row whose ``generic_name``
resolves to nothing. It did not close the door beside it.

``GraphService._merge_medication`` resolves the brand and writes the INN it found. When the
brand resolves to nothing there is no INN to write, so the only name on the row is
``brand_name_raw`` — and ``SafetyService._current_meds`` read ``generic_name`` alone. Such a row
reached neither list: not ``current_meds``, so no interaction, contraindication, allergy or
duplicate-therapy rule was evaluated against it; and not ``unresolved_current_meds``, so no note
said a line had gone unread. The chart displayed "Zerodol-SP" to the clinician and the safety
screen came back clean.

This is not the exotic case. The vocabulary ships fifty drugs, the product is built for Indian
prescriptions, and an unseeded brand name is what it will meet all day — it is the situation the
brand-to-INN pipeline exists for, and the one where its failure was silent. A row carrying no
name in either column had the same fate, and is now counted too.

Both halves are also retried here rather than trusted to the merge, so seeding the brand into
the vocabulary makes an already-charted row resolve on the next check instead of on the next
re-approval of the document it came from.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.models.allergy import Allergy
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.graph_service import GraphService
from app.services.safety_service import SafetyService

# A real Indian brand, deliberately outside the seeded corpus.
UNSEEDED_BRAND = "Zerodol-SP"


async def _patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"brand-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Brand Only Patient",
        sex="female",
        date_of_birth=datetime(1966, 2, 11).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _approve(db, patient: Patient, *fields: dict) -> None:
    """Extracted medication lines merged into the graph, as approving a document does."""
    await GraphService(db).merge_entities(
        patient=patient,
        document=None,
        entities=[{"entity_type": "medication", "fields": f, "confidence": {}} for f in fields],
    )
    await db.flush()


async def _notes(db, patient: Patient) -> dict[str, dict]:
    flags = await SafetyService(db).chart_completeness_flags(patient.id)
    return {flag.check_type: flag.details for flag in flags}


# --- The blind spot ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_brand_the_vocabulary_does_not_carry_is_reported_not_dropped(db):
    """The row the merge could not resolve keeps its brand, and that is a name.

    Before this, the medication was on the chart, on the screen, and in neither of the two lists
    the safety engine works from — so the clinician was shown the drug and told nothing had been
    found against it.
    """
    _account, patient = await _patient(db)
    await _approve(db, patient, {"brand_name_raw": UNSEEDED_BRAND, "event_type": "continue"})

    ctx = await SafetyService(db)._build_context(patient.id)
    assert ctx.current_meds == []
    assert ctx.unresolved_current_meds == [UNSEEDED_BRAND]
    assert (await _notes(db, patient))["unevaluated_medication"]["unresolved_medications"] == [
        UNSEEDED_BRAND
    ]


@pytest.mark.asyncio
async def test_the_note_reaches_the_screen_beside_a_drug_that_did_resolve(db):
    """The mixed chart, which is what a real one looks like.

    One line resolved and one did not. The flag list for the drug that resolved must not be read
    as a verdict on the chart, so the note about the other has to be there alongside it.
    """
    account, patient = await _patient(db)
    await _approve(
        db,
        patient,
        {"generic_name": "Metformin", "event_type": "continue"},
        {"brand_name_raw": UNSEEDED_BRAND, "event_type": "continue"},
    )

    service = SafetyService(db)
    evaluated = await service.active_flags(account_id=account.id, patient_id=patient.id)
    assert [drug.generic_name for drug, _flags in evaluated] == ["Metformin"]
    assert (await _notes(db, patient))["unevaluated_medication"]["unresolved_medications"] == [
        UNSEEDED_BRAND
    ]


@pytest.mark.asyncio
async def test_a_line_with_no_drug_name_at_all_is_counted_rather_than_forgotten(db):
    """A dose and nothing else — what a badly scanned line can extract to.

    It cannot be evaluated and it cannot be named, but it is still a medication line on this
    patient's chart, and the one thing that must not happen is for it to leave no trace in
    either direction.
    """
    _account, patient = await _patient(db)
    await _approve(db, patient, {"dose": "10mg", "event_type": "continue"})

    details = (await _notes(db, patient))["unevaluated_medication"]
    assert details["unresolved_medications"] == ["a line with no drug name"]
    assert details["evaluated"] is False


@pytest.mark.asyncio
async def test_an_unreadable_brand_does_not_hide_an_allergy_behind_a_clean_check(db):
    """Why the note is worth the pixel, stated as a hard block that cannot be reached.

    The patient is documented allergic to aspirin and is on a brand nothing can resolve. Nothing
    in this system can say whether the two conflict — and the answer to that is a clinician
    reading the line, which they only do if the screen says the line was not read.
    """
    _account, patient = await _patient(db)
    await _approve(db, patient, {"brand_name_raw": UNSEEDED_BRAND, "event_type": "continue"})
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name="Aspirin",
            allergen_type="drug",
            status="active",
        )
    )
    await db.flush()

    notes = await _notes(db, patient)
    assert UNSEEDED_BRAND in notes["unevaluated_medication"]["unresolved_medications"]


# --- What must not change ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_brand_the_vocabulary_does_carry_still_resolves_through_it(db):
    """The ordinary path: Glycomet is Metformin, and the row is evaluated as such."""
    _account, patient = await _patient(db)
    await _approve(db, patient, {"brand_name_raw": "Glycomet", "event_type": "continue"})

    ctx = await SafetyService(db)._build_context(patient.id)
    assert [m.reference_id for m in ctx.current_meds] == ["MET-500"]
    assert ctx.unresolved_current_meds == []


@pytest.mark.asyncio
async def test_seeding_the_brand_later_makes_an_already_charted_row_resolve(db):
    """Resolution is retried on read, not frozen at approval.

    A row is written with whatever the vocabulary knew that day. Adding the brand afterwards is
    the documented remedy the unevaluated note asks the clinician to request — so it has to take
    effect on the next safety check rather than requiring the original document to be approved
    again, which for a chart assembled out of other people's paper may not be possible at all.
    """
    _account, patient = await _patient(db)
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            brand_name_raw="Glycomet",
            generic_name=None,
            event_type="continue",
            is_current=True,
        )
    )
    await db.flush()

    ctx = await SafetyService(db)._build_context(patient.id)
    assert [m.reference_id for m in ctx.current_meds] == ["MET-500"]


@pytest.mark.asyncio
async def test_the_generic_wins_when_a_row_carries_both_names(db):
    """Generic first: it is the vocabulary's own key, and the brand is the fallback."""
    _account, patient = await _patient(db)
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            brand_name_raw=UNSEEDED_BRAND,
            generic_name="Metformin",
            event_type="continue",
            is_current=True,
        )
    )
    await db.flush()

    ctx = await SafetyService(db)._build_context(patient.id)
    assert [m.reference_id for m in ctx.current_meds] == ["MET-500"]
    assert ctx.unresolved_current_meds == []


@pytest.mark.asyncio
async def test_a_chart_with_nothing_unreadable_still_raises_no_note(db):
    """The regression that matters most: a clean chart must stay silent.

    Widening what counts as a name widens what can be reported, and a note on every chart is the
    thing that teaches clinicians to stop reading notes.
    """
    _account, patient = await _patient(db)
    await _approve(
        db,
        patient,
        {"generic_name": "Metformin", "event_type": "continue"},
        {"brand_name_raw": "Glycomet", "dose": "1000mg", "event_type": "continue"},
    )

    assert await SafetyService(db).chart_completeness_flags(patient.id) == []
