"""Where the exported bundle stopped being valid FHIR, or said something it did not mean.

An export is only worth writing if something else can read it, so a resource a strict receiver
rejects is not a partial success — that patient's diagnosis does not arrive at all. Two of these
are that; the third is the same failure in reverse, where the file parsed cleanly and asserted
something the chart never said.

* **``Condition.clinicalStatus`` carried a code FHIR does not have.** The binding is *required*
  and its value set is active | recurrence | relapse | inactive | remission | resolved. Our own
  vocabulary has an "unknown", which was exported verbatim, and every unrecognised status fell
  through to the same invalid code.

* **An allergy of unknown standing was exported as ``inactive``.** Valid FHIR, and a clinical
  claim nobody made: an inactive allergy is one the patient no longer has, so the receiving EMR
  will not raise it the next time that drug is prescribed. The conservative reading of an
  unknown allergy is not that it has gone away (Critical Safety Rule #2).

* **Nothing referenced the visits.** Once encounters were exported they sat in the bundle as
  orphans — the file said the patient attended A&E on a date, and separately that they are on a
  drug, and joined the two nowhere.

The last of those brings its own hazard, and it is pinned below: a reference must never point
outside the file it is in. Sections are read under a per-section ceiling, so the encounter a
finding was recorded at can be truncated away while the finding itself is exported.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.encounter import Encounter
from app.models.lab_result import LabResult
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account

PREFIX = "/api/v1"

# The value sets FHIR R4 binds these elements to, restated here on purpose. Reading them off the
# export's own mapping tables would make this test agree with whatever the code does; the point
# is to agree with the specification.
FHIR_CONDITION_CLINICAL = {"active", "recurrence", "relapse", "inactive", "remission", "resolved"}
FHIR_ALLERGY_CLINICAL = {"active", "inactive", "resolved"}
FHIR_ALLERGY_VERIFICATION = {"unconfirmed", "confirmed", "refuted", "entered-in-error"}
FHIR_ENCOUNTER_CLASS = {"AMB", "IMP", "EMER", "VR", "ACUTE", "NONAC", "OBSENC", "PRENC", "SS", "HH"}


def _resources(bundle: dict, resource_type: str) -> list[dict]:
    return [
        entry["resource"]
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == resource_type
    ]


def _codes(resource: dict, element: str) -> set[str]:
    return {coding["code"] for coding in resource.get(element, {}).get("coding", [])}


async def _export(auth_client, patient_id) -> dict:
    resp = await auth_client.get(f"{PREFIX}/patients/{patient_id}/export")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest_asyncio.fixture
async def auth_account(db, auth_client) -> Account:
    return (
        await db.execute(select(Account).where(Account.email == "doc@example.com"))
    ).scalar_one()


@pytest_asyncio.fixture
async def charted(db, auth_account) -> Patient:
    """A chart whose statuses are the ones with no FHIR equivalent, recorded at a visit."""
    patient = Patient(account_id=auth_account.id, full_name="Meera Nair", consent_given=True)
    db.add(patient)
    await db.flush()

    visit = Encounter(
        patient_id=patient.id,
        encounter_date=date(2026, 4, 18),
        encounter_type="emergency",
        presenting_complaint="Rash after starting a new antibiotic",
    )
    db.add(visit)
    await db.flush()

    db.add_all(
        [
            Condition(
                patient_id=patient.id,
                encounter_id=visit.id,
                condition_name="Chronic kidney disease",
                status="unknown",
                clinician_confirmed=True,
            ),
            Condition(
                patient_id=patient.id,
                encounter_id=visit.id,
                condition_name="Type 2 diabetes mellitus",
                status="active",
                clinician_confirmed=True,
            ),
            Allergy(
                patient_id=patient.id,
                encounter_id=visit.id,
                allergen_name="Sulfamethoxazole",
                allergen_type="drug",
                status="unknown",
                severity="severe",
            ),
            MedicationEvent(
                patient_id=patient.id,
                encounter_id=visit.id,
                brand_name_raw="Septran",
                generic_name="Cotrimoxazole",
                event_type="stop",
                event_date=date(2026, 4, 18),
                is_current=False,
                clinician_confirmed=True,
            ),
            LabResult(
                patient_id=patient.id,
                encounter_id=visit.id,
                marker_name="Creatinine",
                value_numeric=Decimal("2.4"),
                unit="mg/dL",
                sample_date=datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
                clinician_confirmed=True,
            ),
        ]
    )
    await db.commit()
    return patient


# --- Codes a receiver will actually accept -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_condition_of_unknown_status_carries_no_clinical_status(auth_client, charted):
    """Rather than the ``unknown`` it used to send, which is not in FHIR's required value set —
    so a validating receiver rejected the resource and the diagnosis never arrived at all.

    Omitting is what the element's 0..1 cardinality is for, and it is the honest reading: the
    chart does not know, so the file does not say. Every guess available is a claim — "active"
    asserts a diagnosis nobody made.
    """
    conditions = {
        c["code"]["text"]: c
        for c in _resources(await _export(auth_client, charted.id), "Condition")
    }

    assert "clinicalStatus" not in conditions["Chronic kidney disease"]
    # The one status we do know is still stated: this is not a blanket removal.
    assert _codes(conditions["Type 2 diabetes mellitus"], "clinicalStatus") == {"active"}


@pytest.mark.asyncio
async def test_an_allergy_of_unknown_status_is_not_exported_as_inactive(auth_client, charted):
    """The safety-relevant half. An inactive allergy is one the patient no longer has, and a
    receiving EMR will not raise it when that drug is next prescribed — so a status we never
    knew was being turned into a reassurance nobody gave. Absent, and left "unconfirmed", it
    stays a live caution.
    """
    (allergy,) = _resources(await _export(auth_client, charted.id), "AllergyIntolerance")

    assert "clinicalStatus" not in allergy
    assert _codes(allergy, "verificationStatus") == {"unconfirmed"}
    # And it is still an allergy worth stopping for.
    assert allergy["criticality"] == "high"


@pytest.mark.asyncio
async def test_every_status_our_chart_can_hold_exports_a_code_fhir_defines(
    db, auth_client, auth_account
):
    """The general form, over the whole vocabulary rather than the one value that was wrong.

    Read out of the models' ``CHECK`` constraints, so a status added to the chart later is
    covered here the day it is added rather than the day someone remembers this file.
    """
    from app.services.graph_service import _ENUM_VALUES

    for status in sorted(_ENUM_VALUES["Condition"]["status"]):
        patient = Patient(account_id=auth_account.id, full_name=f"C {status}", consent_given=True)
        db.add(patient)
        await db.flush()
        db.add(Condition(patient_id=patient.id, condition_name="Probe", status=status))
        await db.commit()

        (condition,) = _resources(await _export(auth_client, patient.id), "Condition")
        assert _codes(condition, "clinicalStatus") <= FHIR_CONDITION_CLINICAL, (
            f"condition status {status!r} exported a code FHIR does not define"
        )

    for status in sorted(_ENUM_VALUES["Allergy"]["status"]):
        patient = Patient(account_id=auth_account.id, full_name=f"A {status}", consent_given=True)
        db.add(patient)
        await db.flush()
        db.add(
            Allergy(
                patient_id=patient.id,
                allergen_name="Probe",
                allergen_type="drug",
                status=status,
            )
        )
        await db.commit()

        (allergy,) = _resources(await _export(auth_client, patient.id), "AllergyIntolerance")
        assert _codes(allergy, "clinicalStatus") <= FHIR_ALLERGY_CLINICAL, (
            f"allergy status {status!r} exported a code FHIR does not define"
        )
        assert _codes(allergy, "verificationStatus") <= FHIR_ALLERGY_VERIFICATION


@pytest.mark.asyncio
async def test_every_visit_type_our_chart_can_hold_exports_a_v3_act_code(
    db, auth_client, auth_account
):
    """``Encounter.class`` is 1..1 and required-bound, so an unmapped visit type is not a missing
    element — it is a resource that does not validate."""
    from app.services.graph_service import _ENUM_VALUES

    types: list[str | None] = [*sorted(_ENUM_VALUES["Encounter"]["encounter_type"]), None]
    for encounter_type in types:
        patient = Patient(
            account_id=auth_account.id, full_name=f"E {encounter_type}", consent_given=True
        )
        db.add(patient)
        await db.flush()
        db.add(
            Encounter(
                patient_id=patient.id,
                encounter_date=date(2026, 4, 18),
                encounter_type=encounter_type,
            )
        )
        await db.commit()

        (encounter,) = _resources(await _export(auth_client, patient.id), "Encounter")
        assert encounter["class"]["code"] in FHIR_ENCOUNTER_CLASS, (
            f"visit type {encounter_type!r} exported class {encounter['class']['code']!r}"
        )


# --- The findings are joined to the visit ------------------------------------------------------


@pytest.mark.asyncio
async def test_each_finding_references_the_visit_it_was_recorded_at(auth_client, charted):
    """Without this the encounters are orphans and the bundle is a pile of resources rather than
    a chart. Which drug was stopped at which visit is most of what a referral is for.

    Note ``context`` on MedicationStatement: R4 does not call it ``encounter`` there, because
    the element takes an EpisodeOfCare too.
    """
    bundle = await _export(auth_client, charted.id)
    (visit,) = _resources(bundle, "Encounter")
    expected = {"reference": f"urn:uuid:{visit['id']}"}

    assert all(c["encounter"] == expected for c in _resources(bundle, "Condition"))
    assert all(a["encounter"] == expected for a in _resources(bundle, "AllergyIntolerance"))
    assert all(m["context"] == expected for m in _resources(bundle, "MedicationStatement"))
    assert all(
        o["encounter"] == expected
        for o in _resources(bundle, "Observation")
        if o["category"][0]["coding"][0]["code"] == "laboratory"
    )


@pytest.mark.asyncio
async def test_no_reference_in_the_bundle_points_outside_it(auth_client, charted):
    """The property that makes the links safe to add at all. Every ``urn:uuid:`` a resource
    names must be the ``fullUrl`` of an entry in the same file — an importer given a dangling
    one either refuses the bundle or resolves it against whatever else happens to hold that id.
    """
    bundle = await _export(auth_client, charted.id)
    present = {entry["fullUrl"] for entry in bundle["entry"]}

    def _references(node: object) -> list[str]:
        if isinstance(node, dict):
            found = [node["reference"]] if isinstance(node.get("reference"), str) else []
            return found + [r for value in node.values() for r in _references(value)]
        if isinstance(node, list):
            return [r for item in node for r in _references(item)]
        return []

    dangling = [ref for ref in _references(bundle) if ref not in present]
    assert not dangling, f"bundle references resources it does not contain: {dangling}"


@pytest.mark.asyncio
async def test_a_visit_truncated_by_the_ceiling_is_not_referenced(
    db, auth_client, auth_account, monkeypatch
):
    """The case that makes the presence check load-bearing rather than decorative. Sections are
    read under a per-section ceiling, so a chart with more visits than the ceiling exports the
    finding and not the visit it names — and a reference written unconditionally would point at
    a resource this file does not contain.
    """
    monkeypatch.setattr(settings, "export_max_rows_per_section", 1)
    patient = Patient(account_id=auth_account.id, full_name="Truncated", consent_given=True)
    db.add(patient)
    await db.flush()

    visits = [
        Encounter(
            patient_id=patient.id, encounter_date=date(2026, 1, day), encounter_type="follow_up"
        )
        for day in (5, 12)
    ]
    db.add_all(visits)
    await db.flush()
    # Attached to the *older* visit, which sorts second and is the one the ceiling drops.
    db.add(
        Condition(
            patient_id=patient.id,
            encounter_id=visits[0].id,
            condition_name="Hypertension",
            status="active",
        )
    )
    await db.commit()

    bundle = await _export(auth_client, patient.id)
    exported_visits = _resources(bundle, "Encounter")
    (condition,) = _resources(bundle, "Condition")

    assert len(exported_visits) == 1
    assert exported_visits[0]["id"] != str(visits[0].id)
    assert "encounter" not in condition
    # And the file says it is short, which is what makes the missing link readable rather than
    # merely absent.
    outcomes = _resources(bundle, "OperationOutcome")
    assert any("Encounter" in o["issue"][0]["diagnostics"] for o in outcomes)


@pytest.mark.asyncio
async def test_a_finding_recorded_at_no_visit_carries_no_encounter_element(
    db, auth_client, auth_account
):
    """Most of the record predates encounters being charted at all, and an absent link has to
    stay absent rather than becoming an empty or null element — FHIR forbids the latter."""
    patient = Patient(account_id=auth_account.id, full_name="Unlinked", consent_given=True)
    db.add(patient)
    await db.flush()
    db.add(Condition(patient_id=patient.id, condition_name="Asthma", status="active"))
    await db.commit()

    (condition,) = _resources(await _export(auth_client, patient.id), "Condition")
    assert "encounter" not in condition
