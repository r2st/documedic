"""The report's own date, read off the report and attached to the results it dates.

Nothing in the extraction layer used to emit ``sample_date``. Two things followed from that,
and both are the subject of this module:

1. Every lab ingested through a document landed with ``sample_date = NULL``, so "the patient's
   most recent potassium" — the question the panic-value screen exists to answer — degraded to
   ingestion order. File an old report after a new one and the stale value screens as current.
2. The line carrying that date, "Sample Collected on: 12/03/2026", has the same shape as a lab
   line, so it parsed as a marker named "Sample Date" with a value of 12. Approved once, that
   is a fabricated result in the longitudinal record.

The masthead of a printed report is full of lines with that shape — age, accession number,
bill number — so the fix covers the family, not just the date. What it must not do is silence
a real clinical line that happens to start with an administrative-sounding word, which is what
``test_a_diagnosis_beginning_with_a_denylisted_word_survives`` pins.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.extraction.claude_client import _to_entities
from app.services.extraction.text_parser import parse_text
from app.services.graph_service import _parse_date, _parse_datetime

# A page from an Indian pathology lab: masthead, then the results block.
PRINTED_REPORT = """
CITY DIAGNOSTICS - LABORATORY REPORT
Patient Name: Ramesh Kumar
Age: 54 Years
Sex: Male
Ref. By: Dr. S. Menon
Lab No.: 4471
Sample Collected on: 12/03/2026 09:15 AM
Report Date: 14/03/2026
Potassium: 7.4 mmol/L (3.5-5.1)
Creatinine       1.8 mg/dL   (0.7 - 1.3)
"""


def _entities(text: str) -> list[dict]:
    return [
        {"entity_type": e.entity_type, **{f.name: f.value for f in e.fields}}
        for e in parse_text(text)
    ]


def _labs(text: str) -> list[dict]:
    return [e for e in _entities(text) if e["entity_type"] == "lab_result"]


def _confidences(text: str, field_name: str) -> list[float]:
    return [f.confidence for e in parse_text(text) for f in e.fields if f.name == field_name]


# --- The date line stops being a lab result ---------------------------------------------


def test_the_collection_date_line_is_not_ingested_as_a_marker():
    """The regression this module is named for.

    "Sample Collected on: 12/03/2026" matches the lab grammar — a word, a colon, a number —
    and became a result. It is not a measurement of anything.
    """
    assert [lab["marker_name"] for lab in _labs(PRINTED_REPORT)] == ["Potassium", "Creatinine"]


@pytest.mark.parametrize(
    "line",
    [
        "Sample Date: 12/03/2026",
        "Collection Date: 12/03/2026",
        "Collected on: 12-Mar-2026",
        "Date of Collection: 2026-03-12",
        "Report Date: 12/03/2026",
        "Reported on: March 12, 2026",
        "Received on: 12/03/2026",
        "Date: 12/03/2026",
    ],
    ids=lambda line: line.split(":")[0],
)
def test_no_spelling_of_a_report_date_becomes_a_lab_result(line):
    """Every lab prints this line its own way; one recognised spelling is not a fix."""
    assert _labs(f"LABS:\n{line}\nPotassium: 4.0 mmol/L (3.5-5.1)\n") == [
        {
            "entity_type": "lab_result",
            "marker_name": "Potassium",
            "value_numeric": 4.0,
            "unit": "mmol/L",
            "reference_range_low": 3.5,
            "reference_range_high": 5.1,
            "sample_date": "2026-03-12T00:00:00",
        }
    ]


def test_the_administrative_masthead_produces_no_entities_at_all():
    """Age, accession number and referring doctor are not markers either, and each of them
    reached the record by the same route as the date line."""
    assert _entities(PRINTED_REPORT[: PRINTED_REPORT.index("Potassium")]) == []


# --- ...and becomes the sample date instead ---------------------------------------------


def test_every_lab_on_the_report_carries_the_reports_collection_date():
    assert [lab["sample_date"] for lab in _labs(PRINTED_REPORT)] == [
        "2026-03-12T09:15:00",
        "2026-03-12T09:15:00",
    ]


def test_the_time_of_day_is_kept_when_the_report_prints_one():
    """Two draws in one day is ordinary on an inpatient chart — a repeat potassium after
    treatment. Truncated to midnight they tie, and which one is "latest" falls back to
    ingestion order, which is the failure this whole change is closing."""
    labs = _labs(
        "Sample Collected on: 12/03/2026 09:15 AM\nLABS:\nPotassium: 7.4 mmol/L (3.5-5.1)\n"
    )
    assert labs[0]["sample_date"] == "2026-03-12T09:15:00"


def test_an_indian_report_date_is_read_day_first():
    """12/03/2026 is 12 March in every Indian lab printout, not 3 December. Read the other way
    round it is a *later* date, so a stale report outranks the current one on the ordering the
    panic screen uses."""
    labs = _labs("Sample Date: 12/03/2026\nLABS:\nPotassium: 4.0 mmol/L (3.5-5.1)\n")
    assert labs[0]["sample_date"].startswith("2026-03-12")


def test_the_collection_date_wins_over_the_report_date():
    """Both are printed on ``PRINTED_REPORT``, two days apart. ``sample_date`` means when the
    blood was drawn; a send-out panel can be reported days later, and dating the result by the
    report would order it after a draw that actually came first."""
    assert _labs(PRINTED_REPORT)[0]["sample_date"].startswith("2026-03-12")


def test_a_report_date_is_used_when_no_collection_date_is_printed():
    """Better than nothing: many prescriptions and smaller labs print only one date."""
    labs = _labs("Report Date: 14/03/2026\nLABS:\nPotassium: 4.0 mmol/L (3.5-5.1)\n")
    assert labs[0]["sample_date"] == "2026-03-14T00:00:00"


def test_a_report_date_is_scored_low_enough_to_need_the_clinicians_eye():
    """A report date is a proxy for the draw date, not the draw date. The confidence band is
    what puts the field in front of the clinician before it is merged, so the proxy has to
    score below the threshold a collection date clears."""
    lab_line = "LABS:\nK: 4.0 mmol/L (3.5-5.1)\n"
    collected = _confidences(f"Sample Collected on: 12/03/2026\n{lab_line}", "sample_date")
    reported = _confidences(f"Report Date: 14/03/2026\n{lab_line}", "sample_date")
    assert reported < collected


def test_a_masthead_below_the_results_still_dates_them():
    """Plenty of report layouts print the collection date in a footer. The date is applied
    after the whole page is walked for exactly this."""
    labs = _labs("LABS:\nPotassium: 7.4 mmol/L (3.5-5.1)\nSample Collected on: 12/03/2026\n")
    assert labs[0]["sample_date"] == "2026-03-12T00:00:00"


def test_a_report_with_no_legible_date_leaves_the_labs_undated():
    """No invented dates. ``sample_date`` is nullable and the ranked read handles NULL — a
    guessed date would silently outrank a real one."""
    labs = _labs("LABS:\nPotassium: 7.4 mmol/L (3.5-5.1)\n")
    assert "sample_date" not in labs[0]


def test_a_date_label_with_an_unreadable_value_dates_nothing():
    """OCR returns "Sample Date: --" often enough. dateutil would happily read a bare number
    as a day of the current month, inventing a date out of a line the parser did not
    understand — which is the failure mode being fixed, not a fix for it."""
    labs = _labs("Sample Date: --\nLABS:\nPotassium: 7.4 mmol/L (3.5-5.1)\n")
    assert "sample_date" not in labs[0]
    assert [lab["marker_name"] for lab in labs] == ["Potassium"]


def test_only_lab_results_are_dated():
    """``sample_date`` is a property of a specimen. A medication event has its own date field
    with different semantics (when it was prescribed), and inferring it from a lab report's
    header would be a fabrication."""
    parsed = _entities("Sample Date: 12/03/2026\nRX:\nGlycomet 500mg BD\n")
    assert all("sample_date" not in e for e in parsed)


# --- What the fix must not break ---------------------------------------------------------


def test_a_diagnosis_beginning_with_a_denylisted_word_survives():
    """ "Status" is a masthead label ("Report Status: Final") and the first word of a real
    diagnosis. Dropping the line by its first word alone would delete clinical data, which is
    a strictly worse failure than the fabricated marker this change removes — so a colon-less
    line is only dropped when its value is numeric, as a masthead field's always is."""
    parsed = _entities("DIAGNOSIS:\n- Status epilepticus\n- Status asthmaticus\n")
    assert [e["condition_name"] for e in parsed] == ["Status epilepticus", "Status asthmaticus"]


def test_a_marker_whose_name_looks_administrative_is_still_read():
    """A colon-less "Age 54 Years" is masthead; a marker line is discriminated by its
    reference range, which no masthead field carries."""
    labs = _labs("Age 54 Years\nSodium           138 mmol/L  (135 - 145)\n")
    assert [lab["marker_name"] for lab in labs] == ["Sodium"]


def test_a_prescription_is_unaffected_by_the_masthead_handling():
    parsed = _entities("Patient Name: Ramesh Kumar\nRX:\nGlycomet 500mg BD\nTelma 40mg OD\n")
    assert [e["brand_name_raw"] for e in parsed] == ["Glycomet", "Telma"]


def test_a_date_of_birth_line_is_not_read_as_the_sample_date():
    """ "Date of Birth" contains "Date". Matching the label as a whole rather than as a
    fragment is what keeps a 1972 DOB from dating a 2026 potassium — which would put the
    result last in every longitudinal ordering it appears in."""
    labs = _labs("Date of Birth: 04/07/1972\nLABS:\nPotassium: 7.4 mmol/L (3.5-5.1)\n")
    assert "sample_date" not in labs[0]


# --- The vision path lands the same shape ------------------------------------------------


def test_a_vision_extracted_lab_inherits_the_documents_date():
    """The model reports the collection date once, in the header, where the report prints it.
    Asking it to repeat the date on every marker invites it to invent one per row."""
    entities, _ = _to_entities(
        {
            "document_type": "lab_report",
            "document_date": "2026-03-12",
            "entities": [
                {"entity_type": "lab_result", "fields": {"marker_name": "K", "value_numeric": 7.4}},
                {
                    "entity_type": "lab_result",
                    "fields": {"marker_name": "Na", "value_numeric": 138},
                },
            ],
        }
    )
    assert [{f.name: f.value for f in e.fields}["sample_date"] for e in entities] == [
        "2026-03-12",
        "2026-03-12",
    ]


def test_a_result_the_model_dated_itself_keeps_its_own_date():
    """A panel spanning two draws is why the per-result field exists at all."""
    entities, _ = _to_entities(
        {
            "document_date": "2026-03-12",
            "entities": [
                {
                    "entity_type": "lab_result",
                    "fields": {"marker_name": "Potassium", "sample_date": "2026-03-14"},
                }
            ],
        }
    )
    assert {f.name: f.value for f in entities[0].fields}["sample_date"] == "2026-03-14"


def test_only_vision_lab_results_inherit_the_document_date():
    entities, _ = _to_entities(
        {
            "document_date": "2026-03-12",
            "entities": [
                {"entity_type": "medication", "fields": {"brand_name_raw": "Glycomet"}},
                {"entity_type": "allergy", "fields": {"allergen_name": "Penicillin"}},
            ],
        }
    )
    assert all(f.name != "sample_date" for e in entities for f in e.fields)


@pytest.mark.parametrize("document_date", [None, "", "   ", 20260312])
def test_an_absent_or_unusable_document_date_dates_nothing(document_date):
    """Structured output is a request, not a guarantee. A missing date leaves the labs
    undated, which the ranked read already handles."""
    entities, _ = _to_entities(
        {
            "document_date": document_date,
            "entities": [{"entity_type": "lab_result", "fields": {"marker_name": "Potassium"}}],
        }
    )
    assert all(f.name != "sample_date" for f in entities[0].fields)


def test_a_malformed_per_field_confidence_does_not_lose_the_document():
    """The model is asked for a number per field and mostly obliges. "high" and null both turn
    up, and ``float()`` on either raised straight out of extraction — throwing away a whole
    page of correctly-read values over one bad score."""
    entities, _ = _to_entities(
        {
            "entities": [
                {
                    "entity_type": "lab_result",
                    "fields": {"marker_name": "Potassium", "value_numeric": 7.4},
                    "confidence": {"marker_name": "high", "value_numeric": None},
                }
            ]
        }
    )
    assert [f.confidence for f in entities[0].fields] == [0.7, 0.7]


def test_an_out_of_range_confidence_is_clamped_not_dropped():
    """The band a score lands in is what it means; a 1.4 is a high-confidence read expressed
    badly, and dropping the field would lose a value the model actually read."""
    entities, _ = _to_entities(
        {
            "entities": [
                {
                    "entity_type": "lab_result",
                    "fields": {"marker_name": "Potassium", "value_numeric": 7.4},
                    "confidence": {"marker_name": 1.4, "value_numeric": -2},
                }
            ]
        }
    )
    assert [f.confidence for f in entities[0].fields] == [1.0, 0.0]


# --- The merge refuses an implausible date -----------------------------------------------


def test_a_sample_date_in_the_future_is_refused():
    """OCR misreads digits, and "2026" comes back "2126". ``sample_date`` is the primary sort
    key for "most recent value per marker", so one future-dated row outranks every real result
    for that marker and pins a stale value in front of the clinician as current. Refusing it
    leaves NULL, which the ranked read already handles."""
    assert _parse_datetime("2126-03-12") is None
    assert _parse_date("2126-03-12") is None


def test_a_date_a_few_hours_ahead_of_utc_is_still_accepted():
    """Reports are printed in IST (UTC+5:30), so a document dated today in the clinic is
    legitimately tomorrow's date against a UTC clock for part of every day. Rejecting those
    would undate the reports most likely to matter — the ones filed the day they were drawn."""
    tomorrow = (datetime.now(UTC) + timedelta(hours=6)).date()
    assert _parse_datetime(tomorrow.isoformat()) is not None


def test_a_date_before_clinical_records_existed_is_refused():
    assert _parse_datetime("1799-01-01") is None
    assert _parse_date(date(1799, 1, 1)) is None


def test_a_plausible_date_still_parses():
    assert _parse_datetime("12/03/2026") == datetime(2026, 3, 12, tzinfo=UTC)


# --- Day-first, except when it isn't -----------------------------------------------------


def test_an_iso_date_is_not_re_read_day_first():
    """``dayfirst=True`` is an instruction to dateutil, not a hint, and it applies even when
    the string leads with an unambiguous four-digit year: "2026-03-12" came back as 3 December.

    Silent, and now on the hot path — both extraction paths emit ISO (the deterministic parser
    via ``isoformat()``, the vision path because the model is asked for YYYY-MM-DD), so every
    sample date either of them produced between March and December of a year would have landed
    on the wrong day, in the column the longitudinal ordering sorts by.
    """
    assert _parse_datetime("2026-03-12") == datetime(2026, 3, 12, tzinfo=UTC)
    assert _parse_date("2026-03-12") == date(2026, 3, 12)


def test_an_ambiguous_date_is_still_read_day_first():
    """The Indian convention, which is what the reports being scanned are printed in."""
    assert _parse_date("03/12/2025") == date(2025, 12, 3)


def test_the_two_paths_agree_on_the_same_printed_date():
    """The deterministic parser hands ``sample_date`` on as an ISO string and the merge reads
    it back. A disagreement between them is a wrong day in the record with nothing to notice
    it — the round trip is the only place it would show."""
    (lab,) = _labs("Sample Collected on: 12/03/2026\nLABS:\nPotassium: 7.4 mmol/L (3.5-5.1)\n")
    assert _parse_datetime(lab["sample_date"]) == datetime(2026, 3, 12, tzinfo=UTC)
