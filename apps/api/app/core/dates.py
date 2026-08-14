"""Reading dates off clinical documents, and refusing the ones that cannot be real.

Two subtleties live here rather than in each caller, because getting either wrong is silent:
the value still parses, it is just a different day than the one printed on the report.

**Day-first, except when it isn't.** Indian labs and prescriptions print DD/MM/YYYY, so
``12/03/2026`` is 12 March — read the other way round it becomes 3 December, a *later* date,
which is the direction that does damage: a stale result outranks a current one on the
``sample_date`` ordering the panic-value screen evaluates. But ``dayfirst=True`` is not a hint
to dateutil, it is an instruction, and it applies even when the string leads with an
unambiguous four-digit year: ``dateutil.parse("2026-03-12", dayfirst=True)`` returns 3
December. That matters now that both extraction paths emit ISO — the deterministic parser via
``datetime.isoformat()`` and the vision path because the model is asked for ``YYYY-MM-DD``.
So the flag is applied only to strings that are actually ambiguous.

**Plausibility.** Clinical dates arrive from OCR of a scan, not only from a typed form, and
OCR misreads digits — a "2026" that comes back "2126". ``sample_date`` is the primary sort key
for "the patient's most recent value per marker", so one future-dated row outranks every real
result for that marker and pins a stale value in front of the clinician as current. Refusing
the date leaves the column NULL, which the ranked read already handles (NULLS LAST, then
most-recently-recorded) and which is the same state as a report whose date was never legible.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

# "2026-03-12" and "2026/03/12": the year is unambiguous, so day-first must not be forced.
_LEADING_YEAR_RE = re.compile(r"^\s*\d{4}[-/.]")

_EARLIEST_PLAUSIBLE_CLINICAL_DATE = date(1900, 1, 1)
# One day, not zero: reports are printed in IST (UTC+5:30), so a document dated "today" in the
# clinic is legitimately tomorrow's date against a UTC clock for five and a half hours a day.
# Undating the reports filed the day they were drawn would be the worst possible trade.
_FUTURE_DATE_TOLERANCE = timedelta(days=1)


def parse_clinical_date(text: str) -> datetime | None:
    """Parse a date (with optional time) as printed on an Indian clinical document.

    Returns ``None`` for anything unparseable. Does *not* range-check the result — callers
    persisting it should pass it through ``is_plausible_clinical_date`` as well.
    """
    try:
        from dateutil import parser as dtparser

        return dtparser.parse(text, dayfirst=not _LEADING_YEAR_RE.match(text))
    except (ValueError, OverflowError, TypeError):
        return None


def is_plausible_clinical_date(value: date) -> bool:
    """True if this date could belong to a real clinical event. See the module docstring."""
    latest = datetime.now(UTC).date() + _FUTURE_DATE_TOLERANCE
    return _EARLIEST_PLAUSIBLE_CLINICAL_DATE <= value <= latest
