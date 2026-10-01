"""A very small PDF writer, for the one thing this API has to produce a PDF of.

Why this exists rather than a dependency
----------------------------------------
The record export needed a second format: FHIR JSON is what another *system* reads, and a
clinician handing a patient their record, or filing it with a referral, needs something a human
opens. That is a page of laid-out text and nothing more — no images, no vector graphics, no
tables with rules, no forms.

The libraries that do this well (ReportLab, WeasyPrint) are large, and the one thing they would
add over these two hundred lines is the part this document does not use. That trade would be
worth making anyway if it bought correct Unicode, but it does not: their default is the same
base-14 Type 1 fonts with the same single-byte encoding used here, and getting Devanagari out of
either means registering and subsetting a TrueType font, which is a different piece of work in
any of them. So the dependency would buy layout features this page has no use for, at the cost
of another package on the untrusted-input review list in ``requirements.txt``.

This module only ever *writes*. It parses nothing, and nothing a clinician or a document can
supply reaches it except as text that is escaped and measured. That is what makes hand-rolling
it a defensible choice here and a bad one for reading PDFs, which is why the read side uses
pypdf and is pinned to within an inch of its life.

What it cannot do, stated plainly
---------------------------------
The base-14 fonts are encoded WinAnsi — a single-byte set that covers Latin-1 and no more. A
patient whose name is written in Devanagari, Bengali or Tamil cannot be rendered by it. Rather
than silently emitting the substitution character, :meth:`PdfBuilder.render` reports whether any
character was replaced, and the export puts a line on the page saying so and pointing at the
JSON export, which is lossless. A record that quietly misspells the patient's name is worse than
one that says it could not print it.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

# A4 in PostScript points, and the margins the page is laid out inside.
PAGE_WIDTH = 595.28
PAGE_HEIGHT = 841.89
MARGIN_X = 56.0
MARGIN_TOP = 56.0
MARGIN_BOTTOM = 56.0

BODY_SIZE = 9.5
BODY_LEADING = 13.0

_REGULAR = "F1"
_BOLD = "F2"

# Adobe's published widths for the two base-14 faces used here, in 1/1000 em, for the printable
# ASCII range. Wrapping is done by measuring against these rather than by counting characters:
# "MMMM" and "iiii" differ by a factor of nearly four in Helvetica, so a character count wraps a
# line of drug names either half a page short or off the right edge.
#
# Anything outside the range measures as ``_DEFAULT_WIDTH``. Every such character is either a
# Latin-1 accented letter (which really is about this wide) or one this font cannot draw at all,
# and those are reported rather than laid out precisely.
_DEFAULT_WIDTH = 556

# fmt: off
_HELVETICA_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)
_HELVETICA_BOLD_WIDTHS = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
)
# fmt: on


def _widths(bold: bool) -> tuple[int, ...]:
    return _HELVETICA_BOLD_WIDTHS if bold else _HELVETICA_WIDTHS


def text_width(text: str, size: float, *, bold: bool = False) -> float:
    """How wide ``text`` will be when drawn, in points."""
    table = _widths(bold)
    total = 0
    for char in text:
        index = ord(char) - 32
        total += table[index] if 0 <= index < len(table) else _DEFAULT_WIDTH
    return total * size / 1000.0


def wrap(text: str, size: float, max_width: float, *, bold: bool = False) -> list[str]:
    """``text`` broken into lines that fit ``max_width``.

    Breaks on spaces, and falls back to breaking inside a word only when a single word does not
    fit on a line of its own — a 60-character storage hash or a run-together drug name would
    otherwise be drawn straight off the edge of the page and silently truncated by the reader.
    """
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        current = ""
        for word in paragraph.split(" "):
            candidate = f"{current} {word}".strip()
            if current and text_width(candidate, size, bold=bold) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
            while text_width(current, size, bold=bold) > max_width and len(current) > 1:
                cut = len(current) - 1
                while cut > 1 and text_width(current[:cut], size, bold=bold) > max_width:
                    cut -= 1
                lines.append(current[:cut])
                current = current[cut:]
        lines.append(current)
    return lines


def _escape(text: str) -> tuple[bytes, bool]:
    """``text`` as a PDF literal string's bytes, and whether anything was lost encoding it.

    WinAnsi is Latin-1-shaped, so a Devanagari name encodes to nothing meaningful. The caller
    needs to know that happened — see this module's docstring — so the loss is returned rather
    than swallowed by ``errors="replace"`` alone.
    """
    encoded = text.encode("cp1252", errors="replace")
    lossy = encoded.decode("cp1252") != text
    out = bytearray()
    for byte in encoded:
        if byte in (0x28, 0x29, 0x5C):  # ( ) \
            out += b"\\"
        out.append(byte)
    return bytes(out), lossy


@dataclass
class _Line:
    """One drawn line of text, already positioned within its page."""

    text: str
    size: float
    bold: bool
    indent: float
    y: float


@dataclass
class PdfBuilder:
    """Accumulates lines, flows them onto pages, and renders the file.

    The caller appends in reading order and never thinks about pagination: a heading that would
    land at the foot of a page pulls itself onto the next one, so a section title is never the
    last thing on a sheet with its content overleaf.
    """

    title: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _pages: list[list[_Line]] = field(default_factory=lambda: [[]])
    _y: float = field(default=PAGE_HEIGHT - MARGIN_TOP)

    @property
    def _usable_width(self) -> float:
        return PAGE_WIDTH - 2 * MARGIN_X

    def _advance(self, height: float) -> None:
        if self._y - height < MARGIN_BOTTOM:
            self._pages.append([])
            self._y = PAGE_HEIGHT - MARGIN_TOP
        self._y -= height

    def _draw(self, text: str, *, size: float, bold: bool, indent: float, leading: float) -> None:
        self._advance(leading)
        # The baseline is fixed here, not recomputed at render time. ``space()`` advances the
        # cursor without appending a line, so anything that re-derived positions from the line
        # list alone would drift by exactly the blank space between sections.
        self._pages[-1].append(_Line(text=text, size=size, bold=bold, indent=indent, y=self._y))

    def text(
        self,
        body: str,
        *,
        size: float = BODY_SIZE,
        bold: bool = False,
        indent: float = 0.0,
        leading: float = BODY_LEADING,
    ) -> None:
        """One run of text, wrapped to the page width and flowed across pages."""
        for line in wrap(body, size, self._usable_width - indent, bold=bold):
            self._draw(line, size=size, bold=bold, indent=indent, leading=leading)

    def heading(self, body: str, *, size: float = 13.0) -> None:
        """A section title, kept with at least one line of what follows it."""
        needed = size + BODY_LEADING * 2
        if self._y - needed < MARGIN_BOTTOM:
            self._pages.append([])
            self._y = PAGE_HEIGHT - MARGIN_TOP
        self.space(6)
        self.text(body, size=size, bold=True, leading=size + 3)
        self.space(2)

    def field(self, label: str, value: str, *, indent: float = 0.0) -> None:
        """A ``label: value`` row, wrapped so a long value indents under itself."""
        self.text(f"{label}: {value}", indent=indent)

    def space(self, height: float = BODY_LEADING) -> None:
        self._advance(height)

    def _content(self, lines: list[_Line], page_number: int, page_count: int) -> tuple[bytes, bool]:
        lossy = False
        out = bytearray()
        for line in lines:
            body, line_lossy = _escape(line.text)
            lossy = lossy or line_lossy
            font = _BOLD if line.bold else _REGULAR
            out += (
                f"BT /{font} {line.size:.2f} Tf "
                f"{MARGIN_X + line.indent:.2f} {line.y:.2f} Td ".encode("ascii")
            )
            out += b"(" + body + b") Tj ET\n"
        footer, footer_lossy = _escape(f"Page {page_number} of {page_count}")
        lossy = lossy or footer_lossy
        out += f"BT /{_REGULAR} 8.00 Tf {MARGIN_X:.2f} {MARGIN_BOTTOM - 22:.2f} Td ".encode("ascii")
        out += b"(" + footer + b") Tj ET\n"
        return bytes(out), lossy

    def render(self) -> tuple[bytes, bool]:
        """The finished PDF, and whether any character could not be represented in WinAnsi."""
        lossy = False
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog_id = add(b"")  # 1 — patched below, once the page tree id is known
        pages_id = add(b"")  # 2
        regular_id = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
        )
        bold_id = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>"
        )
        resources = f"<< /Font << /{_REGULAR} {regular_id} 0 R /{_BOLD} {bold_id} 0 R >> >>".encode(
            "ascii"
        )

        page_ids: list[int] = []
        total = len(self._pages)
        for number, lines in enumerate(self._pages, start=1):
            content, page_lossy = self._content(lines, number, total)
            lossy = lossy or page_lossy
            # Flate-compressed: an export of a long chart is mostly repeated field labels, and
            # a clinical record leaving the building should not be larger than it needs to be.
            compressed = zlib.compress(content, 9)
            stream_id = add(
                b"<< /Length "
                + str(len(compressed)).encode("ascii")
                + b" /Filter /FlateDecode >>\nstream\n"
                + compressed
                + b"\nendstream"
            )
            page_ids.append(
                add(
                    f"<< /Type /Page /Parent {pages_id} 0 R "
                    f"/MediaBox [0 0 {PAGE_WIDTH:.2f} {PAGE_HEIGHT:.2f}] ".encode("ascii")
                    + b"/Resources "
                    + resources
                    + f" /Contents {stream_id} 0 R >>".encode("ascii")
                )
            )

        kids = " ".join(f"{pid} 0 R" for pid in page_ids)
        objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode(
            "ascii"
        )
        objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii")

        title, title_lossy = _escape(self.title)
        lossy = lossy or title_lossy
        stamp = self.created_at.astimezone(UTC).strftime("D:%Y%m%d%H%M%SZ")
        info_id = add(
            b"<< /Title (" + title + b") /Producer (DoAide Med) "
            b"/CreationDate (" + stamp.encode("ascii") + b") >>"
        )

        out = bytearray(b"%PDF-1.4\n")
        offsets: list[int] = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

        xref_at = len(out)
        out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode("ascii")
        out += (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R "
            f"/Info {info_id} 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode("ascii")
        )
        return bytes(out), lossy
