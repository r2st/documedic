"""The dashboard aggregates, and above all what they are scoped to.

Every other read in this API is anchored to one chart, and the sweep in
``test_cross_account_resource_sweep.py`` holds that line by walking the ``{patient_id}`` in each
route's path. These routes have no path parameter at all — they aggregate across a whole account
— so that sweep cannot see them, and the scoping has to be asserted directly.

That is what most of this file is. An aggregate is the easiest place in an API to cross a tenancy
boundary without anyone noticing: a ``COUNT(*)`` missing a predicate returns a number rather than
an error, and a number that is silently the whole deployment's looks exactly like a number that
is correctly this clinic's. So every test below builds a *second* account holding data of the
same shape, and asserts the first account's numbers are unmoved by it. Reading the SQL and
agreeing that it looks scoped is precisely the check that does not survive a refactor.

The rest covers the two counting decisions the service makes that a reader could reasonably
expect to have gone the other way — ranking drugs by distinct patients rather than by rows, and
counting a withdrawn chart rather than hiding it — plus the SQLite/PostgreSQL boolean-sum trap
that ``safety_flags`` sidesteps with a CASE.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest

from app.models.drug_safety_check import DrugSafetyCheck
from app.models.drug_vocabulary import DrugVocabulary
from app.models.encounter import Encounter
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.dashboard_service import MAX_TOP_N, DashboardService

pytestmark = pytest.mark.asyncio


async def _account(db) -> Account:
    account = Account(email=f"dash-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    return account


async def _patient(db, account: Account, **fields) -> Patient:
    defaults = {
        "full_name": "Dashboard Patient",
        "sex": "female",
        "date_of_birth": date(1980, 3, 4),
        "consent_given": True,
        "consent_given_at": datetime.now(UTC),
    }
    defaults.update(fields)
    patient = Patient(account_id=account.id, **defaults)
    db.add(patient)
    await db.flush()
    return patient


async def _encounter(db, patient: Patient, **fields) -> Encounter:
    defaults = {"encounter_date": date(2026, 5, 1), "encounter_type": "outpatient"}
    defaults.update(fields)
    row = Encounter(patient_id=patient.id, **defaults)
    db.add(row)
    await db.flush()
    return row


async def _med(db, patient: Patient, **fields) -> MedicationEvent:
    defaults = {"event_type": "start", "is_current": True, "generic_name": "Paracetamol"}
    defaults.update(fields)
    row = MedicationEvent(patient_id=patient.id, **defaults)
    db.add(row)
    await db.flush()
    return row


async def _drug(db) -> DrugVocabulary:
    drug = DrugVocabulary(
        brand_name="Crocin",
        generic_name="Paracetamol",
        reference_id=f"ref-{uuid.uuid4().hex}",
    )
    db.add(drug)
    await db.flush()
    return drug


async def _flag(db, account: Account, patient: Patient, drug: DrugVocabulary, **fields):
    defaults = {
        "check_type": "drug_interaction",
        "severity": "warning",
        "is_hard_block": False,
        "summary": "A finding",
        "details": {},
    }
    defaults.update(fields)
    row = DrugSafetyCheck(
        patient_id=patient.id, account_id=account.id, drug_vocabulary_id=drug.id, **defaults
    )
    db.add(row)
    await db.flush()
    return row


async def _a_second_account_holding_everything(db) -> Account:
    """A neighbouring clinic with one of each row type, to be absent from every count.

    Deliberately the same shapes and the same drug name as the account under test: a scoping bug
    that grouped across accounts would merge these rows into the first account's buckets rather
    than adding a visibly foreign one, and a fixture using distinct names would let that pass.
    """
    other = await _account(db)
    patient = await _patient(db, other)
    drug = await _drug(db)
    await _encounter(
        db,
        patient,
        encounter_type="emergency",
        status="signed",
        signed_at=datetime.now(UTC),
        signed_by_account_id=other.id,
    )
    await _med(db, patient, generic_name="Paracetamol")
    await _flag(db, other, patient, drug, check_type="allergy_conflict")
    return other


# --- scoping: the claim the module docstring makes -------------------------------------------


async def test_patient_counts_exclude_another_accounts_charts(db) -> None:
    account = await _account(db)
    await _patient(db, account)
    await _a_second_account_holding_everything(db)

    counts = await DashboardService(db).patients(account.id)

    assert counts["total"] == 1
    assert counts["active"] == 1


async def test_encounter_counts_exclude_another_accounts_visits(db) -> None:
    """The neighbour's visit is an ``emergency``; a leak would add that bucket, not just a row."""
    account = await _account(db)
    patient = await _patient(db, account)
    await _encounter(db, patient, encounter_type="outpatient")
    await _a_second_account_holding_everything(db)

    counts = await DashboardService(db).encounters(account.id)

    assert counts["total"] == 1
    assert counts["by_type"] == {"outpatient": 1}


async def test_medication_counts_exclude_another_accounts_prescriptions(db) -> None:
    """Both accounts prescribe Paracetamol, so a leak merges rather than appends."""
    account = await _account(db)
    patient = await _patient(db, account)
    await _med(db, patient, generic_name="Paracetamol")
    await _a_second_account_holding_everything(db)

    counts = await DashboardService(db).medications(account.id)

    assert counts["most_prescribed"] == [
        {"drug": "Paracetamol", "patient_count": 1, "event_count": 1}
    ]


async def test_safety_flag_counts_exclude_another_accounts_findings(db) -> None:
    account = await _account(db)
    patient = await _patient(db, account)
    drug = await _drug(db)
    await _flag(db, account, patient, drug)
    await _a_second_account_holding_everything(db)

    counts = await DashboardService(db).safety_flags(account.id)

    assert counts["total"] == 1
    assert [row["check_type"] for row in counts["by_check_type"]] == ["drug_interaction"]


async def test_a_safety_check_row_whose_two_owners_disagree_is_counted_by_neither(db) -> None:
    """``drug_safety_checks`` carries its own ``account_id`` *and* a patient that has one.

    The service filters on both. A row whose account column says one clinic while its patient
    belongs to another is a row that cannot be attributed, and appearing in either account's
    numbers would be worse than appearing in neither — the point of the second predicate.
    """
    account = await _account(db)
    other = await _account(db)
    their_patient = await _patient(db, other)
    drug = await _drug(db)
    # Our account_id, their patient.
    await _flag(db, account, their_patient, drug)

    ours = await DashboardService(db).safety_flags(account.id)
    theirs = await DashboardService(db).safety_flags(other.id)

    assert ours["total"] == 0
    assert theirs["total"] == 0


async def test_the_overview_is_scoped_everywhere_its_tiles_are(db) -> None:
    """The combined route must not be a fifth query written to different rules."""
    account = await _account(db)
    patient = await _patient(db, account)
    await _encounter(db, patient)
    await _med(db, patient)
    await _a_second_account_holding_everything(db)

    overview = await DashboardService(db).overview(account.id)

    assert overview["patients"]["total"] == 1
    assert overview["encounters"]["total"] == 1
    assert overview["medications"]["most_prescribed"][0]["patient_count"] == 1
    assert overview["safety_flags"]["total"] == 0


# --- what a withdrawn or deleted row does to a count ------------------------------------------


async def test_a_withdrawn_chart_is_counted_as_withdrawn_rather_than_vanishing(db) -> None:
    """DPDP erasure is a soft delete here, and a total that silently shrank would report an
    erasure as an absence."""
    account = await _account(db)
    await _patient(db, account)
    await _patient(db, account, is_deleted=True, deleted_at=datetime.now(UTC))

    counts = await DashboardService(db).patients(account.id)

    assert counts["total"] == 2
    assert counts["active"] == 1
    assert counts["withdrawn"] == 1


async def test_a_withdrawn_chart_is_absent_from_the_counts_that_describe_usable_charts(
    db,
) -> None:
    """``consent_*`` and the two demographic gaps describe charts a clinician can still open."""
    account = await _account(db)
    await _patient(db, account, date_of_birth=None, is_deleted=True, deleted_at=datetime.now(UTC))

    counts = await DashboardService(db).patients(account.id)

    assert counts["date_of_birth_missing"] == 0
    assert counts["consent_recorded"] == 0


async def test_a_deleted_encounter_and_medication_leave_the_counts(db) -> None:
    account = await _account(db)
    patient = await _patient(db, account)
    await _encounter(db, patient, is_deleted=True, deleted_at=datetime.now(UTC))
    await _med(db, patient, is_deleted=True, deleted_at=datetime.now(UTC))

    service = DashboardService(db)

    assert (await service.encounters(account.id))["total"] == 0
    assert (await service.medications(account.id))["most_prescribed"] == []


async def test_an_encounter_on_a_withdrawn_chart_is_not_counted(db) -> None:
    """The chart is unopenable, so its visits are not part of the panel's volume."""
    account = await _account(db)
    patient = await _patient(db, account, is_deleted=True, deleted_at=datetime.now(UTC))
    await _encounter(db, patient)

    assert (await DashboardService(db).encounters(account.id))["total"] == 0


# --- the counting decisions ---------------------------------------------------------------


async def test_drugs_are_ranked_by_patients_not_by_rows(db) -> None:
    """A titration is not popularity.

    Amlodipine here has six events across one chart; Metformin has two events across two charts.
    Ranking by rows would put the drug that is adjusted most often at the top of a list every
    reader will understand as "what we prescribe most".
    """
    account = await _account(db)
    one = await _patient(db, account)
    two = await _patient(db, account)
    for _ in range(6):
        await _med(db, one, generic_name="Amlodipine", event_type="change")
    await _med(db, one, generic_name="Metformin")
    await _med(db, two, generic_name="Metformin")

    counts = await DashboardService(db).medications(account.id)

    assert [row["drug"] for row in counts["most_prescribed"]] == ["Metformin", "Amlodipine"]
    assert counts["most_prescribed"][0]["patient_count"] == 2
    assert counts["most_prescribed"][1]["event_count"] == 6


async def test_a_medication_with_no_generic_is_grouped_under_the_brand_as_charted(db) -> None:
    """Not resolved through the vocabulary here, and visibly so — the row is named as written."""
    account = await _account(db)
    patient = await _patient(db, account)
    await _med(db, patient, generic_name=None, brand_name_raw="Crocin")

    counts = await DashboardService(db).medications(account.id)

    assert counts["most_prescribed"][0]["drug"] == "Crocin"


async def test_a_medication_with_neither_name_is_counted_as_unnamed_rather_than_dropped(
    db,
) -> None:
    """ "We prescribed things we cannot name" is the finding, not a row to discard."""
    account = await _account(db)
    patient = await _patient(db, account)
    await _med(db, patient, generic_name=None, brand_name_raw=None)

    counts = await DashboardService(db).medications(account.id)

    assert counts["most_prescribed"][0]["drug"] == "(unnamed)"


async def test_an_encounter_with_no_recorded_type_is_bucketed_rather_than_dropped(db) -> None:
    """The column is nullable and extraction routinely cannot read a type off a scanned note."""
    account = await _account(db)
    patient = await _patient(db, account)
    await _encounter(db, patient, encounter_type=None)

    counts = await DashboardService(db).encounters(account.id)

    assert counts["by_type"] == {"(unspecified)": 1}
    assert counts["total"] == 1


async def test_encounter_status_counts_separate_signed_work_from_the_backlog(db) -> None:
    account = await _account(db)
    patient = await _patient(db, account)
    await _encounter(db, patient, status="draft")
    await _encounter(db, patient, status="draft")
    await _encounter(
        db,
        patient,
        status="signed",
        signed_at=datetime.now(UTC),
        signed_by_account_id=account.id,
    )

    counts = await DashboardService(db).encounters(account.id)

    assert counts["by_status"] == {"draft": 2, "signed": 1}


async def test_hard_blocks_are_summed_without_summing_a_boolean(db) -> None:
    """PostgreSQL will not SUM a boolean and SQLite will, so a cast here passes the suite and
    fails in production. The service uses a CASE; this is the assertion that notices if it
    stops."""
    account = await _account(db)
    patient = await _patient(db, account)
    drug = await _drug(db)
    await _flag(db, account, patient, drug, check_type="allergy_conflict", is_hard_block=True)
    await _flag(db, account, patient, drug, check_type="allergy_conflict", is_hard_block=False)
    await _flag(db, account, patient, drug, check_type="drug_interaction", is_hard_block=False)

    counts = await DashboardService(db).safety_flags(account.id)

    allergy = next(r for r in counts["by_check_type"] if r["check_type"] == "allergy_conflict")
    assert allergy == {"check_type": "allergy_conflict", "count": 2, "hard_blocks": 1}
    assert counts["total"] == 3
    assert counts["hard_blocks"] == 1


async def test_the_flag_distribution_leads_with_the_most_frequent_check(db) -> None:
    """This is the alert-fatigue instrument: the check firing most often is the finding."""
    account = await _account(db)
    patient = await _patient(db, account)
    drug = await _drug(db)
    await _flag(db, account, patient, drug, check_type="drug_interaction")
    await _flag(db, account, patient, drug, check_type="drug_interaction")
    await _flag(db, account, patient, drug, check_type="stale_medication")

    counts = await DashboardService(db).safety_flags(account.id)

    assert [row["check_type"] for row in counts["by_check_type"]] == [
        "drug_interaction",
        "stale_medication",
    ]


# --- bounds ------------------------------------------------------------------------------------


async def test_the_top_n_is_clamped_at_both_ends(db) -> None:
    """An unbounded GROUP BY over every medication row an account holds is the query that is
    instant on a demo and not on a year of use."""
    account = await _account(db)
    service = DashboardService(db)

    assert (await service.medications(account.id, limit=10_000))["limit"] == MAX_TOP_N
    assert (await service.medications(account.id, limit=0))["limit"] == 1
    assert (await service.medications(account.id, limit=-5))["limit"] == 1


# --- the routes --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/dashboard",
        "/api/v1/dashboard/patients",
        "/api/v1/dashboard/encounters",
        "/api/v1/dashboard/medications",
        "/api/v1/dashboard/safety-flags",
    ],
)
async def test_every_dashboard_route_requires_authentication(client, path) -> None:
    """There is no patient id in these paths, so the credential is the only thing deciding whose
    numbers come back."""
    resp = await client.get(path)

    assert resp.status_code in (401, 403), resp.text


async def test_the_overview_route_returns_all_four_tiles(auth_client) -> None:
    resp = await auth_client.get("/api/v1/dashboard")

    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == {"patients", "encounters", "medications", "safety_flags"}


async def test_the_medications_route_rejects_a_limit_past_the_ceiling(auth_client) -> None:
    """Clamped in the service, refused at the edge: a caller asking for 10,000 has misunderstood
    the route rather than made a request worth silently rewriting."""
    resp = await auth_client.get(f"/api/v1/dashboard/medications?limit={MAX_TOP_N + 1}")

    assert resp.status_code == 422, resp.text
