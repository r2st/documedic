"""One FHIR resource type at a time, and the MedicationRequest reading of a medication row.

The whole-record bundle is the referral artefact: everything, in one file, unpaged. It is also
all-or-nothing, and interoperability mostly is not — a registry that holds conditions, a nightly
observation sync, an ordering module importing prescriptions each had to pull the entire chart
and discard nine tenths of it.

Two things need pinning beyond "the filter works":

* **``MedicationStatement`` and ``MedicationRequest`` are two readings of one set of rows**, so
  the whole-record bundle must carry exactly one of them. A bundle carrying both would state
  every medication twice, and an importer has no way to tell they are the same prescription.
* **Nothing in a single-type bundle may reference a resource that is not in it.** The collection
  bundle can point a Condition at its Encounter because both are present; here they are not, and
  a dangling reference is worse than an absent one — the same rule the truncation path follows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.models.condition import Condition
from app.models.encounter import Encounter
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.export_service import SUPPORTED_RESOURCE_TYPES, PatientExportService

from .conftest import create_patient

pytestmark = pytest.mark.asyncio


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"fhir-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="FHIR Patient",
        sex="male",
        date_of_birth=date(1970, 1, 1),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _populate(db, patient: Patient) -> Encounter:
    encounter = Encounter(
        patient_id=patient.id,
        encounter_date=date(2025, 4, 2),
        encounter_type="outpatient",
        status="draft",
    )
    db.add(encounter)
    await db.flush()
    db.add(
        Condition(
            patient_id=patient.id,
            condition_name="Type 2 diabetes mellitus",
            status="active",
            encounter_id=encounter.id,
        )
    )
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            event_type="start",
            is_current=True,
            generic_name="Metformin",
            brand_name_raw="Glycomet 500",
            dose="500",
            dose_unit="mg",
            frequency="BD",
            route="oral",
            event_date=date(2025, 4, 2),
            encounter_id=encounter.id,
            prescriber_name="Dr A Sharma",
        )
    )
    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name="HbA1c",
            value_numeric=9.1,
            unit="%",
            is_abnormal=True,
            sample_date=datetime(2025, 4, 1, tzinfo=UTC),
        )
    )
    await db.flush()
    return encounter


def _types(bundle: dict) -> set[str]:
    return {entry["resource"]["resourceType"] for entry in bundle["entry"]}


# --- the whole-record bundle is unchanged ---------------------------------------------------


async def test_the_collection_bundle_still_carries_only_the_statement_reading(db) -> None:
    """A bundle carrying both would state every medication twice, indistinguishably."""
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    bundle = await PatientExportService(db).build_bundle(patient)

    assert "MedicationStatement" in _types(bundle)
    assert "MedicationRequest" not in _types(bundle)
    assert bundle["type"] == "collection"


# --- the per-type bundle ---------------------------------------------------------------------


@pytest.mark.parametrize("resource_type", SUPPORTED_RESOURCE_TYPES)
async def test_every_supported_type_returns_a_searchset_of_only_that_type(
    db, resource_type
) -> None:
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    bundle = await PatientExportService(db).build_resource_bundle(patient, resource_type)

    assert bundle["type"] == "searchset"
    assert bundle["total"] == len(bundle["entry"])
    # Observation covers labs and computed markers; everything else is exactly its own type.
    assert _types(bundle) <= {resource_type}


async def test_the_type_name_is_matched_case_insensitively(db) -> None:
    """A caller writing ``medicationrequest`` has unambiguously asked for one thing."""
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    bundle = await PatientExportService(db).build_resource_bundle(patient, "medicationrequest")

    assert _types(bundle) == {"MedicationRequest"}


async def test_an_unknown_type_is_refused_rather_than_answered_with_an_empty_bundle(db) -> None:
    """An empty searchset says this patient has no such resources, which is a different claim."""
    from app.exceptions import ValidationError

    _, patient = await _account_and_patient(db)

    with pytest.raises(ValidationError):
        await PatientExportService(db).build_resource_bundle(patient, "Procedure")


async def test_a_single_type_bundle_carries_no_reference_to_a_resource_it_lacks(db) -> None:
    """The condition was recorded at a visit. That visit is not in this file."""
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    bundle = await PatientExportService(db).build_resource_bundle(patient, "Condition")

    condition = bundle["entry"][0]["resource"]
    assert condition["resourceType"] == "Condition"
    assert "encounter" not in condition


async def test_observations_cover_both_labs_and_computed_markers(db) -> None:
    from app.models.derived_marker import DerivedMarker

    _, patient = await _account_and_patient(db)
    await _populate(db, patient)
    db.add(
        DerivedMarker(
            patient_id=patient.id,
            marker_name="eGFR",
            value_numeric=64,
            unit="mL/min/1.73m2",
            formula_name="CKD-EPI",
            formula_version="2021",
            input_values={"creatinine_mg_dl": "1.1"},
            computed_at=datetime.now(UTC),
        )
    )
    await db.flush()

    bundle = await PatientExportService(db).build_resource_bundle(patient, "Observation")

    codes = {entry["resource"]["code"]["text"] for entry in bundle["entry"]}
    assert {"HbA1c", "eGFR"} <= codes


# --- what a MedicationRequest says ------------------------------------------------------------


async def test_a_medication_request_says_it_is_a_transcription_and_not_our_order(db) -> None:
    """This system read the prescription off a document. It did not issue one."""
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    bundle = await PatientExportService(db).build_resource_bundle(patient, "MedicationRequest")

    request = bundle["entry"][0]["resource"]
    assert request["intent"] == "order"
    assert request["reportedBoolean"] is True
    # Display-only: there is no Practitioner resource behind the name, and inventing one would
    # assert an identity this record cannot resolve.
    assert request["requester"] == {"display": "Dr A Sharma"}
    assert "Metformin" in request["medicationCodeableConcept"]["text"]


async def test_a_medication_requests_dosage_is_the_charted_line(db) -> None:
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    bundle = await PatientExportService(db).build_resource_bundle(patient, "MedicationRequest")

    dosage = bundle["entry"][0]["resource"]["dosageInstruction"][0]
    assert dosage["text"] == "500 mg BD"
    assert dosage["route"] == {"text": "oral"}


@pytest.mark.parametrize(
    ("event_type", "is_current", "expected"),
    [
        ("stop", True, "stopped"),
        ("stop", False, "stopped"),
        ("start", True, "active"),
        ("start", False, "completed"),
    ],
)
async def test_a_stop_event_is_stopped_whatever_the_flag_says(
    db, event_type, is_current, expected
) -> None:
    """The same precedence MedicationStatement applies, in MedicationRequest's own vocabulary."""
    _, patient = await _account_and_patient(db)
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            event_type=event_type,
            is_current=is_current,
            generic_name="Warfarin",
            event_date=date(2025, 1, 1),
        )
    )
    await db.flush()

    bundle = await PatientExportService(db).build_resource_bundle(patient, "MedicationRequest")

    assert bundle["entry"][0]["resource"]["status"] == expected


async def test_a_medication_request_carries_the_same_provenance_the_statement_does(db) -> None:
    """A receiving system that cannot tell an OCR guess from a confirmed entry treats both as
    fact."""
    _, patient = await _account_and_patient(db)
    await _populate(db, patient)

    bundle = await PatientExportService(db).build_resource_bundle(patient, "MedicationRequest")

    extension = bundle["entry"][0]["resource"]["extension"][0]["extension"]
    assert {"url": "clinician-confirmed", "valueBoolean": False} in extension


# --- over the wire ------------------------------------------------------------------------------


async def test_the_route_serves_fhir_json(auth_client) -> None:
    patient = await create_patient(auth_client)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/export/Condition")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/fhir+json")
    assert resp.json()["type"] == "searchset"


async def test_the_pdf_route_is_not_shadowed_by_the_resource_type_route(auth_client) -> None:
    """``/export/pdf`` is registered first; a type named "pdf" would be a routing accident."""
    patient = await create_patient(auth_client)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/export/pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")


async def test_an_unsupported_type_is_a_422_naming_what_is_supported(auth_client) -> None:
    patient = await create_patient(auth_client)

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/export/Procedure")

    assert resp.status_code == 422
    assert "MedicationRequest" in resp.json()["message"]


async def test_the_per_type_export_records_which_slice_left_the_system(auth_client) -> None:
    """Otherwise the trail cannot tell a whole-chart disclosure from a single-section one."""
    patient = await create_patient(auth_client)
    await auth_client.get(f"/api/v1/patients/{patient['id']}/export/Observation")

    trail = (await auth_client.get(f"/api/v1/patients/{patient['id']}/audit")).json()

    exported = [e for e in trail["items"] if e["action"] == "patient_record_exported"]
    assert exported and exported[0]["payload"]["resource_type"] == "Observation"


async def test_another_accounts_chart_cannot_be_exported_by_type(
    auth_client, second_auth_client
) -> None:
    patient = await create_patient(auth_client)

    resp = await second_auth_client.get(f"/api/v1/patients/{patient['id']}/export/Condition")

    assert resp.status_code == 404


async def test_the_per_type_export_shares_the_whole_chart_ceiling(monkeypatch, auth_client) -> None:
    """Six types in six requests must not buy six times the bulk-retrieval rate."""
    monkeypatch.setattr("app.config.settings.rate_limit_exports_per_hour", 1)
    patient = await create_patient(auth_client)

    first = await auth_client.get(f"/api/v1/patients/{patient['id']}/export")
    second = await auth_client.get(f"/api/v1/patients/{patient['id']}/export/Condition")

    assert first.status_code == 200
    assert second.status_code == 429
