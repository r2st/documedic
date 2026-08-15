"""Control-character normalisation for clinician-supplied text, in one place.

These rules already existed, correctly reasoned and well tested — in ``app.schemas.patient``,
applied to ``full_name``, ``phone``, ``address_text`` and ``notes`` and to nothing else. Every
*other* free-text field a clinician can write took the string verbatim: the presenting complaint
that opens a reasoning run, the answers to the Triage agent's questions, the reason recorded
against a clinical decision, the documented justification for overriding a hard block, and the
corrected drug names and doses submitted from the extraction review screen.

That split is not defensible on the merits. The two reasons the patient module gives for
cleaning are properties of the *column*, not of the field:

* **U+0000 cannot be stored in a PostgreSQL text column at all.** asyncpg raises, the flush
  fails, and the whole request rolls back — which for extraction approval means the clinician's
  entire reviewed document is discarded, and for a hard-block override means the one audited
  path past an allergy block dies with a 500. SQLite accepts NUL silently, which is why a suite
  of 3,700 tests never saw it.
* **CR/LF turn one field into two** in any line-oriented export of a chart, and the C1 range
  carries terminal escapes that are interpreted by whatever renders a log or a CSV.

So the helpers move here and every clinician-writable text field uses them. Two flavours, the
same distinction the patient module drew:

``clean_identifier``  — single-line values (a name, a drug, a dose). Control characters are
                        removed, whitespace runs collapse to one space, ends are trimmed.
``clean_free_text``   — multi-line prose (notes, a complaint, a justification). Newlines and
                        tabs are content and survive; everything else in C0/C1 goes.

Both *clean* rather than reject. A control character in pasted text is not a clinical error and
not something a clinician can be usefully asked to hunt for; refusing the write over one would
be the worse outcome. What callers still reject is a value that is *empty once cleaned*, which
is not a recoverable typo — see the per-schema validators.
"""

from __future__ import annotations

import re

# The C0/C1 control range excluding \t \n \v \f \r, which Python's ``\s`` already matches and
# which _WHITESPACE_RUN therefore folds into a single space. Splitting the range this way is
# what keeps "Ramesh\tKumar" two words: deleting the whole control range would join them into
# one unsearchable token, the very defect this normalisation exists to prevent.
_NON_SPACING_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x9f]")

# For multi-line free text, where tab and newline are content rather than separators.
_CONTROL_CHARS_KEEPING_LINES = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

_WHITESPACE_RUN = re.compile(r"\s+")


def clean_identifier(value: str) -> str:
    """Normalise a single-line identifying value: no control characters, no stray whitespace.

    Whitespace runs collapse because these values are matched against, not just displayed:
    patient search is a substring match over the decrypted name, and a corrected drug name is
    resolved through the DrugVocabulary. A value stored as ``"Ramesh  Kumar"`` is invisible to
    a search for ``"Ramesh Kumar"``, and ``"Amoxi cillin "`` resolves to nothing.
    """
    return _WHITESPACE_RUN.sub(" ", _NON_SPACING_CONTROL_CHARS.sub("", value)).strip()


def clean_free_text(value: str) -> str:
    """Strip control characters from multi-line clinical prose, keeping its line structure.

    Nothing is rejected and nothing is trimmed beyond the control characters. Clinical free
    text is the clinician's own record and is not the API's to tidy.
    """
    # CRLF first, so Windows-pasted text keeps its line breaks rather than losing each break
    # along with the CR.
    return _CONTROL_CHARS_KEEPING_LINES.sub("", value.replace("\r\n", "\n"))
