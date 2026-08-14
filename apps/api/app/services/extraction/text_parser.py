"""Deterministic heuristic parser for medical-document text.

Used for the OCR/text-mode path and as the offline/no-API-key fallback. Parses a simple,
common structure found in prescriptions and lab reports into typed entities with
heuristic per-field confidence scores. Real-world robustness comes from the Claude vision
path; this parser keeps the pipeline fully functional and testable without an LLM.

Supported line grammars (case-insensitive section headers):
  MEDICATIONS / RX:
    - <brand or generic> <dose><unit> <frequency>      e.g. "Glycomet 500mg BD"
  LABS / INVESTIGATIONS:
    - <marker>: <value> <unit> (<low>-<high>)          e.g. "HbA1c: 9.2 % (4.0-5.6)"
  CONDITIONS / DIAGNOSIS:
    - <condition name>
  ALLERGIES:
    - <allergen> [- reaction]

Printed reports also carry a masthead of labelled administrative lines — the collection date,
the patient's age, the lab's accession number. Those share the ``<label>: <number>`` shape of a
lab line, so they are recognised and handled before the entity grammars run: the date lines
become the ``sample_date`` carried by every lab on the report, and the rest are dropped rather
than ingested as markers. See ``_metadata_date`` and ``_NON_MARKER_LABELS``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.core.dates import parse_clinical_date

_SECTION_ALIASES = {
    "medication": "medications",
    "medications": "medications",
    "rx": "medications",
    "drugs": "medications",
    "prescription": "medications",
    "lab": "labs",
    "labs": "labs",
    "investigation": "labs",
    "investigations": "labs",
    "results": "labs",
    # Headers that actually appear on Indian pathology-lab printouts.
    "lab report": "labs",
    "laboratory report": "labs",
    "test report": "labs",
    "biochemistry": "labs",
    "haematology": "labs",
    "hematology": "labs",
    "pathology report": "labs",
    "condition": "conditions",
    "conditions": "conditions",
    "diagnosis": "conditions",
    "diagnoses": "conditions",
    "problems": "conditions",
    "allergy": "allergies",
    "allergies": "allergies",
}

_FREQ_TOKENS = (
    "od",
    "bd",
    "tds",
    "qid",
    "hs",
    "sos",
    "stat",
    "qd",
    "bid",
    "tid",
    "once daily",
    "twice daily",
    "thrice daily",
    "at night",
    "morning",
)

_MED_RE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9\-\+\.\/ ]*?)\s+"
    r"(?P<dose>\d+(?:\.\d+)?)\s*(?P<unit>mg|mcg|g|ml|iu|units?|%)?\b"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)

_LAB_RE = re.compile(
    r"^(?P<marker>[A-Za-z][A-Za-z0-9\-\.\/\(\) ]*?)\s*[:=]\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z%/µμ\^0-9]+)?\s*"
    r"(?:\(?\s*(?P<low>\d+(?:\.\d+)?)\s*[-–]\s*(?P<high>\d+(?:\.\d+)?)\s*\)?)?\s*$"
)

# Whitespace-column layout, which is what most printed lab reports actually use:
#   "HbA1c            8.4 %      (4.0 - 5.6)"
# There is no ':' to key off, and a bare "<name> <number> <unit>" line is indistinguishable
# from a prescription line — so a parenthesised reference range is required as the
# discriminator. Prescriptions do not carry one (a dose schedule like "(1-0-1)" has three
# parts and fails this pattern), which keeps medications from being read as labs.
_LAB_COLUMNAR_RE = re.compile(
    r"^(?P<marker>[A-Za-z][A-Za-z0-9\-\.\/\+ ]*?)\s{1,}"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>%|[A-Za-z][A-Za-z/µμ\^0-9\.]*)?\s*"
    r"\(\s*(?P<low>\d+(?:\.\d+)?)\s*[-–]\s*(?P<high>\d+(?:\.\d+)?)\s*\)\s*$"
)


# --- Labelled masthead lines ------------------------------------------------------------
#
# Everything below exists because a printed lab report is not only lab lines. Above the
# results sits a block of labelled administrative fields, and several of them are a word
# followed by a colon followed by a number -- which is exactly the lab grammar. "Sample Date:
# 12/03/2026" parsed as a marker named "Sample Date" with a value of 12, "Age: 54 years" as a
# marker named "Age" with a value of 54. Both were then offered to the clinician for approval
# alongside the real results, and one hurried approval put them in the longitudinal record.
#
# The date lines are worse than noise, because the report's own date is the single piece of
# information the record most needs from it and had no other source: nothing in the extraction
# layer emitted ``sample_date``, so every lab ingested through a document landed undated and
# "the patient's most recent potassium" -- the question the panic-value screen exists to answer
# -- fell back to ingestion order. A chart where an old report is filed after a new one then
# screens the stale value as current. Reading the date off the report and attaching it to the
# report's labs closes that, and turns the line that was corrupting the record into the field
# that fixes it.

# Label -> (rank, confidence). Rank orders the *kinds* of date a report prints: when a report
# carries several, the one closest to when the blood was actually drawn wins, because that is
# what ``sample_date`` means and what the longitudinal ordering is asking about. A report date
# can trail collection by days on a send-out panel. Confidence tracks the same thing, and is
# what decides whether the clinician is asked to confirm the field before it is merged.
_COLLECTED, _RECEIVED, _REPORTED, _BARE = 0, 1, 2, 3
_DATE_LABELS: dict[str, tuple[int, float]] = {
    "sample collected on": (_COLLECTED, 0.9),
    "sample collected": (_COLLECTED, 0.9),
    "sample collection date": (_COLLECTED, 0.9),
    "specimen collected on": (_COLLECTED, 0.9),
    "date of collection": (_COLLECTED, 0.9),
    "collection date": (_COLLECTED, 0.9),
    "collected on": (_COLLECTED, 0.9),
    "collected": (_COLLECTED, 0.9),
    "sample date": (_COLLECTED, 0.9),
    "specimen date": (_COLLECTED, 0.9),
    "drawn on": (_COLLECTED, 0.9),
    "received on": (_RECEIVED, 0.75),
    "date of receipt": (_RECEIVED, 0.75),
    "registered on": (_RECEIVED, 0.75),
    "registration date": (_RECEIVED, 0.75),
    "date of registration": (_RECEIVED, 0.75),
    "report date": (_REPORTED, 0.6),
    "reported on": (_REPORTED, 0.6),
    "date of report": (_REPORTED, 0.6),
    "reporting date": (_REPORTED, 0.6),
    "date": (_BARE, 0.55),
    "dated": (_BARE, 0.55),
}

# Labels that are never a clinical marker or a drug. Matched on the whole normalised label, so
# "Date of Birth" does not reach the "date" entry above and "Sodium" reaches none of them.
_NON_MARKER_LABELS = frozenset(
    {
        "name",
        "patient name",
        "patient",
        "patients name",
        "pt name",
        "age",
        "sex",
        "gender",
        "age sex",
        "sex age",
        "age gender",
        "dob",
        "date of birth",
        "birth date",
        "uhid",
        "mrn",
        "patient id",
        "hospital no",
        "ip no",
        "op no",
        "opd no",
        "ipd no",
        "reg no",
        "registration no",
        "visit id",
        "episode id",
        "encounter id",
        "lab no",
        "lab id",
        "sid",
        "sid no",
        "accession no",
        "accession number",
        "barcode",
        "sample id",
        "sample no",
        "specimen no",
        "order id",
        "order no",
        "bill no",
        "invoice no",
        "receipt no",
        "amount",
        "ref by",
        "referred by",
        "referring doctor",
        "ref doctor",
        "referrer",
        "doctor",
        "consultant",
        "physician",
        "pathologist",
        "verified by",
        "approved by",
        "mobile",
        "phone",
        "contact",
        "contact no",
        "email",
        "address",
        "pin",
        "pincode",
        "page",
        "printed on",
        "printed",
        "print date",
        "generated on",
        "department",
        "centre",
        "center",
        "branch",
        "client",
        "client code",
        "sample type",
        "specimen type",
        "specimen",
        "method",
        "instrument",
        "status",
        "report status",
    }
)

# The value half of a date line must *look* like a date before it is parsed. dateutil is happy
# to read "12" as the 12th of the current month, which would invent a sample date out of a
# line this module failed to understand -- the exact failure mode being fixed here.
_DATE_VALUE_RE = re.compile(
    r"\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4}"  # 12/03/2026, 2026-03-12, 12.03.26
    r"|\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{2,4}"  # 12-Mar-2026, 12 March 2026
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}"  # March 12, 2026
)
_TIME_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?")

_LABELLED_RE = re.compile(r"^(?P<label>[^:=]{1,60})[:=]\s*(?P<value>.*)$")

# Whether a line carries a number at all — the cheapest test for "this could have been a
# medication or a lab result". See ``parse_document`` on why the loss count needs it.
_DIGIT_RE = re.compile(r"\d")

# How many leading words of a colon-less line may form a label. "Sample Collected on" is three.
_MAX_LABEL_WORDS = 4


def _normalise_label(raw: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace: "Ref. By" -> "ref by"."""
    return " ".join(re.sub(r"[^a-z]+", " ", raw.lower()).split())


def _label_candidates(line: str) -> list[tuple[str, str]]:
    """Every (normalised label, remainder) split this line could plausibly be.

    A colon or equals sign is definitive, so it yields the single true split. Without one --
    the whitespace-column layout that ``_LAB_COLUMNAR_RE`` exists for prints "Sample Collected
    on    12/03/2026" the same way it prints "HbA1c    8.4 % (4.0 - 5.6)" -- the leading words
    are offered as candidate labels and the caller decides whether any is one it knows.
    """
    m = _LABELLED_RE.match(line)
    if m:
        return [(_normalise_label(m.group("label")), m.group("value").strip())]
    words = line.split()
    return [
        (_normalise_label(" ".join(words[:n])), " ".join(words[n:]))
        for n in range(1, min(_MAX_LABEL_WORDS, len(words)) + 1)
    ]


def _parse_date_value(value: str) -> datetime | None:
    """Read a date (and optional time-of-day) out of the value half of a labelled line.

    Only the date-shaped span is handed to the date reader, never the whole remainder: a lab's
    footer prints "Sample Date: -" and dateutil is willing to read almost anything, including a
    bare number as a day of the current month. That would invent a sample date out of a line
    this module failed to understand, which is the failure being fixed rather than a fix for it.
    """
    found = _DATE_VALUE_RE.search(value)
    if not found:
        return None
    text = found.group(0)
    tail = _TIME_RE.match(value[found.end() :].strip())
    if tail:
        text = f"{text} {tail.group(0)}"
    return parse_clinical_date(text)


def _metadata_date(line: str) -> tuple[int, float, datetime] | None:
    """(rank, confidence, value) if this line is a report date line, else ``None``."""
    for label, remainder in _label_candidates(line):
        ranked = _DATE_LABELS.get(label)
        if ranked is None:
            continue
        parsed = _parse_date_value(remainder)
        if parsed is not None:
            return ranked[0], ranked[1], parsed
    return None


def _is_non_marker_line(line: str) -> bool:
    """True for a masthead line whose label can never name a marker, drug, or diagnosis.

    A colon makes the label unambiguous. Without one the leading words are only a guess, and
    guessing wrong here *deletes* clinical data -- "Status epilepticus" under DIAGNOSIS begins
    with a denylisted label. So the colon-less form additionally requires a numeric value,
    which is what the administrative lines this drops all have ("Age 54", "Lab No 4471") and
    what a diagnosis, allergen, or drug name never begins with.
    """
    explicit = _LABELLED_RE.match(line)
    if explicit:
        return _normalise_label(explicit.group("label")) in _NON_MARKER_LABELS
    return any(
        label in _NON_MARKER_LABELS and remainder[:1].isdigit()
        for label, remainder in _label_candidates(line)
    )


@dataclass
class ParsedField:
    name: str
    value: object
    confidence: float


@dataclass
class ParsedEntity:
    entity_type: str
    fields: list[ParsedField] = field(default_factory=list)


def _detect_section(line: str) -> str | None:
    key = re.sub(r"[^a-z ]", "", line.strip().lower()).strip().rstrip(":").strip()
    return _SECTION_ALIASES.get(key)


# A dose and its unit left *behind* in the tail of a medication line -- "5mg" in ",5mg OD", "000mg"
# in " 000mg BD". Finding one means the number this parser captured is not the whole dose; see
# ``_dose_is_truncated``.
_STRANDED_DOSE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|iu|units?|%)\b", re.IGNORECASE)


def _dose_is_truncated(unit: str, rest: str) -> bool:
    """Whether the dose grammar stopped early and left the real dose in the remainder.

    ``_MED_RE`` reads a dose as an unbroken run of digits with an optional decimal point, and any
    character that is not one ends it. A scan that puts something else in the middle of the number
    therefore does not fail -- it *succeeds on the prefix*. The rest of the number stays in
    ``rest``, which nothing but the frequency scan ever looks at, and the fragment is emitted at
    the same 0.86 confidence a cleanly-read dose gets. 0.86 is above
    ``confirmation_confidence_threshold``, so the field is banded "high" and the clinician is never
    asked about it.

    The failures are not exotic. A comma decimal separator is how much of the world writes 2.5, and
    a scanner that reads the decimal point of "0.25" as a comma yields "Digoxin 0,25mg OD" ->
    a dose of **0**. A space that creeps into a thousands position yields "Metformin 1 000mg BD" ->
    a dose of **1**. Both are silent, both are off by orders of magnitude, and both describe a real
    drug the patient is really taking, so nothing downstream has cause to doubt them.

    The tell is specific enough to act on: the unit did not attach to the captured number, and a
    number *with* a unit is sitting in the remainder. That is the dose the page actually carries.
    It deliberately does not fire on an Indian dosing schedule -- "Crocin 500 1-0-1" also leaves
    digits in the remainder, but they carry no unit and the 500 is a complete reading.
    """
    return not unit and bool(_STRANDED_DOSE_RE.search(rest))


def _parse_medication(line: str) -> ParsedEntity | None:
    m = _MED_RE.match(line.strip())
    if not m:
        return None
    name = m.group("name").strip()
    if not name or name.lower() in _SECTION_ALIASES:
        return None
    dose = m.group("dose")
    unit = (m.group("unit") or "").strip()
    rest = (m.group("rest") or "").strip()
    frequency = ""
    lowered = rest.lower()
    for tok in _FREQ_TOKENS:
        if re.search(rf"\b{re.escape(tok)}\b", lowered):
            frequency = tok.upper() if len(tok) <= 4 else tok
            break
    # Banded "low" rather than dropped: the drug, the frequency and the line itself are still worth
    # showing, and what the clinician needs is to be *asked* about the number. Low is what routes
    # the field into the review queue -- see ``DocumentService._band``, where anything short of
    # "high" sets ``needs_confirmation`` and holds the document at ``needs_confirmation``.
    truncated = _dose_is_truncated(unit, rest)
    return ParsedEntity(
        entity_type="medication",
        fields=[
            ParsedField("brand_name_raw", name, 0.88),
            ParsedField("dose", dose, 0.3 if truncated else 0.86),
            ParsedField("dose_unit", unit or None, 0.80 if unit else (0.3 if truncated else 0.4)),
            ParsedField("frequency", frequency or None, 0.7 if frequency else 0.45),
            ParsedField("event_type", "continue", 0.6),
        ],
    )


def _finite(token: str) -> float | None:
    """The token as a float, or ``None`` when it is too large for one to represent.

    The value grammars match ``\\d+(?:\\.\\d+)?`` with no length bound, which is right -- a bound
    would silently truncate a reading -- but ``float()`` of a long enough digit run is ``inf``,
    not an error. A smudged decimal point or a scanner that repeats a digit down a column is all
    it takes, and ~309 digits is the threshold.

    ``inf`` is not merely a wrong number here, it is one that cannot leave the process. Extracted
    entities are returned to the clinician for approval and stored in the document's JSON meta
    column *before* anything numeric-aware sees them, and infinity has no JSON representation:
    the approval response raised ``ValueError: Out of range float values are not JSON compliant``
    and 500'd the upload, and ``json.dumps`` wrote the non-standard token ``Infinity`` into meta,
    which PostgreSQL's jsonb and any strict reader both reject. The database layer already
    refuses non-finite values (``graph_service._to_decimal``), but that guard sits downstream of
    the serialisation that fails.
    """
    try:
        value = float(token)
    except (TypeError, ValueError):  # pragma: no cover — the grammars only match digits
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _parse_lab(line: str) -> ParsedEntity | None:
    m = _LAB_RE.match(line.strip()) or _LAB_COLUMNAR_RE.match(line.strip())
    if not m:
        return None
    marker = m.group("marker").strip()
    if not marker or marker.lower() in _SECTION_ALIASES:
        return None
    # An unrepresentable value keeps the digits the document actually printed rather than being
    # dropped. ``GraphService._merge_lab`` files a value it cannot store as a number into
    # ``value_text``, which makes the row qualitative -- the correct handling for a reading
    # nobody can interpret -- and the clinician still sees what was on the page and can correct
    # it. Dropping the field would show them a marker with no value at all.
    value = m.group("value")
    numeric = _finite(value)
    fields = [
        ParsedField("marker_name", marker, 0.9),
        ParsedField("value_numeric", numeric if numeric is not None else value, 0.85),
        ParsedField("unit", (m.group("unit") or None), 0.75 if m.group("unit") else 0.4),
    ]
    low = _finite(m.group("low")) if m.group("low") else None
    high = _finite(m.group("high")) if m.group("high") else None
    # Both bounds or neither: a range with one unrepresentable end is not a narrower range, and
    # ``_merge_lab`` reads a lone bound as a real one when it screens the value as abnormal.
    if low is not None and high is not None:
        fields.append(ParsedField("reference_range_low", low, 0.8))
        fields.append(ParsedField("reference_range_high", high, 0.8))
    return ParsedEntity(entity_type="lab_result", fields=fields)


def _parse_condition(line: str) -> ParsedEntity | None:
    name = line.strip().lstrip("-•* ").strip()
    if not name or name.lower() in _SECTION_ALIASES:
        return None
    return ParsedEntity(
        entity_type="condition",
        fields=[
            ParsedField("condition_name", name, 0.78),
            ParsedField("status", "active", 0.6),
        ],
    )


def _parse_allergy(line: str) -> ParsedEntity | None:
    raw = line.strip().lstrip("-•* ").strip()
    if not raw or raw.lower() in _SECTION_ALIASES:
        return None
    allergen, _, reaction = raw.partition("-")
    return ParsedEntity(
        entity_type="allergy",
        fields=[
            ParsedField("allergen_name", allergen.strip(), 0.85),
            ParsedField("allergen_type", "drug", 0.6),
            ParsedField(
                "reaction_description",
                reaction.strip() or None,
                0.7 if reaction.strip() else 0.4,
            ),
        ],
    )


_PARSERS = {
    "medications": _parse_medication,
    "labs": _parse_lab,
    "conditions": _parse_condition,
    "allergies": _parse_allergy,
}


@dataclass
class ParsedDocument:
    """What the walk produced, and what it had to give up on.

    ``unreadable`` is the count of lines *inside a recognised clinical section* that no grammar
    could read. It exists because the entity list alone cannot express a loss: a prescription
    whose warfarin line was scanned as "Warfarin 5rng OD" yields three medications where the page
    printed four, every one of them cleanly read and confidently banded, and nothing anywhere says
    a fourth line was ever there. The clinician reviews three drugs, approves them, and the chart
    is missing the most interaction-heavy drug on the page.

    A count and not the text. ``extraction_metadata``, where this ends up, is not an encrypted
    column — the same reason ``DocumentService._mark_extraction_failed`` records an exception's
    type and not its message, and the reason the upload audit payload carries no file name. An
    unreadable line is still the patient's prescribing. The count is what the clinician needs to
    know to go back to the original, and the original is a download away.
    """

    entities: list[ParsedEntity]
    unreadable: int


def parse_document(text: str) -> ParsedDocument:
    """Parse free text into typed entities by walking section headers.

    Labelled masthead lines are consumed before the entity grammars see them: administrative
    ones are dropped, and the best report date found anywhere in the document is attached to
    every lab result as ``sample_date`` (see ``_apply_sample_date``). The date is applied after
    the whole document is walked because the masthead can sit either side of the results block.

    Only lines under a section header are counted as unreadable, and only ones containing a digit.
    Both restrictions exist to keep the count meaningful rather than merely present:

    * An un-sectioned line that parses to nothing is ordinary page furniture — a letterhead, an
      address, a footer.
    * ``section`` persists until the *next* header, so the prescriber's name and signature at the
      foot of a prescription are still "under MEDICATIONS". Counting those would put a nonzero
      loss on nearly every real document, and a review flag that is always on is one clinicians
      learn to click past — the automation bias this system is built to resist. The medication and
      lab grammars both require a number, so a line without one was never a candidate for either;
      a line *with* one that the grammar still rejected is the case worth raising.

    (The condition and allergy grammars accept any non-empty line, so they never reach this at
    all.)
    """
    entities: list[ParsedEntity] = []
    unreadable = 0
    section: str | None = None
    best_date: tuple[int, float, datetime] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        detected = _detect_section(line)
        if detected:
            section = detected
            continue
        dated = _metadata_date(line)
        if dated is not None:
            # Lower rank wins; ties keep the first seen, which is the topmost on the page.
            if best_date is None or dated[0] < best_date[0]:
                best_date = dated
            continue
        if _is_non_marker_line(line):
            continue
        if section is None:
            # Heuristic: try lab then medication on un-sectioned lines.
            ent = _parse_lab(line) or _parse_medication(line)
            if ent:
                entities.append(ent)
            continue
        parser = _PARSERS.get(section)
        if parser is None:
            continue
        ent = parser(line)
        if ent:
            entities.append(ent)
        elif _DIGIT_RE.search(line):
            unreadable += 1
    if best_date is not None:
        _apply_sample_date(entities, best_date[2], best_date[1])
    return ParsedDocument(entities=entities, unreadable=unreadable)


def parse_text(text: str) -> list[ParsedEntity]:
    """The entities alone. See :func:`parse_document` for what the walk could not read."""
    return parse_document(text).entities


def _apply_sample_date(entities: list[ParsedEntity], value: datetime, confidence: float) -> None:
    """Stamp the report's date onto every lab result that did not carry one of its own.

    Emitted as an ordinary field rather than plumbed through a separate channel, so it inherits
    everything the other fields already get: it is shown to the clinician for confirmation with
    its own confidence band, it is correctable through the same ``corrections`` payload, and
    ``GraphService._merge_lab`` already reads ``fields["sample_date"]``. A report date carries
    lower confidence than a collection date precisely so the clinician is asked about it.
    """
    iso = value.isoformat()
    for entity in entities:
        if entity.entity_type != "lab_result":
            continue
        if any(f.name == "sample_date" for f in entity.fields):
            continue
        entity.fields.append(ParsedField("sample_date", iso, confidence))
