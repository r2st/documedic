"""The record export, for a person rather than for a system.

The FHIR bundle solved half the problem: a receiving EMR can read the chart. The other half is
everyone who is not a system — the clinician writing a referral by hand, the patient who asks
for a copy of their own record under the DPDP Act, the file that goes in a folder. Handing them
a JSON bundle is the same answer as before ("read it off the screen") with extra steps.

So there is a second format, and what is asserted here is what makes it a *record* rather than a
printout:

* it is a real PDF — parseable by something that is not us, since the whole point is that it
  leaves;
* it carries the same rows as the bundle, read once, so the two cannot drift apart;
* provenance survives into print — a line a clinician confirmed and a line an OCR pass guessed
  at cannot look identical on paper, where there is no tooltip to hover;
* an incomplete export says so on the page;
* reasoning output is not on it, for the reason it is not in the bundle;
* the disclosure is audited and metered on the same bucket as the bundle, because the ceiling is
  on pulling a whole chart out of the system and that does not depend on the file extension; and
* a name this document's font cannot draw is reported rather than silently mangled.
"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pypdf
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.models.allergy import Allergy
from app.models.audit_log import AuditLog
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.encounter import Encounter
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account

PREFIX = "/api/v1"


@pytest_asyncio.fixture
async def auth_account(db, auth_client) -> Account:
    return (
        await db.execute(select(Account).where(Account.email == "doc@example.com"))
    ).scalar_one()


async def _make_patient(db, account: Account, *, full_name: str = "Asha Menon") -> Patient:
    patient = Patient(
        account_id=account.id,
        full_name=full_name,
        date_of_birth=date(1968, 4, 2),
        sex="female",
        consent_given=True,
    )
    db.add(patient)
    await db.flush()
    return patient


@pytest_asyncio.fixture
async def charted(db, auth_account) -> Patient:
    """A patient with one of everything, including a row nobody has confirmed."""
    patient = await _make_patient(db, auth_account)
    encounter = Encounter(
        patient_id=patient.id,
        encounter_date=date(2026, 7, 1),
        encounter_type="outpatient",
        presenting_complaint="Swelling of the ankles for two weeks",
        clinician_notes="Reviewed renal function; nephrology referral discussed.",
    )
    db.add(encounter)
    db.add_all(
        [
            Allergy(
                patient_id=patient.id,
                allergen_name="Penicillin",
                allergen_type="drug",
                reaction_description="Urticaria",
                severity="severe",
                status="active",
                clinician_confirmed=True,
            ),
            Condition(
                patient_id=patient.id,
                condition_name="Type 2 diabetes mellitus",
                icd10_code="E11",
                status="active",
                onset_date=date(2015, 1, 1),
                clinician_confirmed=True,
            ),
            MedicationEvent(
                patient_id=patient.id,
                brand_name_raw="Glycomet GP 1",
                generic_name="Metformin",
                dose="500",
                dose_unit="mg",
                frequency="BD",
                route="oral",
                event_type="start",
                event_date=date(2023, 6, 1),
                is_current=True,
                clinician_confirmed=False,  # an unreviewed extraction
            ),
            LabResult(
                patient_id=patient.id,
                marker_name="Creatinine",
                value_numeric=Decimal("1.4"),
                unit="mg/dL",
                reference_range_low=Decimal("0.6"),
                reference_range_high=Decimal("1.1"),
                is_abnormal=True,
                abnormality_direction="high",
                sample_date=datetime(2026, 7, 1, 9, 30, tzinfo=UTC),
                clinician_confirmed=True,
            ),
            DerivedMarker(
                patient_id=patient.id,
                marker_name="eGFR",
                value_numeric=Decimal("44.2"),
                unit="mL/min/1.73m2",
                formula_name="ckd_epi_2021",
                formula_version="v1",
                input_values={"creatinine": "1.4"},
                computed_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            ),
        ]
    )
    await db.commit()
    return patient


async def _pdf(auth_client, patient_id) -> tuple[bytes, str]:
    """The exported PDF and its extracted text, asserted to be a PDF something else can read."""
    resp = await auth_client.get(f"{PREFIX}/patients/{patient_id}/export/pdf")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    assert f'filename="aether-record-{patient_id}.pdf"' in resp.headers["content-disposition"]
    body = resp.content
    assert body.startswith(b"%PDF-")
    reader = pypdf.PdfReader(io.BytesIO(body))
    return body, "\n".join(page.extract_text() for page in reader.pages)


# --- It is a document -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_export_is_a_pdf_another_program_can_open(auth_client, charted):
    """Not a smoke test. A file we alone can read is not an export — the entire purpose is that
    it leaves this system, so it is parsed back with a library that knows nothing about us."""
    body, text = await _pdf(auth_client, charted.id)

    reader = pypdf.PdfReader(io.BytesIO(body))
    assert len(reader.pages) >= 1
    assert reader.metadata is not None
    assert "Asha Menon" in str(reader.metadata.title)
    assert "Asha Menon" in text


@pytest.mark.asyncio
async def test_every_section_of_the_chart_is_on_the_page(auth_client, charted):
    _, text = await _pdf(auth_client, charted.id)

    assert "Penicillin" in text
    assert "Type 2 diabetes mellitus" in text
    assert "Metformin" in text
    assert "Creatinine" in text and "1.4" in text
    assert "eGFR" in text and "44.2" in text
    assert "Swelling of the ankles" in text


@pytest.mark.asyncio
async def test_allergies_are_printed_before_everything_else(auth_client, charted):
    """The one section whose absence from a glance can kill someone.

    A handed-over record is read top-down under time pressure, and the allergy list is short.
    Ordering it after the medication history — which is what a chronological layout would do —
    puts it below the fold on any chart with a real drug history.
    """
    _, text = await _pdf(auth_client, charted.id)

    assert text.index("Allergies") < text.index("Medications")
    assert text.index("Allergies") < text.index("Lab results")


@pytest.mark.asyncio
async def test_an_unconfirmed_extraction_says_so_on_paper(auth_client, charted):
    """Provenance has to survive into print.

    On screen a confidence badge does this. On paper there is nothing to hover, so a line an OCR
    pass guessed at and a line a clinician read and confirmed would be the same sentence in the
    same typeface — and the reader would be right to treat both as fact.
    """
    _, text = await _pdf(auth_client, charted.id)

    metformin = next(line for line in text.splitlines() if "Metformin" in line)
    penicillin = next(line for line in text.splitlines() if "Penicillin" in line)
    assert "unconfirmed extraction" in metformin
    assert "unconfirmed extraction" not in penicillin


@pytest.mark.asyncio
async def test_an_empty_section_says_which_kind_of_empty_it_is(auth_client, db, auth_account):
    """ "No allergies recorded" and "no allergy information" are different clinical statements,
    and a blank heading is read as the first one."""
    patient = await _make_patient(db, auth_account, full_name="Empty Chart")
    await db.commit()

    _, text = await _pdf(auth_client, patient.id)

    assert "No allergies recorded on this chart" in text
    assert "not the same as none being known" in text


@pytest.mark.asyncio
async def test_the_page_carries_no_reasoning_output(auth_client, charted):
    """Same exclusion as the bundle. A differential diagnosis printed in the same typeface as a
    recorded condition is indistinguishable from a diagnosis a clinician made — and on paper
    there is nothing at all to mark which is which."""
    _, text = await _pdf(auth_client, charted.id)

    lowered = " ".join(text.lower().split())
    # The page says what it is before it says anything about the patient, so a reader cannot
    # mistake the omission for an oversight — which is why the vocabulary check below starts
    # after the header rather than at the top of the file.
    assert "not a clinical opinion" in lowered
    assert "carries no differential diagnoses" in lowered

    body = lowered.split("allergies", 1)[1]
    for absent in ("differential", "hypothesis", "devil", "autonomy tier", "can't-miss"):
        assert absent not in body, absent


# --- Truncation, encoding, and the things the file must admit about itself --------------------


@pytest.mark.asyncio
async def test_a_truncated_export_says_so_on_the_page(auth_client, db, auth_account, monkeypatch):
    """A printed chart missing its oldest labs looks exactly like a chart that never had any."""
    patient = await _make_patient(db, auth_account, full_name="Long Chart")
    db.add_all(
        [
            LabResult(
                patient_id=patient.id,
                marker_name=f"Marker {index}",
                value_numeric=Decimal("1.0"),
                unit="mg/dL",
                sample_date=datetime(2026, 7, 1, 9, 30, tzinfo=UTC),
                clinician_confirmed=True,
            )
            for index in range(4)
        ]
    )
    await db.commit()
    monkeypatch.setattr(settings, "export_max_rows_per_section", 2)

    _, text = await _pdf(auth_client, patient.id)

    assert "This export is incomplete" in text
    # Named the way the page names the section, not the way the FHIR bundle names the resource:
    # a clinician holding the printout has no reason to know what an "Observation" is.
    assert "Lab results" in " ".join(text.split())
    assert "not the whole record" in text


@pytest.mark.asyncio
async def test_a_name_the_font_cannot_draw_is_reported_not_silently_mangled(
    auth_client, db, auth_account
):
    """The base-14 fonts cover Latin-1 and no more, which in India is a real limit and not an
    edge case. A record that quietly misspells the patient's name is worse than one that says it
    could not print it — so the page says so, and the audit row records that it happened."""
    patient = await _make_patient(db, auth_account, full_name="नमस्ते Menon")
    await db.commit()

    _, text = await _pdf(auth_client, patient.id)

    assert "Some characters could not be printed" in text
    assert "JSON export" in text

    entry = (
        (
            await db.execute(
                select(AuditLog)
                .where(
                    AuditLog.patient_id == patient.id, AuditLog.action == "patient_record_exported"
                )
                .order_by(AuditLog.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    assert entry is not None
    assert entry.payload["characters_dropped"] is True


@pytest.mark.asyncio
async def test_an_ascii_record_reports_no_characters_dropped(auth_client, db, charted):
    await _pdf(auth_client, charted.id)

    entry = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.patient_id == charted.id, AuditLog.action == "patient_record_exported"
                )
            )
        )
        .scalars()
        .first()
    )
    assert entry is not None
    assert entry.payload["characters_dropped"] is False
    assert entry.payload["format"] == "pdf"


# --- Disclosure controls ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_export_is_audited_as_the_widest_disclosure_this_api_performs(
    auth_client, db, charted
):
    await _pdf(auth_client, charted.id)

    actions = (
        (await db.execute(select(AuditLog.action).where(AuditLog.patient_id == charted.id)))
        .scalars()
        .all()
    )
    assert "patient_record_exported" in actions


@pytest.mark.asyncio
async def test_both_export_formats_draw_on_one_rate_limit(auth_client, charted, monkeypatch):
    """The ceiling is on pulling a whole chart out of the system.

    Metering the two formats separately would hand a stolen bearer token twice the bulk-retrieval
    rate for no clinical reason — the same chart, twice, in two file extensions.
    """
    monkeypatch.setattr(settings, "rate_limit_exports_per_hour", 2)

    first = await auth_client.get(f"{PREFIX}/patients/{charted.id}/export")
    second = await auth_client.get(f"{PREFIX}/patients/{charted.id}/export/pdf")
    third = await auth_client.get(f"{PREFIX}/patients/{charted.id}/export/pdf")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429


@pytest.mark.asyncio
async def test_another_accounts_chart_is_a_404(second_auth_client, charted):
    resp = await second_auth_client.get(f"{PREFIX}/patients/{charted.id}/export/pdf")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_the_pdf_and_the_bundle_describe_the_same_chart(auth_client, charted):
    """Both formats read one set of rows under one ceiling. If either loaded its own, a change
    to the ordering or the limit for one would silently not apply to the other, and nobody would
    notice until a referral and a printout disagreed."""
    bundle = (await auth_client.get(f"{PREFIX}/patients/{charted.id}/export")).json()
    _, text = await _pdf(auth_client, charted.id)

    for entry in bundle["entry"]:
        resource = entry["resource"]
        if resource["resourceType"] == "AllergyIntolerance":
            assert resource["code"]["text"] in text
        if resource["resourceType"] == "Condition":
            assert resource["code"]["text"] in text
