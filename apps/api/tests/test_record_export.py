"""There was no way to get a patient's record out of this system.

Every read was a shape assembled for one of our own screens, paged for a chart view, and
understood by nothing outside this repository. A clinician referring a patient onward, a
patient exercising the DPDP Act's right to their own data, and a hospital migrating off the
product all had the same answer: read it off the screen.

What is asserted here is what a *receiving* system needs, which is more than "the fields are
present":

* it is FHIR R4, so something that never learned our field names can read it;
* provenance survives — a receiving system that cannot tell an unreviewed OCR extraction from
  a clinician-confirmed entry will treat both as fact;
* a derived marker carries the equation that produced it, because an eGFR is only interpretable
  against its equation and this system has already shipped a defect where an adult equation was
  applied to a child;
* an incomplete export says so *in the bundle*, since the bundle is what gets saved and mailed;
* reasoning output is **not** in it — a differential that arrives elsewhere as a plain FHIR
  ``Condition`` is indistinguishable from a diagnosis a clinician made;
* and the disclosure is audited, because this is the widest one the API performs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.models.allergy import Allergy
from app.models.audit_log import AuditLog
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.export_service import PROVENANCE_EXTENSION, PatientExportService

PREFIX = "/api/v1"


def _resources(bundle: dict, resource_type: str) -> list[dict]:
    return [
        entry["resource"]
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == resource_type
    ]


def _provenance(resource: dict) -> dict[str, object]:
    """The extension this exporter hangs confirmation and source-document on, flattened."""
    extensions = [e for e in resource.get("extension", []) if e["url"] == PROVENANCE_EXTENSION]
    assert extensions, f"{resource['resourceType']} carries no provenance extension"
    return {
        part["url"]: part.get("valueBoolean", part.get("valueString"))
        for part in extensions[0]["extension"]
    }


@pytest_asyncio.fixture
async def auth_account(db, auth_client) -> Account:
    """The ``Account`` behind the ``auth_client`` fixture, for building chart rows directly."""
    return (
        await db.execute(select(Account).where(Account.email == "doc@example.com"))
    ).scalar_one()


@pytest_asyncio.fixture
async def charted(db, auth_account):
    """A patient with one of everything, including a row a clinician has not confirmed."""
    patient = Patient(
        account_id=auth_account.id,
        full_name="Asha Menon",
        date_of_birth=date(1968, 4, 2),
        sex="female",
        phone="+91 98765 43210",
        address_text="12 Residency Road, Bengaluru",
        consent_given=True,
    )
    db.add(patient)
    await db.flush()

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
            Allergy(
                patient_id=patient.id,
                allergen_name="Sulfa drugs",
                allergen_type="drug",
                status="refuted",
                clinician_confirmed=True,
            ),
            Condition(
                patient_id=patient.id,
                condition_name="Type 2 diabetes mellitus",
                icd10_code="E11",
                status="active",
                onset_date=date(2015, 1, 1),
                severity="moderate",
                clinician_confirmed=True,
            ),
            MedicationEvent(
                patient_id=patient.id,
                brand_name_raw="Glycomet GP 1",
                generic_name="Metformin + Glimepiride",
                dose="1",
                dose_unit="tablet",
                frequency="OD",
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
            LabResult(
                patient_id=patient.id,
                marker_name="Urine culture",
                value_text="No growth",
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


async def _export(auth_client, patient_id) -> dict:
    resp = await auth_client.get(f"{PREFIX}/patients/{patient_id}/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/fhir+json")
    return resp.json()


# --- The bundle ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_export_is_a_fhir_collection_bundle(auth_client, charted):
    """``collection``, not ``document``: a FHIR document is a signed, attested composition and
    this system is in no position to attest."""
    bundle = await _export(auth_client, charted.id)
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "collection"
    assert bundle["timestamp"]
    assert all("fullUrl" in entry and "resource" in entry for entry in bundle["entry"])


@pytest.mark.asyncio
async def test_every_section_of_the_chart_is_in_the_bundle(auth_client, charted):
    bundle = await _export(auth_client, charted.id)
    present = {entry["resource"]["resourceType"] for entry in bundle["entry"]}
    assert present == {
        "Patient",
        "AllergyIntolerance",
        "Condition",
        "MedicationStatement",
        "Observation",
    }


@pytest.mark.asyncio
async def test_demographics_are_decrypted_and_carried(auth_client, charted):
    """The identifying columns are encrypted at rest; an export of ciphertext would be a file
    nobody can read, and the failure would look like a successful export."""
    patient = _resources(await _export(auth_client, charted.id), "Patient")[0]
    assert patient["name"][0]["text"] == "Asha Menon"
    assert patient["birthDate"] == "1968-04-02"
    assert patient["gender"] == "female"
    assert patient["telecom"][0]["value"] == "+91 98765 43210"


@pytest.mark.asyncio
async def test_a_refuted_allergy_is_exported_as_refuted_not_merely_inactive(auth_client, charted):
    """ "Someone looked and it is not true" is a different claim from "no longer active", and
    FHIR carries it in verificationStatus. Flattening the two loses that a clinician actively
    ruled the allergy out — and a receiving system may then re-raise it."""
    allergies = {
        row["code"]["text"]: row
        for row in _resources(await _export(auth_client, charted.id), "AllergyIntolerance")
    }
    refuted = allergies["Sulfa drugs"]
    assert refuted["verificationStatus"]["coding"][0]["code"] == "refuted"

    real = allergies["Penicillin"]
    assert real["clinicalStatus"]["coding"][0]["code"] == "active"
    assert real["criticality"] == "high"


@pytest.mark.asyncio
async def test_a_condition_carries_its_icd10_coding(auth_client, charted):
    condition = _resources(await _export(auth_client, charted.id), "Condition")[0]
    coding = condition["code"]["coding"][0]
    assert coding["system"] == "http://hl7.org/fhir/sid/icd-10"
    assert coding["code"] == "E11"
    assert condition["code"]["text"] == "Type 2 diabetes mellitus"


@pytest.mark.asyncio
async def test_a_medication_carries_both_the_generic_and_the_indian_brand(auth_client, charted):
    """The INN is the interoperable name; the brand is what the prescription actually said, and
    for an Indian chart it is often the only thing the source document carried."""
    medication = _resources(await _export(auth_client, charted.id), "MedicationStatement")[0]
    concept = medication["medicationCodeableConcept"]
    assert concept["text"] == "Metformin + Glimepiride"
    assert concept["coding"][0]["display"] == "Glycomet GP 1"
    assert medication["status"] == "active"
    assert medication["dosage"][0]["text"] == "1 tablet OD"


@pytest.mark.asyncio
async def test_a_numeric_lab_carries_value_range_and_interpretation(auth_client, charted):
    labs = {
        row["code"]["text"]: row
        for row in _resources(await _export(auth_client, charted.id), "Observation")
    }
    creatinine = labs["Creatinine"]
    assert creatinine["valueQuantity"] == {"value": 1.4, "unit": "mg/dL"}
    assert creatinine["referenceRange"][0]["high"]["value"] == 1.1
    assert creatinine["interpretation"][0]["coding"][0]["code"] == "H"


@pytest.mark.asyncio
async def test_a_textual_result_never_carries_a_second_value(auth_client, charted):
    """FHIR allows exactly one ``value[x]``. A resource carrying both is invalid rather than
    richer, and an importer is entitled to reject the whole bundle over it."""
    labs = {
        row["code"]["text"]: row
        for row in _resources(await _export(auth_client, charted.id), "Observation")
    }
    culture = labs["Urine culture"]
    assert culture["valueString"] == "No growth"
    assert "valueQuantity" not in culture


@pytest.mark.asyncio
async def test_a_derived_marker_carries_the_equation_that_produced_it(auth_client, charted):
    """An eGFR is only interpretable against its equation — this system has already shipped a
    defect where an adult equation was applied to a child — so a number without the formula is
    a number a receiving system cannot safely re-use."""
    markers = {
        row["code"]["text"]: row
        for row in _resources(await _export(auth_client, charted.id), "Observation")
    }
    egfr = markers["eGFR"]
    assert egfr["valueQuantity"]["value"] == 44.2
    assert "ckd_epi_2021" in egfr["method"]["text"]
    assert "v1" in egfr["method"]["text"]


# --- Provenance -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_and_unreviewed_entries_are_distinguishable_at_the_far_end(
    auth_client, charted
):
    """The property that decides whether this export is safe to import. Without it, an OCR
    guess about a drug and a line a clinician read and confirmed arrive identical, and the
    receiving system treats both as fact."""
    bundle = await _export(auth_client, charted.id)
    condition = _resources(bundle, "Condition")[0]
    medication = _resources(bundle, "MedicationStatement")[0]

    assert _provenance(condition)["clinician-confirmed"] is True
    assert _provenance(medication)["clinician-confirmed"] is False


@pytest.mark.asyncio
async def test_a_resource_carries_the_document_it_was_extracted_from(auth_client, db, charted):
    document_id = uuid.uuid4()
    condition = (
        await db.execute(select(Condition).where(Condition.patient_id == charted.id))
    ).scalar_one()
    condition.source_document_id = document_id
    await db.commit()

    exported = _resources(await _export(auth_client, charted.id), "Condition")[0]
    assert _provenance(exported)["source-document"] == str(document_id)


# --- What is deliberately absent ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_deleted_rows_are_not_exported(auth_client, db, charted):
    """A row withdrawn from the chart is withdrawn from the export. The one place this would
    have leaked back is a bespoke query that forgot the predicate."""
    condition = (
        await db.execute(select(Condition).where(Condition.patient_id == charted.id))
    ).scalar_one()
    condition.is_deleted = True
    await db.commit()

    assert _resources(await _export(auth_client, charted.id), "Condition") == []


@pytest.mark.asyncio
async def test_another_clinicians_patient_cannot_be_exported(auth_client, db):
    """Ownership is checked the same way every other chart read checks it — a 404 rather than a
    403, so the endpoint does not confirm that the record exists."""
    stranger = Patient(account_id=uuid.uuid4(), full_name="Not Yours")
    db.add(stranger)
    await db.commit()

    resp = await auth_client.get(f"{PREFIX}/patients/{stranger.id}/export")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_an_export_requires_authentication(client, charted):
    # `auth_client` sets the header on this same client object, and `charted` needs it — so the
    # header is removed here rather than by asking for an un-authenticated fixture.
    client.headers.pop("Authorization", None)
    assert (await client.get(f"{PREFIX}/patients/{charted.id}/export")).status_code == 401


# --- Completeness ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_truncated_export_says_so_inside_the_bundle(auth_client, charted, monkeypatch):
    """In the bundle, not in a header: the bundle is the artefact that gets saved and imported,
    and a warning that lives only on the HTTP response is gone by the time anyone reads the
    file. A short export that does not admit it is worse than no export — the reader's natural
    assumption about a file called "the patient's record" is that it is the record."""
    monkeypatch.setattr(settings, "export_max_rows_per_section", 1)
    bundle = await _export(auth_client, charted.id)

    outcomes = _resources(bundle, "OperationOutcome")
    assert len(outcomes) == 1
    issue = outcomes[0]["issue"][0]
    assert issue["severity"] == "warning"
    assert "incomplete" in issue["diagnostics"].lower()
    assert "AllergyIntolerance" in issue["diagnostics"]


@pytest.mark.asyncio
async def test_a_complete_export_carries_no_warning(auth_client, charted):
    assert _resources(await _export(auth_client, charted.id), "OperationOutcome") == []


# --- Audit ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_export_is_audited_as_a_disclosure(auth_client, db, charted):
    """The widest disclosure this API performs — a whole chart leaving in one file — and it has
    to be as legible in the trail as the narrower reads beside it."""
    await _export(auth_client, charted.id)

    rows = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "patient_record_exported")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].patient_id == charted.id
    assert rows[0].payload["format"] == "fhir-r4"
    assert rows[0].payload["resources"] > 0


# --- Service level -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_patient_with_an_empty_chart_still_exports(db, auth_account):
    """An empty record is a valid answer, not an error — and a bundle carrying only the Patient
    is what says "there is nothing else here" rather than leaving the caller guessing."""
    patient = Patient(account_id=auth_account.id, full_name="New Patient")
    db.add(patient)
    await db.commit()

    bundle = await PatientExportService(db).build_bundle(patient)
    assert [e["resource"]["resourceType"] for e in bundle["entry"]] == ["Patient"]


@pytest.mark.asyncio
async def test_no_exported_element_is_null(db, auth_account):
    """FHIR forbids null-valued elements; a resource full of them is one an importer rejects.
    Asserted recursively so a new field added without pruning fails here rather than at a
    hospital's ingest endpoint."""
    patient = Patient(account_id=auth_account.id, full_name="Sparse Chart", sex=None)
    db.add(patient)
    await db.flush()
    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name="Haemoglobin",
            value_numeric=Decimal("11.2"),
        )
    )
    await db.commit()

    bundle = await PatientExportService(db).build_bundle(patient)

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert value is not None, f"null element at {path}.{key}"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                assert value is not None, f"null element at {path}[{index}]"
                walk(value, f"{path}[{index}]")

    walk(bundle, "Bundle")
