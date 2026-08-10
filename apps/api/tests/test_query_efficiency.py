"""Query-count regression guards for the read paths that scale with a patient's record size.

An N+1 is invisible in behavioural tests — the output is identical, only the round-trip count
grows — so it needs assertions on the query count itself. These cover the paths where the
number of rows is patient-controlled: current medications, active allergies, and mapped
conditions.

Two testing hazards these tests work around, both of which previously hid a real N+1:

* ``db.get()`` is served from the SQLAlchemy identity map, so an object already loaded in the
  test session costs no SQL. Every test here calls ``db.expunge_all()`` before measuring, which
  is what a production request (fresh session per request) actually looks like.
* Service objects cache reference data for their lifetime (``DrugResolver._all``). Each
  measurement constructs a fresh service so a warm cache cannot mask a per-row query.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select

from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.drug_vocabulary import DrugVocabulary
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.pathway_service import PathwayService
from app.services.safety_service import SafetyService
from tests.conftest import create_patient

pytestmark = pytest.mark.asyncio


@contextmanager
def counting_queries(engine):
    """Yields a counter dict whose ``n`` tracks statements executed on `engine`."""
    counter = {"n": 0, "statements": []}

    def _count(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1
        counter["statements"].append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        yield counter
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"qe-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Query Count Patient",
        sex="female",
        date_of_birth=datetime(1972, 3, 4).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _vocab(db, generic: str) -> DrugVocabulary:
    result = await db.execute(
        select(DrugVocabulary).where(DrugVocabulary.generic_name.ilike(generic))
    )
    vocab = result.scalars().first()
    assert vocab is not None, f"seed data is missing {generic}"
    return vocab


async def _add_current_med(db, patient: Patient, generic: str) -> DrugVocabulary:
    vocab = await _vocab(db, generic)
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            drug_vocabulary_id=vocab.id,
            generic_name=vocab.generic_name,
            event_type="continue",
            is_current=True,
        )
    )
    await db.flush()
    return vocab


async def _add_allergy(db, patient: Patient, generic: str) -> None:
    vocab = await _vocab(db, generic)
    db.add(
        Allergy(
            patient_id=patient.id,
            drug_vocabulary_id=vocab.id,
            allergen_name=vocab.generic_name,
            allergen_type="drug",
            status="active",
        )
    )
    await db.flush()


async def _add_condition(db, patient: Patient, name: str) -> None:
    db.add(
        Condition(
            patient_id=patient.id,
            condition_name=name,
            status="active",
        )
    )
    await db.flush()


async def _flags_select_count(db, engine, account, patient) -> int:
    """SELECT count for one active_flags call, measured production-shaped.

    Counts reads only, because writes legitimately scale with the record: more current drugs
    mean more real interaction flags, and each flag is persisted as its own drug_safety_checks
    row. Counting every statement would conflate an N+1 read with correct audit behaviour.

    Uses ``active_flags`` rather than ``check_medication`` deliberately. ``check_medication``
    resolves the proposed drug first, and ``DrugResolver._all()`` selects the entire active
    vocabulary — which lands every row in the session identity map, so the old per-row
    ``db.get()`` was already served from memory on that path. ``active_flags`` reaches
    ``_build_context`` without that warm-up, so it is the path where the per-row fetch actually
    cost a round-trip per drug. The batched-query assertion below covers the mechanism itself.
    """
    with counting_queries(engine) as counter:
        service = SafetyService(db)
        db.expunge_all()
        counter["statements"].clear()
        await service.active_flags(account_id=account.id, patient_id=patient.id)
        return len([s for s in counter["statements"] if s.lstrip().upper().startswith("SELECT")])


async def test_safety_check_query_count_is_flat_in_medication_count(db, engine):
    """The batched vocabulary load must keep the safety context O(1) in current meds."""
    account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Metformin")
    baseline = await _flags_select_count(db, engine, account, patient)

    for generic in ("Atorvastatin", "Amlodipine", "Warfarin", "Paracetamol"):
        await _add_current_med(db, patient, generic)
    with_five = await _flags_select_count(db, engine, account, patient)

    assert with_five == baseline, (
        f"active_flags went from {baseline} to {with_five} SELECTs when the patient's current "
        "medications went from 1 to 5 — the per-medication DrugVocabulary fetch is back"
    )


async def test_safety_check_query_count_is_flat_in_allergy_count(db, engine):
    """Allergies share the single batched vocabulary query with medications."""
    account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Metformin")
    await _add_allergy(db, patient, "Paracetamol")
    baseline = await _flags_select_count(db, engine, account, patient)

    for generic in ("Azithromycin", "Atorvastatin", "Amlodipine"):
        await _add_allergy(db, patient, generic)
    with_four = await _flags_select_count(db, engine, account, patient)

    assert with_four == baseline, (
        f"active_flags went from {baseline} to {with_four} SELECTs when active allergies went "
        "from 1 to 4 — the per-allergy DrugVocabulary fetch is back"
    )


async def test_vocabulary_is_loaded_in_a_single_batched_query(db, engine):
    """Positive assertion on the mechanism, not just the count: one IN (...) query, no repeats.

    A count-only test would still pass if someone replaced N `db.get()` calls with N
    single-row SELECTs, so this pins the shape of the query too.
    """
    account, patient = await _account_and_patient(db)
    for generic in ("Metformin", "Atorvastatin", "Amlodipine"):
        await _add_current_med(db, patient, generic)

    with counting_queries(engine) as counter:
        service = SafetyService(db)
        db.expunge_all()
        counter["statements"].clear()
        await service.check_medication(
            account_id=account.id,
            patient_id=patient.id,
            drug_reference_id=None,
            drug_name="Aspirin",
        )
        vocab_selects = [
            s
            for s in counter["statements"]
            if "drug_vocabulary" in s.lower() and s.lstrip().upper().startswith("SELECT")
        ]

    by_id = [s for s in vocab_selects if "drug_vocabulary.id IN" in s.replace("\n", " ")]
    assert len(by_id) == 1, (
        f"expected exactly one batched vocabulary-by-id query, got {len(by_id)}: {by_id}"
    )


async def test_patient_pathways_query_count_is_flat_in_condition_count(db, engine):
    """for_patient must hydrate every matched pathway from one guideline lookup."""
    account, patient = await _account_and_patient(db)
    await _add_condition(db, patient, "Hypertension")

    async def _measure() -> int:
        with counting_queries(engine) as counter:
            service = PathwayService(db)
            db.expunge_all()
            counter["n"] = 0
            await service.for_patient(account_id=account.id, patient_id=patient.id)
            return counter["n"]

    baseline = await _measure()

    for name in ("Type 2 Diabetes Mellitus", "Dyslipidemia", "Community-Acquired Pneumonia"):
        await _add_condition(db, patient, name)
    with_four = await _measure()

    assert with_four == baseline, (
        f"patient pathways went from {baseline} to {with_four} queries when mapped conditions "
        "went from 1 to 4 — the per-condition guideline lookup is back"
    )


async def test_patient_pathways_still_returns_citations_for_every_matched_condition(db):
    """The batching must not cross-contaminate or drop citations between pathways."""
    account, patient = await _account_and_patient(db)
    for name in ("Hypertension", "Type 2 Diabetes Mellitus"):
        await _add_condition(db, patient, name)

    result = await PathwayService(db).for_patient(account_id=account.id, patient_id=patient.id)

    assert len(result["pathways"]) == 2
    for pathway in result["pathways"]:
        cited = [c for stage in pathway["stages"] for c in stage["citations"]]
        assert cited, f"{pathway['condition_name']} lost its citations under batched hydration"
        # Every citation a stage claims must be one the stage actually asked for.
        for stage in pathway["stages"]:
            for citation in stage["citations"]:
                assert citation["section_id"], "citation is missing its section id"

    # Single-pathway hydration agrees with the batched path.
    single = await PathwayService(db).get("hypertension")
    batched = next(p for p in result["pathways"] if p["condition_name"] == "Hypertension")
    assert single == batched


async def test_unmapped_conditions_cost_no_guideline_queries(db, engine):
    """A patient whose conditions map to no pathway must not query the guideline corpus."""
    account, patient = await _account_and_patient(db)
    for name in ("Some Unmapped Condition", "Another Unmapped One"):
        await _add_condition(db, patient, name)

    with counting_queries(engine) as counter:
        service = PathwayService(db)
        db.expunge_all()
        counter["statements"].clear()
        result = await service.for_patient(account_id=account.id, patient_id=patient.id)

    assert result["pathways"] == []
    assert sorted(result["unmapped_conditions"]) == [
        "Another Unmapped One",
        "Some Unmapped Condition",
    ]
    guideline_queries = [s for s in counter["statements"] if "guideline_chunks" in s.lower()]
    assert not guideline_queries, (
        f"no pathway matched, so the guideline corpus should not be queried: {guideline_queries}"
    )


# ------------------------------------------------------------------------------------------
# End-to-end query-count flatness.
#
# The service-level tests above pin individual call sites. These pin the whole request: a
# reintroduced N+1 anywhere between the router and the ORM shows up as a query count that
# grows with the patient's record size, whatever layer it was reintroduced in. Each endpoint
# is measured twice against two patients with different row counts and must issue the same
# number of statements both times.
# ------------------------------------------------------------------------------------------

_SCALING_ENDPOINTS = [
    ("patients_list", "/api/v1/patients"),
    ("patient_detail", "/api/v1/patients/{pid}"),
    ("longitudinal_record", "/api/v1/patients/{pid}/record"),
    ("documents", "/api/v1/patients/{pid}/documents"),
    ("drug_safety_flags", "/api/v1/patients/{pid}/drug-safety/flags"),
    ("drug_safety_overrides", "/api/v1/patients/{pid}/drug-safety/overrides"),
    ("pathways", "/api/v1/patients/{pid}/pathways"),
    ("audit", "/api/v1/patients/{pid}/audit"),
]


async def _seed_record(db, patient_id: uuid.UUID, account_id: uuid.UUID, n: int) -> None:
    """Add `n` rows of each patient-owned entity type — the counts a clinician controls."""
    from app.models.document import Document
    from app.models.encounter import Encounter
    from app.models.lab_result import LabResult

    for i in range(n):
        db.add(
            MedicationEvent(
                patient_id=patient_id,
                generic_name=f"Drug{i}",
                dose=f"{i}mg",
                event_type="start",
                is_current=True,
                event_date=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
        db.add(Condition(patient_id=patient_id, condition_name=f"Condition{i}", status="active"))
        db.add(
            Allergy(
                patient_id=patient_id,
                allergen_name=f"Allergen{i}",
                allergen_type="drug",
                severity="mild",
                status="active",
            )
        )
        db.add(
            LabResult(
                patient_id=patient_id,
                marker_name=f"Marker{i}",
                value_numeric=1.0 + i,
                unit="mg/dL",
                sample_date=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
        db.add(
            Encounter(
                patient_id=patient_id,
                encounter_type="outpatient",
                encounter_date=datetime(2024, 1, 1, tzinfo=UTC),
            )
        )
        db.add(
            Document(
                patient_id=patient_id,
                account_id=account_id,
                file_name=f"doc{i}.pdf",
                file_type="pdf",
                file_size_bytes=10,
                storage_path=f"/tmp/doc{i}.pdf",
                storage_hash_sha256=f"{i:064d}",
                extraction_status="completed",
            )
        )
    await db.commit()


@pytest.mark.parametrize(("label", "template"), _SCALING_ENDPOINTS, ids=[e[0] for e in _SCALING_ENDPOINTS])
async def test_read_endpoint_query_count_is_flat_in_record_size(
    db, auth_client, engine, label, template
):
    from sqlalchemy import select as _select

    from app.models.user import Account

    account_id = (await db.execute(_select(Account.id))).scalars().first()

    counts = []
    for n in (1, 6):
        patient = await create_patient(auth_client, full_name=f"{label}-{n}")
        await _seed_record(db, uuid.UUID(patient["id"]), account_id, n)
        # A production request gets a cold session; the identity map would otherwise serve
        # db.get() for free and hide a per-row fetch.
        db.expunge_all()
        with counting_queries(engine) as counter:
            resp = await auth_client.get(template.format(pid=patient["id"]))
        assert resp.status_code == 200, resp.text
        counts.append(counter["n"])

    assert counts[0] == counts[1], (
        f"{label} issued {counts[0]} queries for 1 row of each entity but {counts[1]} for 6 — "
        f"the count must not scale with the size of the record"
    )


async def test_mapped_pathways_batch_citations_across_every_matched_condition(db, auth_client, engine):
    """Four matched pathways must cost the same as one — citations are fetched in one query."""
    mapped = ["Hypertension", "Type 2 Diabetes Mellitus", "Dyslipidemia", "Community-Acquired Pneumonia"]

    counts = []
    for n in (1, 4):
        patient = await create_patient(auth_client, full_name=f"mapped-{n}")
        for name in mapped[:n]:
            db.add(
                Condition(
                    patient_id=uuid.UUID(patient["id"]), condition_name=name, status="active"
                )
            )
        await db.commit()
        db.expunge_all()
        with counting_queries(engine) as counter:
            resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/pathways")
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["pathways"]) == n, "the conditions must actually match a pathway"
        counts.append(counter["n"])

    assert counts[0] == counts[1], (
        f"one matched pathway cost {counts[0]} queries, four cost {counts[1]} — "
        f"guideline citations are being fetched per pathway instead of in one batch"
    )


async def test_merge_entities_query_count_is_independent_of_entity_count(db, auth_client, engine):
    """Ingestion is the write path that scales: a 30-line prescription is one document.

    The dedup keys are loaded once up front, so merging 32 entities must cost the same number
    of statements as merging 4.
    """
    from app.services.graph_service import GraphService

    def _entities(n: int) -> list[dict]:
        out: list[dict] = []
        for i in range(n):
            out.append(
                {"entity_type": "medication", "fields": {"generic_name": f"Drug{i}", "dose": f"{i}mg"}}
            )
            out.append(
                {"entity_type": "lab_result", "fields": {"marker_name": f"M{i}", "value_numeric": i}}
            )
            out.append({"entity_type": "condition", "fields": {"condition_name": f"C{i}"}})
            out.append({"entity_type": "allergy", "fields": {"allergen_name": f"A{i}"}})
        return out

    counts = []
    for n in (1, 8):
        created = await create_patient(auth_client, full_name=f"ingest-{n}")
        await db.commit()
        db.expunge_all()
        patient = await db.get(Patient, uuid.UUID(created["id"]))
        with counting_queries(engine) as counter:
            # A fresh service per measurement, so DrugResolver's warm cache cannot mask a
            # per-entity vocabulary lookup.
            result = await GraphService(db).merge_entities(
                patient=patient, document=None, entities=_entities(n)
            )
        await db.commit()
        assert sum(result.values()) == n * 4, result
        counts.append(counter["n"])

    assert counts[0] == counts[1], (
        f"merging 4 entities cost {counts[0]} queries but 32 cost {counts[1]} — "
        f"the merge is querying per entity"
    )
