"""Bulk patient import response shapes.

There is no request schema: the request is a file, not a JSON body. See
``app.routers.patients.import_patients``.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.core.patient_import import OPTIONAL_COLUMNS, REQUIRED_COLUMNS


class ImportRowResult(BaseModel):
    """What happened to one line of the uploaded file."""

    # The line number a spreadsheet shows in its own gutter: the header is row 1, so this is the
    # number the operator can navigate to directly.
    row_number: int
    status: Literal["created", "duplicate", "possible_duplicate", "invalid", "not_created"]
    # Echoed back so a report is readable without opening the file beside it. Present even for a
    # refused row, which is the row most likely to be looked up.
    full_name: str | None = None
    patient_id: uuid.UUID | None = None
    # The chart this row was found to be a duplicate of, so the operator can open it.
    existing_patient_id: uuid.UUID | None = None
    # Why, for anything that was not created. Written for the person who will fix the file.
    message: str | None = None


class PatientImportResponse(BaseModel):
    """The whole outcome of one upload.

    ``created + skipped + invalid == total_rows`` always holds, and a dry run reports every
    would-be creation under ``not_created`` with ``created`` at zero — so the two runs are
    directly comparable and an operator can see what changed between them.
    """

    dry_run: bool
    total_rows: int
    created: int
    # Rows that named a patient already on the account. Never created; see
    # ``PatientImportService`` for why there is no override.
    skipped: int
    invalid: int
    # How the file was read, so a mangled name or a misread date is diagnosable from the
    # response rather than by experiment.
    encoding: str
    delimiter: str
    # Which way the file's `03/04/1990`-style dates were read, inferred from the file's own
    # unambiguous rows. Null when the file carried no such dates (ISO only, or none at all) —
    # in which case any ambiguous row was refused rather than guessed at.
    date_convention: Literal["iso", "day_first", "month_first"] | None = None
    # Header cells this importer does not use. Accepted, not refused: a practice's spreadsheet
    # carries its own columns. Reported so a misspelt `date_of_birth` is visible as the reason
    # every date came out empty.
    unknown_columns: list[str] = Field(default_factory=list)
    rows: list[ImportRowResult] = Field(default_factory=list)


# Documented on the route so the columns are discoverable from the API docs rather than from
# this module. Built from the reader's own tuples so the two cannot drift.
COLUMN_HELP = (
    f"Required columns: {', '.join(REQUIRED_COLUMNS)}. "
    f"Optional: {', '.join(OPTIONAL_COLUMNS)}. "
    "Header matching ignores case and treats spaces, hyphens and underscores alike."
)
