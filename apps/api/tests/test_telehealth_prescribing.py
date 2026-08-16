"""``teleconsultation`` was an encounter type that changed nothing about the prescribing.

It has been an accepted ``encounters.encounter_type`` since the table existed, and the safety
engine had no idea an encounter existed at all: a drug started on a telephone call was evaluated
by exactly the checks a drug handed over in clinic was. Every rule in this engine is a statement
about the drug (an interaction, a contraindication, a ceiling); none of them was a statement
about what the *consultation* could and could not supply.

Three things it cannot, and each is a check in ``app.core.telehealth``:

* an injection cannot be administered down a telephone line — ceftriaxone is first-line and
  entirely correct to prescribe, and the remote consultation still cannot end with the dose
  given;
* a baseline the first dose is chosen from cannot be in hand at the moment the prescription is
  written — warfarin from an INR, lithium from renal and thyroid function, digoxin from
  potassium, phenytoin and methotrexate from liver function;
* on an audio-only call the prescriber has not seen the patient, which is where India's
  Telemedicine Practice Guidelines draw their line between a video consultation and a telephone
  one.

Two design claims this file pins as hard as the findings themselves.

**Initiation, not continuation.** Every check fires only on a drug the patient is not already
on. Continuing warfarin for someone who has taken it for three years is the ordinary, intended
use of a teleconsultation; a check that flagged it would fire on every follow-up call in the
country and be trained away within a week. The tests cover the continuation case from both
directions the vocabulary allows — the same row, and the same molecule at another strength.

**Nothing here is a hard block.** Refusing to let a clinician prescribe remotely strands a
patient whose only access to a prescriber is the call they are on, which in this market is the
ordinary case. Hard blocks remain what Critical Safety Rule #3 says they are: a documented
allergy and an absolute contraindication.

The last section is the one that caught a real defect: the three type names are persisted in
``drug_safety_checks.check_type``, which was ``VARCHAR(30)``, and the longest of them is 39
characters. SQLite does not enforce a VARCHAR length, so the entire suite was green over a
column that would have raised on PostgreSQL the first time a clinician ran the check.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import get_args

import pytest
from sqlalchemy import select

from app.core.safety import CheckType, DrugRef, SafetyContext
from app.core.telehealth import check_teleconsultation_prescribing
from app.models.drug_safety_check import DrugSafetyCheck
from app.models.drug_vocabulary import DrugVocabulary
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService
from tests.conftest import create_patient

CEFTRIAXONE = DrugRef(reference_id="CTX-1G", generic_name="Ceftriaxone", drug_class="Cephalosporin")
WARFARIN = DrugRef(reference_id="WARF-5", generic_name="Warfarin", drug_class="Anticoagulant")
LITHIUM = DrugRef(reference_id="LIT-400", generic_name="Lithium carbonate", drug_class="Mood")
AMLODIPINE = DrugRef(reference_id="AML-5", generic_name="Amlodipine", drug_class="CCB")

# The same molecule at another strength: one vocabulary row per formulation is how this corpus is
# built, so a patient stable on warfarin is not necessarily carrying ``WARF-5``.
WARFARIN_3MG = DrugRef(reference_id="WARF-3", generic_name="Warfarin", drug_class="Anticoagulant")

# A fixed-dose combination carrying a curated component. Constructed here rather than taken from
# the corpus because what is under test is the component walk, not this particular product.
COMBINATION = DrugRef(
    reference_id="COMBO-CTX-SUL",
    generic_name="Ceftriaxone + Sulbactam",
    drug_class="Cephalosporin",
    components=(
        CEFTRIAXONE,
        DrugRef(reference_id="SULB-1G", generic_name="Sulbactam", drug_class="Beta-lactamase"),
    ),
)

TELEHEALTH_TYPES = {
    "telehealth_in_person_required",
    "telehealth_baseline_monitoring_required",
    "telehealth_audio_only_initiation",
}


def _on(*drugs: DrugRef) -> SafetyContext:
    return SafetyContext(current_meds=list(drugs))


def _types(drug: DrugRef, ctx: SafetyContext, modality) -> set[str]:
    return {f.check_type for f in check_teleconsultation_prescribing(drug, ctx, modality)}


# --- The format has to be stated before anything fires ------------------------------------------


@pytest.mark.parametrize("modality", [None, "in_person"])
@pytest.mark.parametrize("drug", [CEFTRIAXONE, WARFARIN, AMLODIPINE])
def test_an_in_clinic_visit_and_an_unstated_one_raise_nothing(drug, modality):
    """``None`` is treated as ``in_person`` deliberately, and it is the case that matters.

    Every existing caller of the safety check omits the modality — the field was added with this
    feature. Inventing a restriction from silence would have put a format-dependent flag under
    every in-clinic prescription in the system on the day this shipped.
    """
    assert check_teleconsultation_prescribing(drug, _on(), modality) == []


# --- What a remote consultation cannot do -------------------------------------------------------


def test_a_drug_that_has_to_be_injected_is_flagged_on_a_video_call():
    flags = check_teleconsultation_prescribing(CEFTRIAXONE, _on(), "video")
    assert [f.check_type for f in flags] == ["telehealth_in_person_required"]
    assert "Ceftriaxone" in flags[0].summary
    assert flags[0].details["modality"] == "video"


@pytest.mark.parametrize("drug", [WARFARIN, LITHIUM])
def test_a_drug_dosed_from_a_baseline_is_flagged_when_it_is_started_remotely(drug):
    """The measurement, not the drug, is the finding: a remote consultation can order the test,
    it just cannot have the result at the moment the prescription is written."""
    flags = check_teleconsultation_prescribing(drug, _on(), "video")
    assert [f.check_type for f in flags] == ["telehealth_baseline_monitoring_required"]
    assert drug.generic_name in flags[0].summary


def test_an_audio_call_flags_any_new_medicine_even_one_no_list_names():
    """Amlodipine is on no curated list here, and starting it sight-unseen is still the thing
    the guidelines distinguish a telephone call from a video one over.

    Deliberately not a curated list of its own: a list would be a claim about which new drugs
    are safe to start without seeing the patient, which this module is in no position to make.
    What it can say accurately is that nothing was seen.
    """
    flags = check_teleconsultation_prescribing(AMLODIPINE, _on(), "audio")
    assert [f.check_type for f in flags] == ["telehealth_audio_only_initiation"]
    assert flags[0].severity == "warning"


def test_the_drug_rules_and_the_modality_rule_stack():
    """Starting warfarin on a telephone call is two separate absences, and the clinician is
    shown both rather than whichever one the code happened to reach first."""
    assert _types(WARFARIN, _on(), "audio") == {
        "telehealth_baseline_monitoring_required",
        "telehealth_audio_only_initiation",
    }


def test_an_uncurated_drug_on_a_video_call_raises_nothing():
    """The video path has to stay quiet for the ordinary prescription, or the two findings it
    does carry are read as noise."""
    assert check_teleconsultation_prescribing(AMLODIPINE, _on(), "video") == []


# --- Continuation is the ordinary use of a teleconsultation -------------------------------------


@pytest.mark.parametrize("modality", ["video", "audio"])
def test_a_drug_the_patient_is_already_on_raises_nothing(modality):
    """This is the whole design. A patient on warfarin for three years, calling for a repeat, is
    what teleconsultation exists for; a flag here would fire on every follow-up call."""
    assert check_teleconsultation_prescribing(WARFARIN, _on(WARFARIN), modality) == []


@pytest.mark.parametrize("modality", ["video", "audio"])
def test_the_same_molecule_at_another_strength_still_counts_as_continuation(modality):
    """One molecule has several vocabulary rows. Matched on reference id alone, a warfarin dose
    change — the single commonest reason such a patient calls — would read as an initiation."""
    assert check_teleconsultation_prescribing(WARFARIN, _on(WARFARIN_3MG), modality) == []


def test_a_different_drug_of_the_same_class_is_not_continuation():
    """Continuation is this drug, not this kind of drug. Lithium is not warfarin."""
    assert _types(WARFARIN, _on(LITHIUM), "video") == {"telehealth_baseline_monitoring_required"}


# --- Combination products -----------------------------------------------------------------------


def test_a_combination_is_matched_through_its_components():
    """Every curated rule in this engine is keyed on a molecule and a product row is keyed on a
    formulation; comparing the two directly is how a curated rule finds nothing."""
    flags = check_teleconsultation_prescribing(COMBINATION, _on(), "video")
    assert [f.check_type for f in flags] == ["telehealth_in_person_required"]
    assert flags[0].details["component"] == "Ceftriaxone"
    assert flags[0].details["component_reference_id"] == "CTX-1G"


def test_the_warning_names_the_product_the_clinician_is_prescribing():
    """A warning headed "Ceftriaxone" on a prescription that says "Ceftriaxone + Sulbactam"
    reads as being about a different drug."""
    summary = check_teleconsultation_prescribing(COMBINATION, _on(), "video")[0].summary
    assert summary.startswith("Ceftriaxone + Sulbactam")
    assert "via its Ceftriaxone component" in summary


def test_a_combination_whose_component_is_charted_is_a_continuation():
    assert check_teleconsultation_prescribing(COMBINATION, _on(CEFTRIAXONE), "video") == []


# --- None of it is a refusal --------------------------------------------------------------------


@pytest.mark.parametrize("drug", [CEFTRIAXONE, WARFARIN, LITHIUM, AMLODIPINE, COMBINATION])
@pytest.mark.parametrize("modality", ["video", "audio"])
def test_no_telemedicine_finding_is_ever_a_hard_block(drug, modality):
    """A hard block is a refusal, and refusing here strands the patient on the call. Rule #3's
    two hard blocks — a documented allergy, an absolute contraindication — are unaffected."""
    for flag in check_teleconsultation_prescribing(drug, _on(), modality):
        assert flag.is_hard_block is False


@pytest.mark.parametrize("drug", [CEFTRIAXONE, WARFARIN, AMLODIPINE])
@pytest.mark.parametrize("modality", ["video", "audio"])
def test_the_wording_says_what_the_format_cannot_supply_never_what_to_do(drug, modality):
    """Rule #4. These sentences describe the consultation, not the clinician's next move."""
    imperatives = ("give ", "administer ", "prescribe ", "switch ", "arrange ", "do not ")
    for flag in check_teleconsultation_prescribing(drug, _on(), modality):
        lowered = flag.summary.lower()
        assert not any(lowered.startswith(word) for word in imperatives), flag.summary


# --- The column and the constraint the findings are stored in -----------------------------------


def test_every_check_type_fits_the_column_it_is_persisted_in():
    """The defect this file was written after.

    ``telehealth_baseline_monitoring_required`` is 39 characters and the column was
    ``VARCHAR(30)``. SQLite ignores a VARCHAR length, so every test stored it happily and
    PostgreSQL would have raised ``value too long for type character varying(30)`` the first
    time a clinician ran a drug check on a teleconsultation. Measured rather than assumed from
    here on.
    """
    limit = DrugSafetyCheck.__table__.c.check_type.type.length
    too_long = [value for value in get_args(CheckType) if len(value) > limit]
    assert not too_long, (
        f"check types longer than the {limit}-character column they are written to: {too_long}"
    )


def test_every_check_type_is_one_the_audit_table_will_accept():
    """``ck_dsc_check_type`` lists the vocabulary literally, so a new type is a two-place change
    and the second place is easy to forget. Enumerated from the Literal, not restated."""
    constraint = next(c for c in DrugSafetyCheck.__table_args__ if c.name == "ck_dsc_check_type")
    predicate = str(constraint.sqltext)
    missing = [value for value in get_args(CheckType) if f"'{value}'" not in predicate]
    assert not missing, f"check types the audit table would refuse to store: {missing}"


# --- Through the service and over the wire ------------------------------------------------------


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"tele-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Remote Consultation Patient",
        sex="female",
        date_of_birth=date(1972, 3, 11),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


@pytest.mark.asyncio
async def test_the_service_runs_the_check_and_writes_the_finding_to_the_chart(db):
    account, patient = await _account_and_patient(db)

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Warfarin",
        modality="video",
    )

    assert "telehealth_baseline_monitoring_required" in {f.check_type for f in flags}
    stored = (
        (await db.execute(select(DrugSafetyCheck).where(DrugSafetyCheck.patient_id == patient.id)))
        .scalars()
        .all()
    )
    assert "telehealth_baseline_monitoring_required" in {row.check_type for row in stored}


@pytest.mark.asyncio
async def test_the_same_check_without_a_modality_raises_none_of_them(db):
    """Every caller that existed before this feature omits the field, and none of their results
    may change because of it."""
    account, patient = await _account_and_patient(db)

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Warfarin",
    )

    assert TELEHEALTH_TYPES & {f.check_type for f in flags} == set()


@pytest.mark.asyncio
async def test_a_repeat_of_a_charted_drug_over_video_raises_none_of_them(db):
    account, patient = await _account_and_patient(db)
    row = (
        (
            await db.execute(
                select(DrugVocabulary).where(DrugVocabulary.generic_name.ilike("Warfarin"))
            )
        )
        .scalars()
        .first()
    )
    assert row is not None, "seed data is missing Warfarin"
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            drug_vocabulary_id=row.id,
            generic_name=row.generic_name,
            event_type="continue",
            is_current=True,
        )
    )
    await db.flush()

    _vocab, _ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Warfarin",
        modality="video",
    )

    assert TELEHEALTH_TYPES & {f.check_type for f in flags} == set()


async def test_the_finding_reaches_the_safety_screen(auth_client):
    patient = await create_patient(auth_client, full_name="Video Consultation Patient")

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Warfarin", "modality": "audio"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert TELEHEALTH_TYPES <= {f["check_type"] for f in body["flags"]} or {
        f["check_type"] for f in body["flags"]
    } >= {"telehealth_baseline_monitoring_required", "telehealth_audio_only_initiation"}
    # Advisory, whatever else the chart raised about the drug itself.
    for flag in body["flags"]:
        if flag["check_type"] in TELEHEALTH_TYPES:
            assert flag["is_hard_block"] is False


async def test_an_unknown_modality_is_refused_at_the_boundary(auth_client):
    """The wire accepts three values. A typo silently disabling every telemedicine check is the
    fail-open shape this engine has been corrected for repeatedly."""
    patient = await create_patient(auth_client)

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Warfarin", "modality": "telephone"},
    )

    assert resp.status_code == 422, resp.text


# --- The encounter the modality is recorded on --------------------------------------------------


async def _open(auth_client, patient_id: str, **overrides) -> tuple[int, dict]:
    body = {
        "encounter_date": "2026-08-14",
        "encounter_type": "teleconsultation",
        "presenting_complaint": "Cough for four days",
    }
    body.update(overrides)
    resp = await auth_client.post(f"/api/v1/patients/{patient_id}/encounters", json=body)
    return resp.status_code, (resp.json() if resp.content else {})


async def test_a_teleconsultation_records_how_it_was_conducted(auth_client):
    patient = await create_patient(auth_client)

    status, body = await _open(auth_client, patient["id"], telehealth_modality="video")

    assert status == 201, body
    assert body["telehealth_modality"] == "video"


async def test_a_teleconsultation_without_a_modality_is_refused(auth_client):
    """A remote visit that does not say how it was conducted cannot be checked against the
    telemedicine rules at all — which is worse than the state before this feature, because the
    encounter now claims to have been checked."""
    patient = await create_patient(auth_client)

    status, _body = await _open(auth_client, patient["id"])

    assert status == 422


async def test_a_modality_on_an_in_clinic_visit_is_refused(auth_client):
    """A contradiction rather than extra information: the flags it would authorise are about
    absences this visit did not have."""
    patient = await create_patient(auth_client)

    status, _body = await _open(
        auth_client, patient["id"], encounter_type="outpatient", telehealth_modality="video"
    )

    assert status == 422


async def test_correcting_the_type_without_clearing_the_modality_is_refused(auth_client):
    """The half-edit: a PATCH that moves a visit to ``outpatient`` and leaves the modality
    behind would otherwise produce a row saying both things at once."""
    patient = await create_patient(auth_client)
    status, encounter = await _open(auth_client, patient["id"], telehealth_modality="audio")
    assert status == 201, encounter

    resp = await auth_client.patch(
        f"/api/v1/patients/{patient['id']}/encounters/{encounter['id']}",
        json={"encounter_type": "outpatient"},
    )

    assert resp.status_code == 422, resp.text


async def test_the_billing_code_is_recorded_as_typed(auth_client):
    """Free-form on purpose: the code set belongs to the payer and this product spans several,
    so validating against the wrong one refuses correct codes."""
    patient = await create_patient(auth_client)

    status, body = await _open(
        auth_client, patient["id"], telehealth_modality="video", billing_code="99213"
    )

    assert status == 201, body
    assert body["billing_code"] == "99213"
