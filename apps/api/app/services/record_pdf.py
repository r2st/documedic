"""The patient's record as a printable document.

The FHIR bundle beside this is for a receiving *system*. This is for a person: the clinician
writing a referral, the patient exercising the DPDP Act's right to their own data, the file that
goes in a folder. Neither replaces the other, and both are built from the same
:class:`~app.services.export_service.RecordSections` read so they cannot disagree about what is
on the chart.

What is on the page, and why in this order
------------------------------------------
Allergies first, above everything, because that is the one section whose absence from a glance
can kill someone — it is the first thing a clinician looks for on a handed-over record, and it
is short. Then conditions, medications, labs, computed markers, visits: standing facts before
events, and events newest first, which is how a chart is read.

Provenance is on every row, in words rather than in an extension URL: a line reads "(unconfirmed
extraction)" when no clinician has confirmed it. The bundle carries the same fact as a coded
extension, and for the same reason — a reader who cannot tell an OCR guess from a confirmed
entry will treat both as fact.

What is deliberately absent is the same as in the bundle: no differential diagnoses, no agent
deliberation, no clinical suggestions. Those are this system's opinions about the patient rather
than the patient's record, and a printed page carrying both with equal typographic weight is the
automation bias this product exists to resist, on paper, where nothing marks which is which.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.config import settings
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.encounter import Encounter
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.services.export_service import RecordSections
from app.services.pdf import PdfBuilder

# How far a wrapped continuation line and a row's detail line are indented under their entry.
_INDENT = 12.0

_UNCONFIRMED = "unconfirmed extraction"

# ``RecordSections.incomplete`` names sections the way the FHIR bundle does, because that is what
# a receiving system's ``OperationOutcome`` has to say. A clinician reading a printed page is
# owed the name of the section they can see on it: "Observation (laboratory)" is jargon from a
# domain they have no reason to know, and a warning nobody parses is a warning nobody heeds.
_SECTION_NAMES = {
    "Encounter": "Encounters",
    "AllergyIntolerance": "Allergies",
    "Condition": "Conditions",
    "MedicationStatement": "Medications",
    "Observation (laboratory)": "Lab results",
    "Observation (derived)": "Computed markers",
}


def _day(value: date | datetime | None) -> str:
    if value is None:
        return "date not recorded"
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d")


def _number(value: Decimal | None) -> str:
    """A measured value as written, with a trailing ``.000000`` from the numeric column trimmed.

    Never rounded. A lab value is a measurement, and a printed record that quietly changes one
    by a digit is a different clinical fact from the one in the database.
    """
    if value is None:
        return ""
    text = format(value.normalize(), "f")
    return text


def _joined(*parts: str | None) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def _provenance(confirmed: bool) -> str:
    return "" if confirmed else f" ({_UNCONFIRMED})"


def _allergy_line(row: Allergy) -> str:
    detail = _joined(
        row.severity and f"{row.severity} severity",
        row.reaction_description,
        f"first recorded {_day(row.onset_date)}" if row.onset_date else None,
    )
    head = f"{row.allergen_name} — {row.status}"
    return _joined(head, detail and f"— {detail}") + _provenance(row.clinician_confirmed)


def _condition_line(row: Condition) -> str:
    detail = _joined(
        row.icd10_code and f"ICD-10 {row.icd10_code}",
        row.severity,
        f"onset {_day(row.onset_date)}" if row.onset_date else None,
        f"resolved {_day(row.resolution_date)}" if row.resolution_date else None,
    )
    head = f"{row.condition_name} — {row.status}"
    return _joined(head, detail and f"— {detail}") + _provenance(row.clinician_confirmed)


def _medication_line(row: MedicationEvent) -> str:
    name = row.generic_name or row.brand_name_raw or "medication not named"
    if row.brand_name_raw and row.generic_name and row.brand_name_raw != row.generic_name:
        name = f"{row.generic_name} ({row.brand_name_raw})"
    detail = _joined(
        _joined(row.dose, row.dose_unit),
        row.frequency,
        row.route,
        row.duration_text,
        row.prescriber_name and f"prescriber {row.prescriber_name}",
    )
    # The event type is spelled out rather than abbreviated: "stop" beside a drug name on a
    # printed page has to be unmistakable, because the reader has no tooltip and no chart
    # underneath it.
    standing = "current" if row.is_current else "not current"
    head = f"{name} — {row.event_type} on {_day(row.event_date)} — {standing}"
    return _joined(head, detail and f"— {detail}") + _provenance(row.clinician_confirmed)


def _lab_line(row: LabResult) -> str:
    value = _joined(_number(row.value_numeric) or row.value_text, row.unit) or "no value recorded"
    reference = row.reference_range_text or _joined(
        _number(row.reference_range_low),
        (
            "-"
            if row.reference_range_low is not None and row.reference_range_high is not None
            else None
        ),
        _number(row.reference_range_high),
    )
    flag = ""
    if row.is_abnormal:
        flag = (
            f" [abnormal{f' — {row.abnormality_direction}' if row.abnormality_direction else ''}]"
        )
    detail = _joined(
        reference and f"reference {reference}",
        row.lab_name,
    )
    head = f"{row.marker_name}: {value}{flag} — {_day(row.sample_date)}"
    return _joined(head, detail and f"— {detail}") + _provenance(row.clinician_confirmed)


def _marker_line(row: DerivedMarker) -> str:
    value = _joined(_number(row.value_numeric), row.unit)
    head = f"{row.marker_name}: {value} — computed {_day(row.computed_at)}"
    return _joined(head, f"— {row.formula_name} v{row.formula_version}")


def _encounter_line(row: Encounter) -> str:
    head = f"{_day(row.encounter_date)} — {row.encounter_type or 'visit type not recorded'}"
    # No clinician-confirmation flag exists on this row — an encounter is asserted by the
    # document it was read out of, never separately confirmed — so every visit prints as an
    # unconfirmed extraction. That is the same statement the bundle makes in its provenance
    # extension, and it is the honest one: saying nothing would read as "confirmed".
    return head + _provenance(False)


def _encounter_detail(row: Encounter) -> list[str]:
    out: list[str] = []
    if row.presenting_complaint:
        out.append(f"Presenting complaint: {row.presenting_complaint}")
    if row.clinician_notes:
        out.append(f"Notes: {row.clinician_notes}")
    return out


def build_record_pdf(
    sections: RecordSections, *, generated_at: datetime | None = None
) -> tuple[bytes, bool]:
    """Render ``sections`` as a PDF. Returns the file and whether any character was lost.

    The loss flag comes from :mod:`app.services.pdf` — the base-14 fonts cannot draw scripts
    outside Latin-1 — and is reported to the caller as well as printed on the page, so the audit
    record of the export says whether the file that left the building was a faithful rendering
    of the name on it.
    """
    stamp = generated_at or datetime.now(UTC)
    patient = sections.patient
    pdf = PdfBuilder(title=f"Patient record — {patient.full_name}", created_at=stamp)

    pdf.text("DoAide Med — patient record", size=15, bold=True, leading=19)
    pdf.text(
        "Clinical decision support record export. This is a copy of what was recorded, not a "
        "clinical opinion: it carries no differential diagnoses, no management suggestions and "
        "no reasoning output.",
        size=8.5,
        leading=11,
    )
    if settings.demo_mode:
        # The demo banner is persistent everywhere else in the product for a reason, and a page
        # that leaves the building is the one place its absence would be permanent.
        pdf.text(
            "DEMO MODE — this deployment is not a production clinical system and this record "
            "is not a clinical document.",
            size=8.5,
            bold=True,
            leading=11,
        )
    pdf.space(4)

    pdf.heading("Patient")
    pdf.field("Name", patient.full_name)
    pdf.field("Record id", str(patient.id))
    pdf.field("Date of birth", _day(patient.date_of_birth))
    pdf.field("Sex", patient.sex or "not recorded")
    pdf.field("Exported", stamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"))

    def section(title: str, lines: list[str], *, empty: str) -> None:
        pdf.heading(title)
        if not lines:
            pdf.text(empty, indent=_INDENT)
            return
        for line in lines:
            pdf.text(line, indent=_INDENT)

    # "None recorded", never a blank section. An empty heading reads as "nothing here", and for
    # allergies the difference between "no allergies" and "no allergy information" is the whole
    # question — so each empty section says which of the two it is in words.
    section(
        "Allergies",
        [_allergy_line(row) for row in sections.allergies],
        empty="No allergies recorded on this chart. This is not the same as none being known.",
    )
    section(
        "Conditions",
        [_condition_line(row) for row in sections.conditions],
        empty="No conditions recorded on this chart.",
    )
    section(
        "Medications",
        [_medication_line(row) for row in sections.medications],
        empty="No medication events recorded on this chart.",
    )
    section(
        "Lab results",
        [_lab_line(row) for row in sections.labs],
        empty="No lab results recorded on this chart.",
    )
    section(
        "Computed markers",
        [_marker_line(row) for row in sections.markers],
        empty="No computed markers on this chart.",
    )

    pdf.heading("Encounters")
    if not sections.encounters:
        pdf.text("No encounters recorded on this chart.", indent=_INDENT)
    for row in sections.encounters:
        pdf.text(_encounter_line(row), indent=_INDENT)
        for detail in _encounter_detail(row):
            pdf.text(detail, indent=_INDENT * 2, size=8.5, leading=11)

    if sections.incomplete:
        # Same statement the bundle makes as an OperationOutcome, and it has to be as loud here:
        # a printed chart missing its oldest labs looks exactly like a chart that has none.
        named = [_SECTION_NAMES.get(section, section) for section in sections.incomplete]
        pdf.heading("This export is incomplete")
        pdf.text(
            "The following sections exceeded the per-section export limit of "
            f"{settings.export_max_rows_per_section} rows and are truncated: "
            f"{', '.join(named)}. This file is not the whole record.",
            indent=_INDENT,
        )

    data, lossy = pdf.render()
    if not lossy:
        return data, False

    # Re-rendered with the warning on it. Cheaper than measuring every string up front, and it
    # keeps the check where the loss actually happens — at the point of encoding — rather than
    # in a second implementation of the same rule that could drift from it.
    pdf.heading("Some characters could not be printed")
    pdf.text(
        "This document is typeset in a font that covers the Latin alphabet only, so any "
        "character outside it has been replaced with '?'. Names and free text may therefore be "
        "rendered incorrectly on this page. The JSON export of the same record carries the "
        "original text unaltered.",
        indent=_INDENT,
    )
    data, _ = pdf.render()
    return data, True
