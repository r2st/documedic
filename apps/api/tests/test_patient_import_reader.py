"""The import reader on its own — no app, no database, no event loop.

Split out of ``test_patient_import`` because these are the level the ambiguity rules are actually
decided at. A parser is bytes in and rows out, and asserting that through an HTTP round trip and
a transaction says less about it while taking longer to say it.
"""

from __future__ import annotations

import pytest

from app.core import patient_import as reader


def test_it_reads_a_plain_file():
    parsed = reader.parse_csv(b"full_name,consent_given\nRamesh Kumar,yes\n")

    assert parsed.delimiter == ","
    assert parsed.rows[0].get("full_name") == "Ramesh Kumar"
    # The header is row 1, so a report an operator can navigate from starts at 2.
    assert parsed.rows[0].row_number == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [("yes", True), ("NO", False), ("", None), ("perhaps", None), ("1", True), ("0", False)],
)
def test_consent_tokens(value, expected):
    """``None`` is not "no". The caller refuses the row either way, but the two produce different
    messages, and "we could not read your consent column" is the one that gets a file fixed."""
    assert reader.parse_consent(value) is expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [("Full Name", "full_name"), ("date-of-birth", "date_of_birth"), ("  SEX  ", "sex")],
)
def test_headers_are_matched_however_a_person_typed_them(header, expected):
    assert reader.normalise_header(header) == expected


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        (["25/12/1970"], "day_first"),
        (["12/25/1970"], "month_first"),
        # Nothing over twelve anywhere: the file proves nothing, and those rows are refused
        # individually rather than read under a guess.
        (["03/04/1990", "05/06/1991"], None),
        # ISO needs no convention and contributes no evidence.
        (["1970-12-25"], None),
        ([""], None),
    ],
)
def test_the_convention_comes_only_from_proof(dates, expected):
    assert reader.date_convention(dates) == expected


def test_a_file_proving_both_orders_is_a_file_level_refusal():
    with pytest.raises(reader.ImportFileError) as exc:
        reader.date_convention(["25/12/1970", "12/25/1970"])
    assert "mixes date orders" in str(exc.value)


def test_an_ambiguous_date_raises_rather_than_picking_a_side():
    with pytest.raises(reader.AmbiguousDateError):
        reader.parse_date("03/04/1990", None)


@pytest.mark.parametrize(
    ("value", "convention", "iso"),
    [
        ("1990-04-03", None, "1990-04-03"),
        ("03/04/1990", "day_first", "1990-04-03"),
        ("03/04/1990", "month_first", "1990-03-04"),
        ("03.04.1990", "day_first", "1990-04-03"),
        ("03-04-1990", "day_first", "1990-04-03"),
    ],
)
def test_dates_are_read_under_the_settled_convention(value, convention, iso):
    assert reader.parse_date(value, convention).isoformat() == iso


def test_a_name_is_normalised_by_case_and_spacing_and_nothing_more():
    """Deliberately not clever. Reordering ``"Sharma, Rajesh"`` or stripping honorifics would
    also merge two people who are not the same person, and the cost of a wrong merge is a chart
    carrying someone else's allergies."""
    assert reader.normalise_name("  ramesh   KUMAR ") == reader.normalise_name("Ramesh Kumar")
    assert reader.normalise_name("Sharma, Rajesh") != reader.normalise_name("Rajesh Sharma")


@pytest.mark.parametrize(("value", "expected"), [("72.5", 72.5), ("72,5", 72.5), ("", None)])
def test_a_comma_decimal_separator_is_read(value, expected):
    """The locale that writes ``72,5`` also writes its CSV with semicolons — the same file the
    delimiter sniffing exists for."""
    assert reader.parse_decimal(value) == expected


def test_a_file_that_will_not_decode_at_all_says_what_to_do():
    # Lone UTF-16 surrogates: not valid in any of the three encodings tried.
    with pytest.raises(reader.ImportFileError) as exc:
        reader.decode(b"\xff\xfe\x00\xd8\x00\x00")
    assert "CSV (UTF-8)" in str(exc.value)


def test_the_byte_ceiling_is_reported_in_units_an_operator_reads():
    with pytest.raises(reader.ImportFileError) as exc:
        reader.decode(b"x" * (reader.MAX_FILE_BYTES + 1))
    assert "KB" in str(exc.value)


def test_a_ragged_row_keeps_what_it_has():
    """Spreadsheets omit trailing empty columns. Refusing a file for a ragged edge would be
    refusing the ordinary case."""
    parsed = reader.parse_csv(b"full_name,date_of_birth,consent_given\nRamesh Kumar,yes\n")

    assert parsed.rows[0].get("full_name") == "Ramesh Kumar"
    assert parsed.rows[0].get("consent_given") == ""
