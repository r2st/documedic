"""Deterministic text parser — the offline/no-API-key extraction path.

Misclassification here is a data-integrity problem, not just a formatting one: a lab value
read as a medication becomes a fake MedicationEvent in the longitudinal record the moment a
hurried clinician approves the extraction, and the drug-safety engine then reasons about a
drug the patient was never on.
"""

from __future__ import annotations

from app.services.extraction.text_parser import parse_text


def _by_type(text: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for entity in parse_text(text):
        out.setdefault(entity.entity_type, []).append({f.name: f.value for f in entity.fields})
    return out


# --- Section-driven parsing ------------------------------------------------------------


def test_medications_section_is_parsed():
    parsed = _by_type("RX:\nGlycomet 500mg BD\nTelma 40mg OD\n")
    assert [m["brand_name_raw"] for m in parsed["medication"]] == ["Glycomet", "Telma"]
    assert parsed["medication"][0]["dose"] == "500"
    assert parsed["medication"][0]["frequency"] == "BD"


def test_labs_section_with_colon_separated_values():
    parsed = _by_type("LABS:\nHbA1c: 9.2 % (4.0-5.6)\n")
    lab = parsed["lab_result"][0]
    assert lab["marker_name"] == "HbA1c"
    assert lab["value_numeric"] == 9.2
    assert lab["reference_range_low"] == 4.0
    assert lab["reference_range_high"] == 5.6


def test_conditions_section_is_parsed():
    parsed = _by_type("DIAGNOSIS:\n- Type 2 diabetes mellitus\n")
    assert parsed["condition"][0]["condition_name"] == "Type 2 diabetes mellitus"


def test_allergies_section_splits_the_reaction():
    parsed = _by_type("ALLERGIES:\nPenicillin - urticaria\n")
    allergy = parsed["allergy"][0]
    assert allergy["allergen_name"] == "Penicillin"
    assert allergy["reaction_description"] == "urticaria"


def test_section_headers_are_case_insensitive():
    assert _by_type("labs:\nHbA1c: 9.2 %\n") == _by_type("LABS\nHbA1c: 9.2 %\n")


# --- Columnar lab reports --------------------------------------------------------------


COLUMNAR_REPORT = """
LABORATORY REPORT
HbA1c            8.4 %      (4.0 - 5.6)
Serum creatinine 1.3 mg/dL  (0.7 - 1.3)
Haemoglobin      9.1 g/dL   (13.0 - 17.0)
"""


def test_a_columnar_lab_report_is_read_as_labs_not_medications():
    """Regression: printed lab reports use whitespace columns with no ':' separator, and
    every line used to fall through to the medication grammar."""
    parsed = _by_type(COLUMNAR_REPORT)
    assert "medication" not in parsed
    assert [lab["marker_name"] for lab in parsed["lab_result"]] == [
        "HbA1c",
        "Serum creatinine",
        "Haemoglobin",
    ]


def test_columnar_labs_keep_their_values_units_and_ranges():
    parsed = _by_type(COLUMNAR_REPORT)
    creatinine = parsed["lab_result"][1]
    assert creatinine["value_numeric"] == 1.3
    assert creatinine["unit"] == "mg/dL"
    assert creatinine["reference_range_low"] == 0.7
    assert creatinine["reference_range_high"] == 1.3


def test_laboratory_report_is_recognised_as_a_section_header():
    """'LABORATORY REPORT' is the most common header on Indian lab printouts."""
    parsed = _by_type("LABORATORY REPORT\nHbA1c: 8.4 %\n")
    assert "lab_result" in parsed


def test_columnar_labs_are_found_without_any_section_header():
    parsed = _by_type("HbA1c            8.4 %      (4.0 - 5.6)\n")
    assert parsed["lab_result"][0]["marker_name"] == "HbA1c"


# --- Discrimination between the two grammars -------------------------------------------


def test_a_prescription_dose_schedule_is_not_mistaken_for_a_reference_range():
    """'(1-0-1)' is a morning-noon-night schedule, not a lab range — the columnar lab
    pattern requires exactly two numbers, so this must stay a medication."""
    parsed = _by_type("Tab Amlodipine 5mg (1-0-1)\n")
    assert "lab_result" not in parsed
    assert parsed["medication"][0]["brand_name_raw"].endswith("Amlodipine")


def test_a_plain_prescription_line_stays_a_medication():
    parsed = _by_type("Crocin 650mg BD\n")
    assert "lab_result" not in parsed
    assert parsed["medication"][0]["dose"] == "650"


def test_an_explicit_medications_section_wins_over_lab_shaped_lines():
    """Inside RX:, the medication parser is authoritative regardless of line shape."""
    parsed = _by_type("RX:\nCrocin 650mg BD\n")
    assert "lab_result" not in parsed


# --- Robustness ------------------------------------------------------------------------


def test_empty_and_whitespace_input_yields_nothing():
    assert parse_text("") == []
    assert parse_text("\n\n   \n") == []


def test_unparseable_prose_yields_nothing():
    assert parse_text("Patient reviewed in clinic today and is doing well.\n") == []


def test_a_section_header_alone_produces_no_entities():
    assert parse_text("ALLERGIES:\n") == []


def test_a_line_matching_a_section_alias_is_not_parsed_as_content():
    parsed = _by_type("ALLERGIES:\nallergies\nPenicillin\n")
    assert [a["allergen_name"] for a in parsed["allergy"]] == ["Penicillin"]


def test_every_field_carries_a_confidence_score():
    """Downstream banding (high/medium/low) drives whether the clinician must confirm."""
    for entity in parse_text("RX:\nGlycomet 500mg BD\n"):
        for f in entity.fields:
            assert 0.0 <= f.confidence <= 1.0


def test_a_missing_unit_scores_lower_confidence_than_a_present_one():
    with_unit = parse_text("RX:\nGlycomet 500mg BD\n")[0]
    without_unit = parse_text("RX:\nGlycomet 500 BD\n")[0]
    unit_conf = {f.name: f.confidence for f in with_unit.fields}["dose_unit"]
    no_unit_conf = {f.name: f.confidence for f in without_unit.fields}["dose_unit"]
    assert no_unit_conf < unit_conf
