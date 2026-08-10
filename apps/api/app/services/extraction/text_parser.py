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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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
    return ParsedEntity(
        entity_type="medication",
        fields=[
            ParsedField("brand_name_raw", name, 0.88),
            ParsedField("dose", dose, 0.86),
            ParsedField("dose_unit", unit or None, 0.80 if unit else 0.4),
            ParsedField("frequency", frequency or None, 0.7 if frequency else 0.45),
            ParsedField("event_type", "continue", 0.6),
        ],
    )


def _parse_lab(line: str) -> ParsedEntity | None:
    m = _LAB_RE.match(line.strip()) or _LAB_COLUMNAR_RE.match(line.strip())
    if not m:
        return None
    marker = m.group("marker").strip()
    if not marker or marker.lower() in _SECTION_ALIASES:
        return None
    fields = [
        ParsedField("marker_name", marker, 0.9),
        ParsedField("value_numeric", float(m.group("value")), 0.85),
        ParsedField("unit", (m.group("unit") or None), 0.75 if m.group("unit") else 0.4),
    ]
    if m.group("low") and m.group("high"):
        fields.append(ParsedField("reference_range_low", float(m.group("low")), 0.8))
        fields.append(ParsedField("reference_range_high", float(m.group("high")), 0.8))
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


def parse_text(text: str) -> list[ParsedEntity]:
    """Parse free text into typed entities by walking section headers."""
    entities: list[ParsedEntity] = []
    section: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        detected = _detect_section(line)
        if detected:
            section = detected
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
    return entities
