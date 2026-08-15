"""The PDF writer, tested as a file format rather than as a string.

``app.services.pdf`` is two hundred lines of byte-level format assembly standing in for a
dependency, and the argument for hand-rolling it only holds if the output is actually a PDF —
not "a PDF that our own code can read", which would be circular, but one that a library written
by someone who has never seen this repository can open. So every structural assertion here goes
through pypdf.

The rest is the two things a text layout engine can get wrong quietly: wrapping (a line drawn
past the right edge is not truncated by the reader, it is simply invisible) and escaping (an
unbalanced parenthesis in a drug name terminates the string operator and corrupts the page from
there down — and parentheses are ordinary in the brand names this system handles).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pypdf
import pytest

from app.services.pdf import (
    MARGIN_X,
    PAGE_WIDTH,
    PdfBuilder,
    text_width,
    wrap,
)

_USABLE = PAGE_WIDTH - 2 * MARGIN_X


def _read(data: bytes) -> pypdf.PdfReader:
    return pypdf.PdfReader(io.BytesIO(data))


def _text(data: bytes) -> str:
    return "\n".join(page.extract_text() for page in _read(data).pages)


def _builder(**kwargs) -> PdfBuilder:
    return PdfBuilder(title="Test", created_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC), **kwargs)


# --- It is a PDF ------------------------------------------------------------------------------


def test_a_rendered_document_opens_in_a_library_that_knows_nothing_about_us():
    pdf = _builder()
    pdf.text("Hello")

    data, lossy = pdf.render()

    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")
    reader = _read(data)
    assert len(reader.pages) == 1
    assert "Hello" in _text(data)
    assert lossy is False


def test_the_cross_reference_offsets_point_at_the_objects_they_claim_to():
    """The xref table is the one part of the format with no redundancy: an offset out by a byte
    makes the file unopenable, and nothing in the writing of it would notice."""
    pdf = _builder()
    pdf.text("Body")
    data, _ = pdf.render()

    start = data.rindex(b"startxref")
    xref_at = int(data[start + len(b"startxref") :].split()[0])
    assert data[xref_at : xref_at + 4] == b"xref"

    # [0] "xref", [1] the subsection header, [2] the mandatory free entry for object 0.
    lines = data[xref_at:].split(b"\n")[3:]
    for index, line in enumerate(lines, start=1):
        if not line.strip() or line.startswith(b"trailer"):
            break
        offset = int(line.split()[0])
        assert data[offset:].startswith(f"{index} 0 obj".encode("ascii")), index


def test_the_title_reaches_the_document_metadata():
    pdf = PdfBuilder(title="Patient record — Asha Menon")
    pdf.text("x")
    data, _ = pdf.render()

    metadata = _read(data).metadata
    assert metadata is not None
    assert "Asha Menon" in str(metadata.title)


# --- Wrapping ---------------------------------------------------------------------------------


def test_a_line_too_long_for_the_page_is_broken_rather_than_drawn_off_the_edge():
    """A reader does not truncate an overlong line — it draws it into the margin and off the
    sheet, so the text is simply gone with nothing to show it ever existed."""
    long_line = "Metformin hydrochloride 500 mg twice daily after food " * 6

    lines = wrap(long_line, 9.5, _USABLE)

    assert len(lines) > 1
    for line in lines:
        assert text_width(line, 9.5) <= _USABLE


def test_wrapping_measures_the_glyphs_rather_than_counting_characters():
    """ "MMMM" and "iiii" differ by nearly a factor of four in Helvetica. A character count wraps
    one of them half a page short and lets the other run off the edge."""
    assert text_width("MMMM", 10) > text_width("iiii", 10) * 3


def test_a_single_word_longer_than_the_line_is_broken_inside_itself():
    """A 64-character storage hash or a run-together extracted name has no space to break at."""
    lines = wrap("A" * 400, 9.5, _USABLE)

    assert len(lines) > 1
    for line in lines:
        assert text_width(line, 9.5) <= _USABLE


def test_bold_text_is_measured_with_the_bold_widths():
    """Helvetica-Bold is wider than Helvetica at every size. Measuring bold text against the
    regular table wraps a heading a word too late, which puts it into the margin."""
    assert text_width("Medications", 13, bold=True) > text_width("Medications", 13)


# --- Escaping ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Glycomet GP (metformin + glimepiride)",
        "Dose 500mg \\ twice daily",
        "Unbalanced ( paren",
        "Unbalanced ) paren",
    ],
)
def test_parentheses_and_backslashes_survive_into_the_extracted_text(value):
    """These are the characters that delimit a PDF literal string. An unescaped one in a drug
    name ends the string operator early and corrupts every glyph after it — and parentheses are
    ordinary in the Indian brand names this system reads off prescriptions."""
    pdf = _builder()
    pdf.text(value)

    data, _ = pdf.render()

    assert value in _text(data)


def test_a_character_outside_the_font_is_reported_rather_than_silently_replaced():
    """The base-14 fonts cover Latin-1, and this product is deployed in India. A record that
    quietly misspells the patient's name is worse than one that admits it could not print it."""
    pdf = _builder()
    pdf.text("नमस्ते")

    _, lossy = pdf.render()

    assert lossy is True


def test_latin_1_punctuation_is_not_treated_as_a_loss():
    """The em dash and the rupee-adjacent Latin-1 range are inside WinAnsi. Reporting them as
    dropped would put the "could not print" warning on every export this system produces, and a
    warning that is always on is a warning nobody reads."""
    pdf = _builder()
    pdf.text("Aether Clinician — patient record; café; ±5%")

    _, lossy = pdf.render()

    assert lossy is False


# --- Pagination -------------------------------------------------------------------------------


def test_content_flows_onto_new_pages_rather_than_off_the_bottom_of_the_first():
    pdf = _builder()
    for index in range(300):
        pdf.text(f"Row {index}")

    data, _ = pdf.render()

    reader = _read(data)
    assert len(reader.pages) > 1
    text = _text(data)
    assert "Row 0" in text
    assert "Row 299" in text


def test_every_page_is_numbered_out_of_the_true_total():
    """A record handed over in print has to be checkable for completeness by whoever receives
    it. "Page 2" alone does not say whether page 3 was lost."""
    pdf = _builder()
    for index in range(300):
        pdf.text(f"Row {index}")

    data, _ = pdf.render()

    reader = _read(data)
    total = len(reader.pages)
    for number, page in enumerate(reader.pages, start=1):
        assert f"Page {number} of {total}" in page.extract_text()


def test_a_heading_is_never_the_last_thing_on_a_page():
    """A section title stranded at the foot of a sheet, with its content overleaf, reads as an
    empty section — and "Allergies" followed by nothing is the worst possible way to be wrong."""
    pdf = _builder()
    # Fill to just short of a page break, then start a section.
    for index in range(53):
        pdf.text(f"Filler {index}")
    pdf.heading("Allergies")
    pdf.text("Penicillin")

    data, _ = pdf.render()

    for page in _read(data).pages:
        page_text = page.extract_text()
        if "Allergies" in page_text:
            assert "Penicillin" in page_text
            break
    else:  # pragma: no cover - the heading has to be somewhere
        pytest.fail("the heading was not rendered at all")


def test_a_blank_run_does_not_shift_the_lines_after_it_out_of_position():
    """Vertical position is fixed when a line is appended, not recomputed at render time from
    the line list — which would silently ignore every ``space()`` and draw the whole page
    creeping upward by the total blank space above it."""
    pdf = _builder()
    pdf.text("First")
    pdf.space(200)
    pdf.text("Second")

    data, _ = pdf.render()

    # Read through the Flate filter rather than off the raw bytes: content streams are
    # compressed, which is also why this is asserted on the decoded stream and not on the file.
    contents = _read(data).pages[0].get_contents()
    assert contents is not None
    body = contents.get_data().decode("latin-1")
    first_y = float(body.split("(First)")[0].rsplit("Td", 1)[0].split()[-1])
    second_y = float(body.split("(Second)")[0].rsplit("Td", 1)[0].split()[-1])
    assert first_y - second_y >= 200
