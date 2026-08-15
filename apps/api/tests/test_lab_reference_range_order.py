"""A reference interval read backwards charts every normal value as abnormally high.

``GraphService._merge_lab`` decides ``is_abnormal`` by testing the two bounds independently::

    if high is not None and value > high:   -> abnormal, "high"
    elif low is not None and value < low:   -> abnormal, "low"
    else:                                    -> normal

That is correct for a well-formed interval and systematically wrong for an inverted one. With
``low=5.1, high=3.5`` — a potassium range of "3.5 - 5.1" read in the wrong column order — a
potassium of 4.2 satisfies ``4.2 > 3.5`` and is charted as abnormally high, and so is every
other value inside the true reference range. The two values that happen to be *outside* it are
flagged correctly, which is what makes the defect quiet: the row looks plausible.

Inverted pairs are not exotic. Lab reports are two-column layouts read by OCR, and the vision
extractor emits ``reference_range_low`` and ``reference_range_high`` as separate JSON fields
that a model can transpose.

The consequence is not cosmetic, because ``is_abnormal`` is not only rendered:

* ``agents.tools.summarize_snapshot`` selects on it to build the record summary that all eight
  agents — including the Verifier — reason from, so a fabricated abnormal finding is put in
  front of the panel as fact.
* ``export_service`` writes it as the FHIR observation ``interpretation``, so it leaves the
  system as a clinical assertion in a bundle another provider will read.

The fix drops both ends rather than swapping them. Swapping assumes the numbers are right and
only their order is wrong, which is a guess about a document nobody has re-read; dropping leaves
``is_abnormal`` at ``None``, which is this engine's standing answer for a comparison it could
not attempt and is exactly what a lab with no printed range already gets. The value itself is
always kept, and the deterministic critical-value guard screens it against its own thresholds
rather than the document's, so nothing dangerous stops being caught.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.services.graph_service import GraphService

pytestmark = pytest.mark.asyncio


async def _patient(db) -> Patient:
    patient = Patient(
        account_id=uuid.uuid4(),
        full_name="Range Test",
        sex="female",
        consent_given=True,
    )
    db.add(patient)
    await db.flush()
    return patient


def _lab(marker: str, value: float, low: float | None, high: float | None) -> dict:
    fields: dict[str, object] = {"marker_name": marker, "value_numeric": value}
    if low is not None:
        fields["reference_range_low"] = low
    if high is not None:
        fields["reference_range_high"] = high
    return {"entity_type": "lab_result", "fields": fields, "confidence": {}}


async def _merge(db, patient: Patient, *entities: dict) -> list[LabResult]:
    await GraphService(db).merge_entities(patient=patient, document=None, entities=list(entities))
    await db.flush()
    rows = await db.execute(select(LabResult).where(LabResult.patient_id == patient.id))
    return list(rows.scalars().all())


async def test_a_value_inside_a_backwards_range_is_not_charted_as_abnormal(db):
    """The defect in one line: 4.2 mmol/L against "5.1 - 3.5" was abnormally high."""
    patient = await _patient(db)

    rows = await _merge(db, patient, _lab("Potassium", 4.2, low=5.1, high=3.5))

    assert len(rows) == 1
    assert rows[0].is_abnormal is None, "an unusable range must not produce a verdict"
    assert rows[0].abnormality_direction is None


async def test_the_unusable_range_is_dropped_rather_than_swapped(db):
    """Not a guess about which end the document meant. Both ends go; the value stays."""
    patient = await _patient(db)

    rows = await _merge(db, patient, _lab("Potassium", 4.2, low=5.1, high=3.5))

    assert rows[0].reference_range_low is None
    assert rows[0].reference_range_high is None
    assert rows[0].value_numeric == Decimal("4.2")


async def test_a_well_formed_range_still_decides(db):
    """The guard must not cost the ordinary case its verdict."""
    patient = await _patient(db)

    rows = await _merge(db, patient, _lab("Potassium", 6.4, low=3.5, high=5.1))

    assert rows[0].is_abnormal is True
    assert rows[0].abnormality_direction == "high"
    assert rows[0].reference_range_low == Decimal("3.5")
    assert rows[0].reference_range_high == Decimal("5.1")


async def test_a_value_inside_a_well_formed_range_is_normal(db):
    patient = await _patient(db)

    rows = await _merge(db, patient, _lab("Potassium", 4.2, low=3.5, high=5.1))

    assert rows[0].is_abnormal is False
    assert rows[0].abnormality_direction is None


async def test_equal_ends_are_a_degenerate_range_not_an_inverted_one(db):
    """``low == high`` is a single-point interval — unusual, but not a misread, so it is kept."""
    patient = await _patient(db)

    rows = await _merge(db, patient, _lab("Marker", 7.0, low=7.0, high=7.0))

    assert rows[0].reference_range_low == Decimal("7.0")
    assert rows[0].is_abnormal is False


async def test_a_lone_bound_is_untouched(db):
    """One end is a real, usable bound and is left alone — only a *pair* can be inverted."""
    patient = await _patient(db)

    rows = await _merge(db, patient, _lab("eGFR", 42.0, low=90.0, high=None))

    assert rows[0].reference_range_low == Decimal("90")
    assert rows[0].is_abnormal is True
    assert rows[0].abnormality_direction == "low"


async def test_the_agents_record_summary_no_longer_carries_the_fabricated_finding(db):
    """Why this matters beyond the row: ``is_abnormal`` is what the panel reads as fact.

    ``summarize_snapshot`` filters the chart's labs down to the abnormal ones and hands that
    line to all eight agents as the patient's history. Both markers below are merged together
    and the summary is built from the resulting rows, so what is asserted is the end of the real
    path rather than a hand-built dict: the genuinely abnormal creatinine is reported, and the
    potassium whose only abnormality was a transposed range is not.
    """
    from app.agents.tools import summarize_snapshot

    patient = await _patient(db)
    rows = await _merge(
        db,
        patient,
        _lab("Potassium", 4.2, low=5.1, high=3.5),  # transposed pair
        _lab("Creatinine", 3.8, low=0.6, high=1.2),  # genuinely high
    )

    summary = summarize_snapshot(
        {
            "lab_results": [
                {
                    "marker_name": row.marker_name,
                    "value_numeric": float(row.value_numeric) if row.value_numeric else None,
                    "unit": row.unit,
                    "is_abnormal": row.is_abnormal,
                }
                for row in rows
            ]
        }
    )

    assert "Creatinine" in summary
    assert "Potassium" not in summary
