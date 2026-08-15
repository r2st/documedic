"""One-sided reference ranges, and the lab rows they were silently costing the chart.

The bug
-------
A reference interval is printed one of two ways, and the parser matched one of them. The
two-ended form ("0.4 - 4.0") was matched; the one-sided form was not::

    Cholesterol, Total     245 mg/dL     (< 200)
    Triglycerides          310 mg/dL     (<150)
    ALT (SGPT)              68 U/L       (Up to 40)
    HDL Cholesterol          32 mg/dL    (> 40)

That is not an edge case. It is how a printed panel states every marker whose normal range has
one clinically meaningful side, which is most of a lipid profile and half a liver function test.

And what it cost was not the range. Both lab grammars anchor on ``$``, so a trailing "(< 200)"
the pattern could not account for failed the **whole** match — and a line that does not match is
not a lab result at all. The cholesterol of 245, the ALT of 68 and the triglyceride of 310 were
absent from the chart entirely, and the clinician approving the report saw a shorter list with
nothing on it to say rows were missing.

It reaches past the display, because the deterministic engine reads the chart and not the
document: ``app.core.hepatic`` cannot score a liver it has no ALT for, and a marker printed this
way is invisible to the critical-value guard for the same reason. Both then report themselves
*unevaluated* — the honest answer to a question nobody could ask, on a report that was carrying
the number.

Two adjacent spellings were losing rows the same way and are covered here too: a comma in the
analyte name ("Cholesterol, Total", "Bilirubin, Direct") and a unit that does not start with a
letter ("10^3/µL", "/cumm" — the canonical platelet and CBC units, both already registered in
``app.core.lab_safety``, neither of which the columnar grammar would match).

What must not change
--------------------
The parenthesised range is what tells a lab line from a prescription line in a columnar layout.
So only *numeric* one-sided forms are admitted — a comparison operator or "up to" followed by a
number, never arbitrary text — and the second half of this file is the prescription corpus,
asserting that widening the range grammar did not start reading dose schedules as lab results.
"""

from __future__ import annotations

import pytest

from app.services.extraction.text_parser import parse_text


def _labs(body: str) -> list[dict]:
    return [
        {f.name: f.value for f in e.fields}
        for e in parse_text(f"LABS:\n{body}\n")
        if e.entity_type == "lab_result"
    ]


def _one(body: str) -> dict:
    rows = _labs(body)
    assert len(rows) == 1, f"expected exactly one lab row, got {rows}"
    return rows[0]


# --- the rows that were being lost --------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Cholesterol, Total     245 mg/dL     (< 200)",
        "Cholesterol: 245 mg/dL (< 200)",
        "Triglycerides         310 mg/dL     (<150)",
        "ALT (SGPT)              68 U/L       (Up to 40)",
        "ALT: 68 U/L (upto 40)",
        "HDL Cholesterol          32 mg/dL    (> 40)",
    ],
)
def test_a_one_sided_reference_range_no_longer_costs_the_whole_result(line):
    """The finding. Before this, every one of these lines produced no lab row at all."""
    assert _labs(line), f"the result was dropped along with the range it could not parse: {line}"


def test_an_upper_bound_is_read_as_an_upper_bound():
    row = _one("Cholesterol, Total     245 mg/dL     (< 200)")
    assert row["reference_range_high"] == 200.0
    # And nothing is invented for the other end. "< 200" says where normal stops; it says
    # nothing about how low a cholesterol may be, and a fabricated low bound would chart a
    # genuinely low result as normal.
    assert "reference_range_low" not in row


def test_a_lower_bound_is_read_as_a_lower_bound():
    """The direction matters more than it looks: an HDL of 32 against "> 40" is low, and read
    as an upper bound it is charted as normal."""
    row = _one("HDL Cholesterol          32 mg/dL    (> 40)")
    assert row["reference_range_low"] == 40.0
    assert "reference_range_high" not in row


@pytest.mark.parametrize(
    ("printed", "expected_high"),
    [("< 200", 200.0), ("<200", 200.0), ("<= 200", 200.0), ("≤200", 200.0), ("Up to 200", 200.0)],
)
def test_every_way_a_report_writes_an_upper_bound(printed, expected_high):
    row = _one(f"Cholesterol: 245 mg/dL ({printed})")
    assert row["reference_range_high"] == expected_high


def test_a_two_ended_range_is_unchanged():
    """The form that already worked has to keep working, bounds and all."""
    row = _one("TSH                     6.2 uIU/mL   (0.4 - 4.0)")
    assert (row["reference_range_low"], row["reference_range_high"]) == (0.4, 4.0)


# --- the printed range, kept as printed ---------------------------------------------------


def test_the_range_is_kept_as_the_document_printed_it():
    """``reference_range_text`` had no writer at all until this change: the record export read
    it and every row answered None, so a chart printed for a patient showed a blank where the
    report had a range."""
    assert _one("Cholesterol: 245 mg/dL (< 200)")["reference_range_text"] == "< 200"
    assert _one("TSH: 6.2 uIU/mL (0.4 - 4.0)")["reference_range_text"] == "0.4 - 4.0"


def test_the_printed_text_is_what_separates_a_one_sided_range_from_no_range():
    """On the row itself, "high = 200, low = None" and "the report printed no range" are the
    same shape. The printed text is the only thing that tells them apart."""
    one_sided = _one("Cholesterol: 245 mg/dL (< 200)")
    none_at_all = _one("Potassium: 5.4 mmol/L")

    assert "reference_range_low" not in one_sided
    assert "reference_range_low" not in none_at_all
    assert one_sided["reference_range_text"] == "< 200"
    assert "reference_range_text" not in none_at_all


# --- the adjacent spellings that were losing rows the same way ----------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Cholesterol, Total     245 mg/dL     (< 200)",
        "Bilirubin, Total: 18.2 mg/dL (0.2 - 1.2)",
        "Protein, Total         5.1 g/dL      (6.4 - 8.3)",
    ],
)
def test_a_qualifier_after_a_comma_does_not_cost_the_row(line):
    """A printed panel qualifies an analyte after it, not before: "Cholesterol, Total". Without
    the comma the line did not match, on exactly the markers whose qualifier decides which
    analyte they are."""
    assert _labs(line), f"an analyte name with a comma was dropped: {line}"


@pytest.mark.parametrize(
    ("line", "unit"),
    [
        ("Platelet Count (PLT)   8 10^3/uL     (150 - 410)", "10^3/uL"),
        ("WBC                  12500 /cumm     (4000 - 11000)", "/cumm"),
    ],
)
def test_a_unit_that_does_not_start_with_a_letter_does_not_cost_the_row(line, unit):
    """Both units are registered in ``app.core.lab_safety`` and neither line would parse, so a
    platelet count of 8 — a panic low — was dropped before any guard could see it."""
    assert _one(line)["unit"] == unit


def test_a_synonym_in_brackets_does_not_cost_the_row():
    """A bracketed synonym is how an analyser prints a name: "ALT (SGPT)", "Potassium (K+)", and
    ``lab_safety._marker_candidates`` already resolves exactly those spellings — it was never
    getting the chance."""
    assert _one("ALT (SGPT)              68 U/L       (Up to 40)")["marker_name"] == "ALT (SGPT)"


# --- what must not have changed -----------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Tab Crocin 500 (SOS)",
        "Glycomet 500mg (1-0-1)",
        "Amlodipine 5 mg (1-0-0)",
        "Telmisartan 40 mg (1-0-1)",
        "Insulin Glargine 18 units (0-0-1)",
        "Metformin 500 (1-0-1)",
        "Aspirin 75 (0-1-0)",
        "Atorvastatin 20 mg (0-0-1)",
    ],
)
def test_a_prescription_line_is_still_not_a_lab_result(line):
    """The safety property the widening had to preserve.

    In a columnar layout the parenthesised range is the *only* thing separating a lab line from
    a prescription line, so admitting arbitrary text after the value would start charting "(SOS)"
    and "(1-0-1)" as reference ranges — a medication in the record as a lab result, with a
    number the safety engine would then read.
    """
    assert not _labs(line), f"a prescription line was read as a lab result: {line}"


def test_a_dose_schedule_is_not_a_reference_range_even_in_a_lab_section():
    """The same claim where it is hardest: the line sits under a LABS heading, so nothing but
    the grammar itself is keeping it out."""
    assert not _labs("Glycomet 500mg (1-0-1)")


# --- through to the chart -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_one_sided_range_reaches_the_chart_and_screens_the_value(auth_client):
    """End to end: upload, approve, and read the row back off the longitudinal record.

    The bound the report printed one-sidedly is what decides ``is_abnormal``, which is what
    ``agents.tools.summarize_snapshot`` selects on to build the summary every agent reasons from
    and what the FHIR export writes as the observation's interpretation.
    """
    from tests.conftest import create_patient

    patient = await create_patient(auth_client)
    report = (
        b"%PDF-1.4\nLABS:\n"
        b"Cholesterol, Total     245 mg/dL     (< 200)\n"
        b"HDL Cholesterol          32 mg/dL    (> 40)\n"
    )
    upload = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("lipids.pdf", report, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]

    extraction = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{doc_id}/extraction"
    )
    assert extraction.status_code == 200, extraction.text
    entities = extraction.json()["entities"]
    approve = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{doc_id}/approve",
        json={"approved_indices": list(range(len(entities)))},
    )
    assert approve.status_code == 200, approve.text

    record = await auth_client.get(f"/api/v1/patients/{patient['id']}/record")
    labs = {row["marker_name"]: row for row in record.json()["lab_results"]}

    assert "Cholesterol, Total" in labs, (
        "the result never reached the chart, which is the whole finding — the range it could "
        "not parse took the row with it"
    )
    total = labs["Cholesterol, Total"]
    assert total["reference_range_high"] == "200.000000"
    assert total["reference_range_text"] == "< 200"
    # 245 against an upper bound of 200 is abnormal, and abnormal *high*.
    assert total["is_abnormal"] is True
    assert total["abnormality_direction"] == "high"

    # And the other direction, from a lower bound the report printed alone.
    hdl = labs["HDL Cholesterol"]
    assert hdl["is_abnormal"] is True
    assert hdl["abnormality_direction"] == "low"
