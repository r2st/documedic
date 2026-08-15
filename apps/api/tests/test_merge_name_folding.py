"""Condition and allergy dedup folds a name the way the medication arm already did.

``_med_identity`` states the rule and the reason: a key is "folded exactly as the caller folds
the incoming line — lowercased *and* trimmed. Both halves have to agree or the comparison is
against a value nothing produces: OCR pads what it reads." That held for drugs and for nothing
else. Conditions and allergens keyed on ``name.lower()`` alone, so a diagnosis first charted from
a scan as ``"  Type 2 Diabetes Mellitus "`` never matched the same diagnosis read cleanly off the
next document, and the chart carried it twice.

A duplicated condition or allergy is not inert. Every contraindication, guideline-adherence and
allergy rule in ``core.safety`` iterates these lists, so a second copy raises the same flag a
second time — on the screen whose whole purpose is to be read carefully rather than scrolled
past. Nothing was mis-evaluated (``core.safety._norm`` folds both ways before it matches); there
were simply two of everything.

The same guard also stops a name that is only whitespace being charted at all. It used to pass
the ``if not name`` check, write a blank-named row, and take the empty string as its dedup key —
which then collided with every other unnamed row on the chart.
"""

from __future__ import annotations

import pytest

from app.models.allergy import Allergy
from app.models.condition import Condition
from app.services.graph_service import GraphService
from tests.test_graph_service_merge_dedup import _document, _entity, _patient, _rows

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------- padding across documents


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Type 2 Diabetes Mellitus", "  Type 2 Diabetes Mellitus "),
        ("  Type 2 Diabetes Mellitus ", "Type 2 Diabetes Mellitus"),
        ("Type 2 Diabetes Mellitus", "type 2 diabetes mellitus\n"),
        ("\tHypertension", "Hypertension  "),
    ],
)
async def test_a_padded_diagnosis_is_the_diagnosis_already_charted(db, first, second):
    """Two documents, one condition. The padding is whatever the scanner left on the line."""
    patient = await _patient(db)
    service = GraphService(db)

    assert (
        await service.merge_entities(
            patient=patient,
            document=await _document(db, patient, "first.pdf"),
            entities=[_entity("condition", condition_name=first)],
        )
    )["conditions"] == 1
    assert (
        await service.merge_entities(
            patient=patient,
            document=await _document(db, patient, "second.pdf"),
            entities=[_entity("condition", condition_name=second)],
        )
    )["conditions"] == 0

    assert len(await _rows(db, Condition, patient)) == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Penicillin", " Penicillin  "),
        (" Penicillin  ", "Penicillin"),
        ("Penicillin", "penicillin\t"),
    ],
)
async def test_a_padded_allergen_is_the_allergy_already_charted(db, first, second):
    """The one that matters most: the allergy list is what the hard blocks are computed from,
    and a second copy raises the same block twice."""
    patient = await _patient(db)
    service = GraphService(db)

    assert (
        await service.merge_entities(
            patient=patient,
            document=await _document(db, patient, "first.pdf"),
            entities=[_entity("allergy", allergen_name=first)],
        )
    )["allergies"] == 1
    assert (
        await service.merge_entities(
            patient=patient,
            document=await _document(db, patient, "second.pdf"),
            entities=[_entity("allergy", allergen_name=second)],
        )
    )["allergies"] == 0

    assert len(await _rows(db, Allergy, patient)) == 1


async def test_padding_within_one_document_charts_one_row(db):
    """The in-payload half of the same key. One report listing a diagnosis twice, once padded,
    is one diagnosis — the merge folds ``seen`` as it goes, so both halves are covered."""
    patient = await _patient(db)
    document = await _document(db, patient)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=document,
        entities=[
            _entity("condition", condition_name="Chronic Kidney Disease"),
            _entity("condition", condition_name=" chronic kidney disease "),
            _entity("allergy", allergen_name="Sulfa"),
            _entity("allergy", allergen_name="  SULFA"),
        ],
    )

    assert counts["conditions"] == 1
    assert counts["allergies"] == 1


# ------------------------------------------------------------------- genuinely different names


async def test_two_different_diagnoses_are_still_two(db):
    """The folding must not over-collapse. Trimming and lowercasing are all it does."""
    patient = await _patient(db)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=await _document(db, patient),
        entities=[
            _entity("condition", condition_name="Type 2 Diabetes Mellitus"),
            _entity("condition", condition_name="Type 1 Diabetes Mellitus"),
        ],
    )

    assert counts["conditions"] == 2


async def test_the_name_is_charted_as_the_document_wrote_it(db):
    """Only the *key* is folded, exactly as ``_med_identity`` folds only the comparison. The
    stored name stays as extracted, so the chart shows what the source document said and a
    clinician comparing the two sees the same string."""
    patient = await _patient(db)

    await GraphService(db).merge_entities(
        patient=patient,
        document=await _document(db, patient),
        entities=[_entity("condition", condition_name="Type 2 Diabetes Mellitus")],
    )

    assert [c.condition_name for c in await _rows(db, Condition, patient)] == [
        "Type 2 Diabetes Mellitus"
    ]


# ------------------------------------------------------------------------------ nothing at all


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  "])
async def test_a_whitespace_only_diagnosis_is_not_charted(db, blank):
    """A blank-named condition is not a diagnosis, and it used to take the empty string as its
    dedup key — so the first one charted suppressed every later unnamed condition too."""
    patient = await _patient(db)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=await _document(db, patient),
        entities=[_entity("condition", condition_name=blank)],
    )

    assert counts["conditions"] == 0
    assert await _rows(db, Condition, patient) == []


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_a_whitespace_only_allergen_is_not_charted(db, blank):
    """A blank row on the safety board is an allergy a clinician can neither act on nor
    dismiss."""
    patient = await _patient(db)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=await _document(db, patient),
        entities=[_entity("allergy", allergen_name=blank)],
    )

    assert counts["allergies"] == 0
    assert await _rows(db, Allergy, patient) == []


async def test_a_blank_name_does_not_suppress_the_real_ones_beside_it(db):
    """The collision the empty-string key caused, stated directly: an unnamed entity must not be
    able to take a key that anything else could match."""
    patient = await _patient(db)

    counts = await GraphService(db).merge_entities(
        patient=patient,
        document=await _document(db, patient),
        entities=[
            _entity("condition", condition_name="   "),
            _entity("condition", condition_name="Hypertension"),
            _entity("allergy", allergen_name=" "),
            _entity("allergy", allergen_name="Ibuprofen"),
        ],
    )

    assert counts["conditions"] == 1
    assert counts["allergies"] == 1
    assert [c.condition_name for c in await _rows(db, Condition, patient)] == ["Hypertension"]
    assert [a.allergen_name for a in await _rows(db, Allergy, patient)] == ["Ibuprofen"]
