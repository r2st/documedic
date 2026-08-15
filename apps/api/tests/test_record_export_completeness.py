"""What the FHIR export was leaving behind.

Three gaps, all of the same kind: the bundle was a correct FHIR file that did not carry
everything the chart knew, and nothing in it said so. A short export that admits it is handled
— that is what the ``OperationOutcome`` is for — but a *silently* partial one is read at the far
end as the patient's whole record, because that is what the file is called.

* **Encounters were not exported at all.** Every other section is findings — drugs, results,
  diagnoses — and the visits they were recorded at were dropped. A receiving clinician got a
  chart with no consultations in it: no date the patient was seen, no reason they came, none of
  what was written at the time. Two prescriptions a year apart, and the fact that the second
  was at an emergency presentation, are different clinical pictures.

* **A derived marker exported its number and nothing about how to read it.** Lab results carry
  a reference range and an H/L interpretation; markers carried neither, though the row holds
  both. An eGFR of 22 arrived as a bare quantity, and the resource that should stop a clinician
  before prescribing metformin looked exactly like a normal one.

* **A derived marker did not say what it was derived from.** The creatinine behind an eGFR was
  in the same bundle and unfindable from it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.models.derived_marker import DerivedMarker
from app.models.encounter import Encounter
from app.models.lab_result import LabResult
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
    extensions = [e for e in resource.get("extension", []) if e["url"] == PROVENANCE_EXTENSION]
    assert extensions, f"{resource['resourceType']} carries no provenance extension"
    return {
        part["url"]: part.get("valueBoolean", part.get("valueString"))
        for part in extensions[0]["extension"]
    }


@pytest_asyncio.fixture
async def auth_account(db, auth_client) -> Account:
    return (
        await db.execute(select(Account).where(Account.email == "doc@example.com"))
    ).scalar_one()


@pytest_asyncio.fixture
async def seen(db, auth_account):
    """A chart with visits on it, and an eGFR computed from a creatinine that is also charted."""
    patient = Patient(account_id=auth_account.id, full_name="Ravi Kulkarni", consent_given=True)
    db.add(patient)
    await db.flush()

    creatinine = LabResult(
        patient_id=patient.id,
        marker_name="Creatinine",
        value_numeric=Decimal("2.1"),
        unit="mg/dL",
        sample_date=datetime(2026, 5, 2, 8, 0, tzinfo=UTC),
        clinician_confirmed=True,
    )
    db.add(creatinine)
    await db.flush()

    db.add_all(
        [
            Encounter(
                patient_id=patient.id,
                encounter_date=date(2026, 5, 2),
                encounter_type="emergency",
                presenting_complaint="Breathlessness on exertion for three days",
                clinician_notes="Bibasal crepitations. For urgent review of renal function.",
            ),
            Encounter(
                patient_id=patient.id,
                encounter_date=date(2026, 1, 12),
                encounter_type="follow_up",
            ),
            DerivedMarker(
                patient_id=patient.id,
                source_lab_result_id=creatinine.id,
                marker_name="eGFR",
                value_numeric=Decimal("28.4"),
                unit="mL/min/1.73m2",
                reference_range_low=Decimal("90"),
                is_abnormal=True,
                formula_name="ckd_epi_2021",
                formula_version="v1",
                input_values={"creatinine": "2.1"},
                computed_at=datetime(2026, 5, 2, 9, 0, tzinfo=UTC),
            ),
        ]
    )
    await db.commit()
    return patient


async def _export(auth_client, patient_id) -> dict:
    resp = await auth_client.get(f"{PREFIX}/patients/{patient_id}/export")
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- Encounters -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_visits_are_in_the_bundle(auth_client, seen):
    """The section that was missing entirely. A referral carrying drugs and results but no
    record that the patient was ever seen is not the patient's record."""
    encounters = _resources(await _export(auth_client, seen.id), "Encounter")
    assert len(encounters) == 2
    assert {e["period"]["start"] for e in encounters} == {"2026-05-02", "2026-01-12"}


@pytest.mark.asyncio
async def test_an_emergency_presentation_is_not_flattened_into_an_ordinary_visit(auth_client, seen):
    """The visit *class* is the difference between "came to clinic" and "came to A&E", and it
    changes how everything recorded at that visit should be read."""
    encounters = {
        e["period"]["start"]: e
        for e in _resources(await _export(auth_client, seen.id), "Encounter")
    }
    assert encounters["2026-05-02"]["class"]["code"] == "EMER"
    assert encounters["2026-01-12"]["class"]["code"] == "AMB"


@pytest.mark.asyncio
async def test_an_unknown_visit_type_is_exported_as_ambulatory_not_as_an_emergency(
    db, auth_account
):
    """A type this map does not recognise falls to the reading with the fewest consequences.
    Guessing upward would put an emergency presentation on a chart that never had one."""
    patient = Patient(account_id=auth_account.id, full_name="Unknown Visit", consent_given=True)
    db.add(patient)
    await db.flush()
    db.add(Encounter(patient_id=patient.id, encounter_date=date(2026, 3, 3), encounter_type=None))
    await db.commit()

    encounter = _resources(await PatientExportService(db).build_bundle(patient), "Encounter")[0]
    assert encounter["class"]["code"] == "AMB"
    assert "type" not in encounter


@pytest.mark.asyncio
async def test_the_reason_for_the_visit_travels_with_it(auth_client, seen):
    encounters = {
        e["period"]["start"]: e
        for e in _resources(await _export(auth_client, seen.id), "Encounter")
    }
    assert (
        encounters["2026-05-02"]["reasonCode"][0]["text"]
        == "Breathlessness on exertion for three days"
    )


@pytest.mark.asyncio
async def test_what_the_clinician_wrote_at_the_visit_is_not_dropped(auth_client, seen):
    """R4 gives ``Encounter`` no ``note`` element, so the notes go in the narrative rather than
    nowhere. Free text written at the bedside is often the only thing that explains the rest of
    a referral."""
    encounters = {
        e["period"]["start"]: e
        for e in _resources(await _export(auth_client, seen.id), "Encounter")
    }
    narrative = encounters["2026-05-02"]["text"]
    assert narrative["status"] == "additional"
    assert "Bibasal crepitations" in narrative["div"]
    # A visit with no notes carries no empty narrative element.
    assert "text" not in encounters["2026-01-12"]


@pytest.mark.asyncio
async def test_clinician_notes_are_escaped_into_the_narrative(db, auth_account):
    """The div is XHTML inside a JSON string. An angle bracket in a notes field, unescaped,
    produces a bundle that is not parseable as FHIR at the far end."""
    patient = Patient(account_id=auth_account.id, full_name="Bracket Notes", consent_given=True)
    db.add(patient)
    await db.flush()
    db.add(
        Encounter(
            patient_id=patient.id,
            encounter_date=date(2026, 2, 2),
            clinician_notes="BP <90 systolic & tachycardic",
        )
    )
    await db.commit()

    encounter = _resources(await PatientExportService(db).build_bundle(patient), "Encounter")[0]
    assert "&lt;90" in encounter["text"]["div"]
    assert "&amp;" in encounter["text"]["div"]
    assert "<90" not in encounter["text"]["div"]


@pytest.mark.asyncio
async def test_an_encounter_says_it_is_unconfirmed_rather_than_saying_nothing(auth_client, seen):
    """Nothing on this row records a clinician confirming it, and the bundle's contract is that
    every clinical resource answers that question. Omitting the extension would read as "this
    resource carries no provenance", which is indistinguishable from an oversight."""
    encounter = _resources(await _export(auth_client, seen.id), "Encounter")[0]
    assert _provenance(encounter)["clinician-confirmed"] is False


@pytest.mark.asyncio
async def test_a_truncated_encounter_section_says_so_in_the_bundle(db, auth_account, monkeypatch):
    """The new section is under the same ceiling as every other, and admits hitting it."""
    patient = Patient(account_id=auth_account.id, full_name="Many Visits", consent_given=True)
    db.add(patient)
    await db.flush()
    db.add_all(
        [Encounter(patient_id=patient.id, encounter_date=date(2026, 1, day)) for day in range(1, 6)]
    )
    await db.commit()
    monkeypatch.setattr(settings, "export_max_rows_per_section", 2)

    bundle = await PatientExportService(db).build_bundle(patient)
    assert len(_resources(bundle, "Encounter")) == 2
    diagnostics = _resources(bundle, "OperationOutcome")[0]["issue"][0]["diagnostics"]
    assert "Encounter" in diagnostics


@pytest.mark.asyncio
async def test_a_withdrawn_visit_is_not_exported(db, auth_client, seen):
    """Soft-deleted rows are out of the export, as in every other section."""
    encounter = (
        await db.execute(select(Encounter).where(Encounter.patient_id == seen.id).limit(1))
    ).scalar_one()
    encounter.is_deleted = True
    await db.commit()

    assert len(_resources(await _export(auth_client, seen.id), "Encounter")) == 1


# --- Derived markers --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_derived_marker_carries_the_range_it_should_be_read_against(auth_client, seen):
    """It was exporting the number and nothing about how to read it, while the lab result beside
    it carried both. A receiving system with no CKD-EPI range of its own had nothing to compare
    an eGFR of 28 to."""
    egfr = next(
        o
        for o in _resources(await _export(auth_client, seen.id), "Observation")
        if o["code"]["text"] == "eGFR"
    )
    assert egfr["referenceRange"][0]["low"]["value"] == 90.0
    assert egfr["referenceRange"][0]["low"]["unit"] == "mL/min/1.73m2"


@pytest.mark.asyncio
async def test_an_abnormal_marker_is_exported_as_abnormal(auth_client, seen):
    """``is_abnormal`` is the row's own verdict and it was being dropped on the floor, so the
    resource that should stop a clinician before prescribing looked like a normal one."""
    egfr = next(
        o
        for o in _resources(await _export(auth_client, seen.id), "Observation")
        if o["code"]["text"] == "eGFR"
    )
    assert egfr["interpretation"][0]["coding"][0]["code"] == "A"


@pytest.mark.asyncio
async def test_a_normal_marker_is_not_exported_as_abnormal(db, auth_account):
    patient = Patient(account_id=auth_account.id, full_name="Normal Marker", consent_given=True)
    db.add(patient)
    await db.flush()
    db.add(
        DerivedMarker(
            patient_id=patient.id,
            marker_name="BMI",
            value_numeric=Decimal("22.4"),
            unit="kg/m2",
            is_abnormal=False,
            formula_name="bmi",
            formula_version="v1",
            input_values={},
            computed_at=datetime(2026, 5, 2, 9, 0, tzinfo=UTC),
        )
    )
    await db.commit()

    marker = _resources(await PatientExportService(db).build_bundle(patient), "Observation")[0]
    assert marker["interpretation"][0]["coding"][0]["code"] == "N"


@pytest.mark.asyncio
async def test_a_marker_with_no_verdict_claims_neither(db, auth_account):
    """``is_abnormal`` is nullable. "We did not decide" must not export as "normal"."""
    patient = Patient(account_id=auth_account.id, full_name="No Verdict", consent_given=True)
    db.add(patient)
    await db.flush()
    db.add(
        DerivedMarker(
            patient_id=patient.id,
            marker_name="Body surface area",
            value_numeric=Decimal("1.7"),
            unit="m2",
            formula_name="dubois",
            formula_version="v1",
            input_values={},
            computed_at=datetime(2026, 5, 2, 9, 0, tzinfo=UTC),
        )
    )
    await db.commit()

    marker = _resources(await PatientExportService(db).build_bundle(patient), "Observation")[0]
    assert "interpretation" not in marker


@pytest.mark.asyncio
async def test_a_marker_points_at_the_result_it_was_computed_from(auth_client, seen):
    """The creatinine behind the eGFR is in the same bundle; without this it was unfindable
    from the marker that depends on it."""
    bundle = await _export(auth_client, seen.id)
    egfr = next(o for o in _resources(bundle, "Observation") if o["code"]["text"] == "eGFR")
    creatinine = next(
        o for o in _resources(bundle, "Observation") if o["code"]["text"] == "Creatinine"
    )
    assert egfr["derivedFrom"] == [{"reference": f"urn:uuid:{creatinine['id']}"}]
    # And the reference resolves inside this bundle rather than pointing out of it.
    assert any(entry["fullUrl"] == f"urn:uuid:{creatinine['id']}" for entry in bundle["entry"])


@pytest.mark.asyncio
async def test_a_marker_does_not_point_at_a_result_the_export_left_out(
    db, auth_account, monkeypatch
):
    """A dangling reference is worse than an absent one: the importer either rejects the bundle
    or resolves the id against whatever else it happens to hold."""
    patient = Patient(account_id=auth_account.id, full_name="Truncated Labs", consent_given=True)
    db.add(patient)
    await db.flush()
    creatinine = LabResult(
        patient_id=patient.id,
        marker_name="Creatinine",
        value_numeric=Decimal("2.1"),
        sample_date=datetime(2020, 1, 1, tzinfo=UTC),
    )
    db.add_all(
        [
            creatinine,
            LabResult(
                patient_id=patient.id,
                marker_name="Sodium",
                value_numeric=Decimal("138"),
                sample_date=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ]
    )
    await db.flush()
    db.add(
        DerivedMarker(
            patient_id=patient.id,
            source_lab_result_id=creatinine.id,
            marker_name="eGFR",
            value_numeric=Decimal("28.4"),
            formula_name="ckd_epi_2021",
            formula_version="v1",
            input_values={},
            computed_at=datetime(2026, 5, 2, 9, 0, tzinfo=UTC),
        )
    )
    await db.commit()
    # One lab per section: the newest survives, so the creatinine the marker points at does not.
    monkeypatch.setattr(settings, "export_max_rows_per_section", 1)

    bundle = await PatientExportService(db).build_bundle(patient)
    egfr = next(o for o in _resources(bundle, "Observation") if o["code"]["text"] == "eGFR")
    assert "derivedFrom" not in egfr


@pytest.mark.asyncio
async def test_a_derived_marker_says_no_clinician_confirmed_it(auth_client, seen):
    """It was computed here, from data that was extracted. The bundle's contract is that every
    clinical resource answers "did a human confirm this", and this one was silent."""
    egfr = next(
        o
        for o in _resources(await _export(auth_client, seen.id), "Observation")
        if o["code"]["text"] == "eGFR"
    )
    assert _provenance(egfr)["clinician-confirmed"] is False


@pytest.mark.asyncio
async def test_no_element_of_the_added_resources_is_null(db, auth_account):
    """FHIR forbids null-valued elements. Asserted recursively over the sparsest possible visit
    and marker, so a field added later without pruning fails here rather than at a hospital's
    ingest endpoint."""
    patient = Patient(account_id=auth_account.id, full_name="Sparse Visit", consent_given=True)
    db.add(patient)
    await db.flush()
    db.add(Encounter(patient_id=patient.id, encounter_date=date(2026, 6, 6)))
    db.add(
        DerivedMarker(
            patient_id=patient.id,
            marker_name="BMI",
            value_numeric=Decimal("22.4"),
            formula_name="bmi",
            formula_version="v1",
            input_values={},
            computed_at=datetime(2026, 6, 6, tzinfo=UTC),
        )
    )
    await db.commit()

    def assert_no_nulls(node, path="bundle"):
        if isinstance(node, dict):
            for key, value in node.items():
                assert value is not None, f"{path}.{key} is null"
                assert_no_nulls(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                assert value is not None, f"{path}[{index}] is null"
                assert_no_nulls(value, f"{path}[{index}]")

    assert_no_nulls(await PatientExportService(db).build_bundle(patient))
