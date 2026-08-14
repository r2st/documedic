"""What the two extraction paths do with input they cannot represent or cannot read.

Two failure classes, both arriving from the same place — a scanner or a vision model rather
than anything a person typed:

**Values no JSON boundary can carry.** The value grammars match an unbounded digit run, which is
right, but ``float()`` of ~309 digits is ``inf`` rather than an error, and ``json.loads`` turns
``1e999`` and a bare ``NaN`` into floats just as quietly. Extracted entities are returned to the
clinician for approval and written to the document's JSON meta column *before* anything
numeric-aware sees them, so a non-finite value 500'd the upload with "Out of range float values
are not JSON compliant" and put the non-standard token ``Infinity`` into meta.
``graph_service._to_decimal`` already refuses non-finite values, but it sits downstream of the
serialisation that fails — R28/R29 covered the column, not the wire.

**Responses shaped differently from the prompt.** ``_to_entities`` walked the model's reply with
``.get``/``.items`` at every level. A list of bare strings where objects belong, or ``fields`` as
a string, raised — and because ``extract`` catches per provider, one malformed entity cost the
document every other entity that had extracted correctly, and usually the vision path entirely.

The contract asserted throughout: nothing non-finite leaves extraction, the reading is preserved
as text where the source token still exists, malformed entries are dropped rather than fatal, and
well-formed entries beside them survive.
"""

from __future__ import annotations

import json
import math
import uuid

import pytest
from sqlalchemy import select

from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.models.user import Account
from app.services.extraction import claude_client
from app.services.extraction.pipeline import ExtractionPipeline
from app.services.extraction.text_parser import parse_text
from app.services.graph_service import GraphService

# Comfortably past the ~309-digit point where float() saturates to infinity.
_OVERFLOWING = "9" * 400


async def _patient(db) -> Patient:
    account = Account(email=f"{uuid.uuid4()}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id, full_name="Extraction Probe", sex="male", consent_given=True
    )
    db.add(patient)
    await db.flush()
    return patient


def _fields(entities, entity_type: str) -> dict:
    for entity in entities:
        if entity.entity_type == entity_type:
            return {f.name: f.value for f in entity.fields}
    raise AssertionError(f"no {entity_type} entity was extracted")


def _assert_json_safe(payload: object) -> str:
    """Round-trip through the encoder the approval response and the meta column both use.

    ``json.dumps`` emits ``Infinity`` for a non-finite float instead of failing, so asserting on
    dumps alone would not have caught this. The strict re-read is what a jsonb column and any
    conforming client do.
    """

    def _reject(token: str) -> float:
        raise AssertionError(f"non-standard JSON token {token!r} reached the wire")

    dumped = json.dumps(payload)
    json.loads(dumped, parse_constant=_reject)
    return dumped


# ---------------------------------------------------------------- deterministic text parser


def test_a_lab_value_too_long_to_be_a_float_does_not_become_infinity():
    """``float("9" * 400)`` is ``inf``, which cannot be serialised back to the clinician."""
    fields = _fields(parse_text(f"LABS\nSodium: {_OVERFLOWING} mmol/L"), "lab_result")

    value = fields["value_numeric"]
    assert not isinstance(value, float) or math.isfinite(value)


def test_an_unrepresentable_lab_value_keeps_the_digits_the_document_printed():
    """The reading is preserved as text, not dropped.

    ``_merge_lab`` files a value it cannot store as a number into ``value_text``, which makes the
    row qualitative — the right handling for a reading nobody can interpret. Dropping the field
    instead would show the clinician a marker with no value beside it.
    """
    fields = _fields(parse_text(f"LABS\nSodium: {_OVERFLOWING} mmol/L"), "lab_result")

    assert fields["value_numeric"] == _OVERFLOWING
    assert fields["marker_name"] == "Sodium"
    assert fields["unit"] == "mmol/L"


def test_a_reference_range_that_overflows_is_dropped_at_both_ends():
    """A range with one unrepresentable end is not a narrower range.

    ``_merge_lab`` screens the value as abnormal against whichever bound is present, so keeping
    the readable half of a broken range would flag against a limit the report never set.
    """
    fields = _fields(
        parse_text(f"LABS\nHbA1c: 9.2 % ({_OVERFLOWING}-{_OVERFLOWING})"), "lab_result"
    )

    assert fields["value_numeric"] == 9.2
    assert "reference_range_low" not in fields
    assert "reference_range_high" not in fields


def test_a_normal_reference_range_is_still_read():
    """Guard against the overflow fix suppressing ordinary ranges."""
    fields = _fields(parse_text("LABS\nHbA1c: 9.2 % (4.0-5.6)"), "lab_result")

    assert fields["reference_range_low"] == 4.0
    assert fields["reference_range_high"] == 5.6


@pytest.mark.parametrize(
    "line",
    [
        f"Sodium: {_OVERFLOWING} mmol/L",
        f"Sodium: 1{'0' * 400} mmol/L",
        f"Sodium: {_OVERFLOWING}.5 mmol/L",
    ],
)
def test_no_overflowing_shape_reaches_the_wire_as_a_non_finite_float(line: str):
    entities = parse_text(f"LAB REPORT\n{line}")
    payload = [
        {
            "entity_type": e.entity_type,
            "fields": [{"name": f.name, "value": f.value} for f in e.fields],
        }
        for e in entities
    ]
    _assert_json_safe(payload)


def test_an_ordinary_lab_line_is_unaffected():
    fields = _fields(parse_text("LABS\nPotassium: 5.4 mmol/L (3.5-5.1)"), "lab_result")

    assert fields["value_numeric"] == 5.4
    assert isinstance(fields["value_numeric"], float)


def test_the_whole_pipeline_output_is_serialisable_for_a_garbled_report():
    """End to end through ``ExtractionPipeline``, which is what the upload handler calls."""
    text = f"LAB REPORT\nSample Date: 12/03/2026\nSodium: {_OVERFLOWING} mmol/L (135-145)\n"
    result = ExtractionPipeline().run(b"", "text/plain", raw_text=text)

    assert result.entities
    _assert_json_safe(
        [
            {
                "entity_type": e.entity_type,
                "fields": [
                    {"name": f.name, "value": f.value, "confidence": f.confidence} for f in e.fields
                ],
            }
            for e in result.entities
        ]
    )


@pytest.mark.asyncio
async def test_an_unrepresentable_value_merges_as_a_qualitative_row(db):
    """The clinician approving it must get a row that records what was read, not a number.

    This is the end the column guard already defended; the point here is that the value now
    arrives at it as text rather than as an ``inf`` that never survived serialisation.
    """
    patient = await _patient(db)
    fields = _fields(parse_text(f"LABS\nSodium: {_OVERFLOWING} mmol/L"), "lab_result")
    await GraphService(db).merge_entities(
        patient=patient, document=None, entities=[{"entity_type": "lab_result", "fields": fields}]
    )
    await db.commit()

    lab = (await db.execute(select(LabResult))).scalar_one()
    assert lab.value_numeric is None, "an uninterpretable reading was stored as a number"
    assert lab.value_text and lab.value_text.startswith("999")
    assert lab.marker_name == "Sodium"


# ------------------------------------------------------------------------- vision path


def test_a_model_value_of_1e999_is_dropped_rather_than_carried_as_infinity():
    """``json.loads`` parses ``1e999`` to ``inf`` without complaint.

    Unlike the text parser there is no source token to fall back to here — the raw text was
    consumed by the model — so the field is dropped rather than kept as unreadable text.
    """
    payload = json.loads(
        '{"entities":[{"entity_type":"lab_result",'
        '"fields":{"marker_name":"Sodium","value_numeric":1e999}}]}'
    )
    entities, _ = claude_client._to_entities(payload)

    fields = _fields(entities, "lab_result")
    assert "value_numeric" not in fields
    assert fields["marker_name"] == "Sodium"


@pytest.mark.parametrize("literal", ["1e999", "-1e999", "NaN", "Infinity", "-Infinity"])
def test_every_non_finite_literal_json_accepts_is_dropped(literal: str):
    payload = json.loads(
        '{"entities":[{"entity_type":"lab_result","fields":'
        f'{{"marker_name":"Sodium","value_numeric":{literal}}}}}]}}'
    )
    entities, _ = claude_client._to_entities(payload)

    for entity in entities:
        for field in entity.fields:
            assert not isinstance(field.value, float) or math.isfinite(field.value)
    _assert_json_safe([{"fields": [{"value": f.value} for f in e.fields]} for e in entities])


def test_a_finite_model_value_is_kept():
    """Guard against the non-finite filter swallowing ordinary numbers, including zero."""
    entities, _ = claude_client._to_entities(
        {
            "entities": [
                {
                    "entity_type": "lab_result",
                    "fields": {"marker_name": "Sodium", "value_numeric": 0, "unit": "mmol/L"},
                }
            ]
        }
    )

    fields = _fields(entities, "lab_result")
    assert fields["value_numeric"] == 0
    assert fields["unit"] == "mmol/L"


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "a", "dict"],  # the whole reply is a list
        "a sentence",
        None,
        {"entities": ["medication"]},  # entities as bare strings
        {"entities": "medication"},
        {"entities": {"first": {"entity_type": "medication"}}},  # object where a list belongs
        {"entities": [None, 7]},
        {"entities": [{"entity_type": "medication", "fields": "Glycomet 500mg"}]},  # fields a str
        {"entities": [{"entity_type": "medication", "fields": ["Glycomet"]}]},
    ],
)
def test_a_malformed_response_yields_nothing_instead_of_raising(payload: object):
    """``extract`` catches per provider, so raising here abandoned the whole vision path."""
    entities, doc_type = claude_client._to_entities(payload)

    assert entities == []
    assert doc_type is None


def test_the_well_formed_entities_survive_beside_a_malformed_one():
    """The point of dropping rather than raising: one bad entity is not the whole document."""
    entities, doc_type = claude_client._to_entities(
        {
            "document_type": "lab_report",
            "entities": [
                "Glycomet 500mg BD",  # bare string — unusable
                {"entity_type": "lab_result", "fields": {"marker_name": "HbA1c"}},
                {"entity_type": "medication", "fields": "not an object"},
                {"entity_type": "condition", "fields": {"condition_name": "Type 2 diabetes"}},
            ],
        }
    )

    assert doc_type == "lab_report"
    assert {e.entity_type for e in entities} == {"lab_result", "condition"}


def test_an_entity_with_no_type_is_dropped_rather_than_shown_as_recorded():
    """``GraphService`` dispatches on ``entity_type``.

    An untyped entity merges nowhere, but it was still offered to the clinician for approval —
    which reads as "this will be recorded" for something that silently will not be.
    """
    entities, _ = claude_client._to_entities(
        {
            "entities": [
                {"fields": {"brand_name_raw": "Glycomet"}},
                {"entity_type": "", "fields": {"brand_name_raw": "Dolo"}},
                {"entity_type": {"kind": "medication"}, "fields": {"brand_name_raw": "Crocin"}},
                {"entity_type": "medication", "fields": {"brand_name_raw": "Augmentin"}},
            ]
        }
    )

    assert [e.entity_type for e in entities] == ["medication"]
    assert entities[0].fields[0].value == "Augmentin"


def test_a_non_dict_confidence_map_falls_back_instead_of_losing_the_entity():
    """The scores are advisory; the extracted values are not. A bad map must not cost both."""
    entities, _ = claude_client._to_entities(
        {
            "entities": [
                {
                    "entity_type": "medication",
                    "fields": {"brand_name_raw": "Glycomet"},
                    "confidence": "high",
                }
            ]
        }
    )

    fields = _fields(entities, "medication")
    assert fields["brand_name_raw"] == "Glycomet"


def test_a_non_string_document_type_is_not_carried_onwards():
    entities, doc_type = claude_client._to_entities(
        {
            "document_type": {"kind": "lab_report"},
            "entities": [{"entity_type": "lab_result", "fields": {"marker_name": "HbA1c"}}],
        }
    )

    assert doc_type is None
    assert entities


def test_a_non_string_document_date_is_not_inherited_as_a_sample_date():
    """``_inherit_document_date`` already guards this; pinned so the guard is not lost."""
    entities, _ = claude_client._to_entities(
        {
            "document_date": 20260312,
            "entities": [{"entity_type": "lab_result", "fields": {"marker_name": "HbA1c"}}],
        }
    )

    assert "sample_date" not in _fields(entities, "lab_result")


def test_a_well_formed_document_date_is_still_inherited():
    entities, _ = claude_client._to_entities(
        {
            "document_date": "2026-03-12",
            "entities": [{"entity_type": "lab_result", "fields": {"marker_name": "HbA1c"}}],
        }
    )

    assert _fields(entities, "lab_result")["sample_date"] == "2026-03-12"
