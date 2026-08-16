"""Reading a spreadsheet of patients into validated demographics. Pure: no I/O, no clock, no DB.

A practice arriving with this product already has a patient list, in a spreadsheet, and the only
way to get it in was to type each chart into the create form. This module is the reading half of
the alternative: bytes in, one decided outcome per row out. Writing them, checking them against
the charts already on the account and recording it is
:mod:`app.services.patient_import_service`.

Two things here are load-bearing and neither is obvious from the shape of the code.

**Consent is a column, and a row without it is refused.** Under the DPDP Act consent is the
lawful basis for holding any of this, and ``PatientService.create`` already refuses to create a
chart without it. A bulk path that defaulted the flag to true — or accepted a blank as "yes" —
would be a way to load a thousand charts nobody consented to through the one door that was built
to make that impossible. So ``consent_given`` is a *required* column, an unrecognised value is a
row error rather than a false, and the refusal is per row: one unmarked patient does not stop the
other four hundred and ninety-nine loading.

**A date that could be read two ways is not read at all.** ``03/04/1990`` is 3 April in the
spreadsheet a clinic in Pune exports and 4 March in one exported from a US-locale Excel, and
nothing in the file says which. Getting it wrong is not a cosmetic error: date of birth is what
``app.core.dose_range`` reads to decide whether a dose is judged against a paediatric mg/kg
ceiling or an adult one, and what eGFR is computed from. Guessing day-first because this product
ships in India would be right most of the time, and the times it was wrong would be silent.

So the convention is *inferred from the whole file and only from proof*: a row whose first
component exceeds twelve can only be day-first, one whose second component exceeds twelve can
only be month-first, and one file is one export from one locale. Rows that prove nothing are read
under whatever the file proved elsewhere. A file that proves both ways is contradictory and is
refused whole; a file that proves neither has its slash-dated rows refused individually, with a
message asking for ISO. Unambiguous ``YYYY-MM-DD`` is always read and never needs any of this.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

# The header names this reader understands, in the spelling the response documents. Matching is
# case-insensitive and treats spaces, hyphens and underscores as the same character, so "Full
# Name", "full-name" and "FULL_NAME" are one column — a header row typed by a person is not a
# programming identifier and refusing it for its capitalisation helps nobody.
REQUIRED_COLUMNS: tuple[str, ...] = ("full_name", "consent_given")
OPTIONAL_COLUMNS: tuple[str, ...] = (
    "date_of_birth",
    "sex",
    "phone",
    "address_text",
    "notes",
    "weight_kg",
)
KNOWN_COLUMNS: frozenset[str] = frozenset(REQUIRED_COLUMNS + OPTIONAL_COLUMNS)

# Most rows one file may carry is configuration (``settings.patient_import_max_rows``) and is
# applied by the service. This is the ceiling on the *bytes* the reader will decode, and it is
# here rather than there because it bounds the work done before any row exists to count.
MAX_FILE_BYTES = 2 * 1024 * 1024

# Candidate delimiters, in the order a tie is broken. Comma first because it is the common case;
# semicolon because that is what Excel writes in every locale where the comma is the decimal
# separator; tab because "Unicode text" and a paste out of a spreadsheet both produce it.
_DELIMITERS: tuple[str, ...] = (",", ";", "\t")

# Tried in order. ``utf-8-sig`` covers plain UTF-8 and the BOM Excel's "CSV UTF-8" writes;
# ``utf-16`` covers Excel's "Unicode text"; ``cp1252`` is the last resort for a legacy Windows
# export, and it is a resort rather than a refusal because a file that will not decode is a
# 400 the operator cannot act on, while a mis-decoded name is visible in the response beside
# the encoding that produced it.
_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-16", "cp1252")

DateConvention = Literal["iso", "day_first", "month_first"]

_ISO_DATE = re.compile(r"\A(\d{4})-(\d{1,2})-(\d{1,2})\Z")
_SEPARATED_DATE = re.compile(r"\A(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\Z")

_TRUE_TOKENS = frozenset({"true", "t", "yes", "y", "1", "given", "granted"})
_FALSE_TOKENS = frozenset({"false", "f", "no", "n", "0", "withheld", "not given"})

_HEADER_PUNCTUATION = re.compile(r"[\s_\-]+")


class ImportFileError(Exception):
    """The file cannot be read as a whole. Distinct from a row that fails validation.

    A row error is data the operator can fix in one line; this is a file that does not describe
    what it claims to, and reporting it per row would produce four hundred identical messages
    instead of one useful one.
    """


def normalise_header(name: str) -> str:
    """A header cell reduced to its canonical column name.

    ``"Date Of Birth"`` and ``"date-of-birth"`` both become ``date_of_birth``.
    """
    return _HEADER_PUNCTUATION.sub("_", name.strip().casefold()).strip("_")


def normalise_name(value: str) -> str:
    """A patient name reduced to what two spellings of the same person share.

    Case and runs of whitespace only. Deliberately not clever: it does not reorder
    ``"Sharma, Rajesh"`` into ``"Rajesh Sharma"``, transliterate, or strip honorifics, because
    every one of those transformations also merges two people who are not the same person, and
    the cost of a wrong merge here is a chart with someone else's allergies on it. The comparison
    exists to catch the ordinary case — the same list uploaded twice — and says so.
    """
    return " ".join(value.split()).casefold()


def decode(raw: bytes) -> tuple[str, str]:
    """The file's text and the encoding that produced it. Raises :class:`ImportFileError`."""
    if len(raw) > MAX_FILE_BYTES:
        raise ImportFileError(
            f"This file is {len(raw) // 1024} KB. The importer reads up to "
            f"{MAX_FILE_BYTES // 1024} KB — split the list and upload it in parts."
        )
    if not raw.strip():
        raise ImportFileError("This file is empty.")
    for encoding in _ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        # A UTF-16 file decoded as cp1252 succeeds and yields NUL between every character, so
        # "it decoded" is not on its own evidence that the encoding was right. NUL cannot appear
        # in a CSV a spreadsheet wrote, and Postgres cannot store it, so it is the tell.
        if "\x00" in text:
            continue
        return text, encoding
    raise ImportFileError(
        "This file is not readable as text. Save it from your spreadsheet as CSV (UTF-8) and "
        "upload it again."
    )


def sniff_delimiter(header_line: str) -> str:
    """Whichever candidate separator appears most often in the header row.

    The header rather than the whole file, and outside quotes is not considered: a header row is
    a handful of short column names, so the count is unambiguous there in a way it is not in a
    body row where an address legitimately contains commas.
    """
    counts = {d: header_line.count(d) for d in _DELIMITERS}
    best = max(_DELIMITERS, key=lambda d: counts[d])
    return best if counts[best] else ","


@dataclass(frozen=True)
class ParsedRow:
    """One body row, keyed by canonical column name, with the line number a spreadsheet shows.

    ``row_number`` counts the header as row 1, so it is the number in the row gutter of the file
    the operator has open. Reporting a zero-based index into the body would be correct and
    useless.
    """

    row_number: int
    values: dict[str, str]

    def get(self, column: str) -> str:
        return self.values.get(column, "").strip()


@dataclass(frozen=True)
class ParsedFile:
    encoding: str
    delimiter: str
    columns: tuple[str, ...]
    # Header cells that are not columns this reader knows. Reported rather than refused: a
    # practice's own spreadsheet carries its own columns (a local MRN, a ward, a referring GP),
    # and refusing the file for them would make the importer usable only by files written for it.
    unknown_columns: tuple[str, ...]
    rows: list[ParsedRow] = field(default_factory=list)


def parse_csv(raw: bytes) -> ParsedFile:
    """Bytes to rows. Raises :class:`ImportFileError` for anything wrong with the file itself."""
    text, encoding = decode(raw)
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = sniff_delimiter(first_line)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        raw_header = next(reader)
    except StopIteration:  # pragma: no cover - `decode` already refused an empty file
        raise ImportFileError("This file is empty.") from None

    columns = tuple(normalise_header(cell) for cell in raw_header)
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise ImportFileError(
            "This file is missing the column"
            f"{'s' if len(missing) > 1 else ''} {', '.join(missing)}. The first row must name "
            f"the columns; required are {', '.join(REQUIRED_COLUMNS)}, and optional are "
            f"{', '.join(OPTIONAL_COLUMNS)}."
        )
    duplicated = sorted({c for c in columns if c in KNOWN_COLUMNS and columns.count(c) > 1})
    if duplicated:
        # Two columns of the same name mean two different values for one field, and `zip` below
        # would silently keep the last. Which one the operator meant is not knowable from here.
        raise ImportFileError(
            f"This file names the column{'s' if len(duplicated) > 1 else ''} "
            f"{', '.join(duplicated)} more than once. Remove the duplicate and upload it again."
        )

    rows: list[ParsedRow] = []
    for offset, cells in enumerate(reader):
        row_number = offset + 2  # header is row 1
        if not any(cell.strip() for cell in cells):
            # A trailing blank line, or the blank row spreadsheets leave between blocks. Silently
            # skipped rather than reported as an invalid row: it carries no intent.
            continue
        # Not `strict=`: a short row (trailing columns omitted, which spreadsheets do) keeps
        # the cells it has, and a long one drops the surplus. Both are files a person exported,
        # and refusing them for a ragged edge would be refusing the ordinary case.
        rows.append(
            ParsedRow(row_number=row_number, values=dict(zip(columns, cells, strict=False)))
        )
    if not rows:
        raise ImportFileError("This file has a header row but no patients under it.")
    return ParsedFile(
        encoding=encoding,
        delimiter=delimiter,
        columns=columns,
        unknown_columns=tuple(sorted({c for c in columns if c and c not in KNOWN_COLUMNS})),
        rows=rows,
    )


def parse_consent(value: str) -> bool | None:
    """``True``/``False`` for a recognised token, ``None`` for anything else — including blank.

    ``None`` is not "no". The caller refuses the row either way, but the two produce different
    messages, and "we could not read your consent column" is the one that gets a file fixed.
    """
    token = " ".join(value.split()).casefold()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return None


def _date_parts(value: str) -> tuple[int, int, int] | None:
    """``(first, second, year)`` for a slash/dot/dash date, or ``None`` if it is not one."""
    match = _SEPARATED_DATE.match(value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def date_convention(values: list[str]) -> DateConvention | None:
    """What order the file's separated dates are in, from the file's own evidence.

    ``None`` means nothing in the file proves it — every separated date has both components at
    twelve or below — and those rows are then refused individually. ISO-only files also return
    ``None``, and correctly: they need no convention.

    Raises :class:`ImportFileError` when the file proves *both*, which is not a locale but a
    file assembled from two sources, and no single reading of it is right.
    """
    day_first_proof: str | None = None
    month_first_proof: str | None = None
    for value in values:
        parts = _date_parts(value.strip())
        if parts is None:
            continue
        first, second, _year = parts
        if first > 12 and second <= 12:
            day_first_proof = day_first_proof or value
        elif second > 12 and first <= 12:
            month_first_proof = month_first_proof or value
    if day_first_proof and month_first_proof:
        raise ImportFileError(
            f"This file mixes date orders — {day_first_proof!r} can only be day/month and "
            f"{month_first_proof!r} can only be month/day, so neither reading fits the whole "
            "file. Re-save the dates as YYYY-MM-DD and upload it again."
        )
    if day_first_proof:
        return "day_first"
    if month_first_proof:
        return "month_first"
    return None


class AmbiguousDateError(ValueError):
    """A separated date in a file that proved no convention. Refused rather than guessed at."""


def parse_date(value: str, convention: DateConvention | None) -> date | None:
    """One cell to a date. Blank is ``None``; anything unreadable raises ``ValueError``."""
    text = value.strip()
    if not text:
        return None
    iso = _ISO_DATE.match(text)
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    parts = _date_parts(text)
    if parts is None:
        raise ValueError(
            f"{text!r} is not a date this importer reads. Use YYYY-MM-DD (for example 1974-03-09)."
        )
    first, second, year = parts
    if convention is None:
        raise AmbiguousDateError(
            f"{text!r} could be day/month or month/day and nothing else in this file settles "
            "which. Re-save the dates as YYYY-MM-DD (for example 1974-03-09)."
        )
    day, month = (first, second) if convention == "day_first" else (second, first)
    return date(year, month, day)


def parse_decimal(value: str) -> float | None:
    """A number, or ``None`` for blank. Raises ``ValueError`` on anything else.

    Accepts a comma decimal separator, because a spreadsheet in a locale that writes ``72,5``
    also writes its CSV with semicolons — the same file the delimiter sniffing above exists for.
    """
    text = value.strip()
    if not text:
        return None
    return float(text.replace(",", ".") if text.count(",") == 1 and "." not in text else text)
