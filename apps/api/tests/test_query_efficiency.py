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
from app.models.drug_vocabulary import Contraindication, DrugInteraction, DrugVocabulary
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.pathway_service import PathwayService
from app.services.safety_service import SafetyService
from tests.conftest import create_patient


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
    ``build_context`` without that warm-up, so it is the path where the per-row fetch actually
    cost a round-trip per drug. The batched-query assertion below covers the mechanism itself.
    """
    with counting_queries(engine) as counter:
        service = SafetyService(db)
        db.expunge_all()
        counter["statements"].clear()
        await service.active_flags(account_id=account.id, patient_id=patient.id)
        return len([s for s in counter["statements"] if s.lstrip().upper().startswith("SELECT")])


async def test_safety_check_query_count_is_flat_in_medication_count(db, engine):
    """The batched vocabulary load must keep the safety context O(1) in current meds.

    The baseline is *two* medications, not one: below two there is no drug pair to interact,
    so ``_load_interactions`` short-circuits and issues no query at all. That step is asserted
    separately in ``test_a_single_medication_costs_no_interaction_query``; here we measure
    growth in the regime where every query is already being issued.
    """
    account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Metformin")
    await _add_current_med(db, patient, "Atorvastatin")
    baseline = await _flags_select_count(db, engine, account, patient)

    for generic in ("Amlodipine", "Warfarin", "Paracetamol"):
        await _add_current_med(db, patient, generic)
    with_five = await _flags_select_count(db, engine, account, patient)

    assert with_five == baseline, (
        f"active_flags went from {baseline} to {with_five} SELECTs when the patient's current "
        "medications went from 2 to 5 — the per-medication DrugVocabulary fetch is back"
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
    # The FHIR export. Absent from this matrix until now, and the one read here with no page
    # limit at all: it assembles every section of the chart into one bundle by design (a short
    # export that does not admit it is worse than no export). A per-row query on this path would
    # therefore be unbounded rather than bounded by a page, which makes it the endpoint where an
    # N+1 costs the most and the one that had no guard.
    ("fhir_export", "/api/v1/patients/{pid}/export"),
    # The deterministic panic-value screen. Offline by construction (Critical Safety Rule #8),
    # runs on every call with no caching, and reads the patient's latest labs — so a per-lab
    # query here would put the growth on the safety path that must stay fast when everything
    # else is degraded.
    ("critical_lab_flags", "/api/v1/patients/{pid}/labs/critical-flags"),
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


@pytest.mark.parametrize(
    ("label", "template"), _SCALING_ENDPOINTS, ids=[e[0] for e in _SCALING_ENDPOINTS]
)
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


async def test_mapped_pathways_batch_citations_across_every_matched_condition(
    db, auth_client, engine
):
    """Four matched pathways must cost the same as one — citations are fetched in one query."""
    mapped = [
        "Hypertension",
        "Type 2 Diabetes Mellitus",
        "Dyslipidemia",
        "Community-Acquired Pneumonia",
    ]

    counts = []
    for n in (1, 4):
        patient = await create_patient(auth_client, full_name=f"mapped-{n}")
        for name in mapped[:n]:
            db.add(
                Condition(patient_id=uuid.UUID(patient["id"]), condition_name=name, status="active")
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
                {
                    "entity_type": "medication",
                    "fields": {"generic_name": f"Drug{i}", "dose": f"{i}mg"},
                }
            )
            out.append(
                {
                    "entity_type": "lab_result",
                    "fields": {"marker_name": f"M{i}", "value_numeric": i},
                }
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


# ---------------------------------------------------- drug-safety reference-data scoping
# The two reference tables (drug_interactions, contraindications) used to be loaded in full on
# every safety check. That is invisible in behavioural tests and cheap at seed size, but it
# scales with the *corpus*, not with the patient — so it gets worse exactly as the product's
# clinical coverage improves. These pin the scoping.


async def _add_interaction_rows(db, count: int) -> None:
    """Insert `count` interaction rules between drugs no test patient is ever on."""
    for i in range(count):
        db.add(
            DrugInteraction(
                drug_a_reference_id=f"NOISE-A-{i:04d}",
                drug_b_reference_id=f"NOISE-B-{i:04d}",
                severity="major",
                description=f"synthetic corpus-growth row {i}",
                source="test",
            )
        )
    await db.flush()


async def _add_contraindication_rows(db, count: int) -> None:
    for i in range(count):
        db.add(
            Contraindication(
                drug_reference_id=f"NOISE-C-{i:04d}",
                condition_name=f"Synthetic Condition {i}",
                severity="absolute",
                description=f"synthetic corpus-growth row {i}",
                is_absolute=True,
                source="test",
            )
        )
    await db.flush()


async def test_reference_rules_loaded_do_not_grow_with_the_corpus(db):
    """Growing the rule tables must not grow what one patient's safety context materialises."""
    _account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Metformin")
    await _add_current_med(db, patient, "Warfarin")

    before = await SafetyService(db).build_context(patient.id)

    await _add_interaction_rows(db, 200)
    await _add_contraindication_rows(db, 200)

    after = await SafetyService(db).build_context(patient.id)

    assert len(after.interaction_rules) == len(before.interaction_rules), (
        f"400 unrelated reference rows changed the loaded interaction count from "
        f"{len(before.interaction_rules)} to {len(after.interaction_rules)} — the safety "
        "context is loading the whole table again"
    )
    assert len(after.contraindication_rules) == len(before.contraindication_rules), (
        "unrelated contraindication rows are being loaded into the patient's safety context"
    )


async def test_only_rules_touching_the_drugs_in_play_are_loaded(db):
    """Positive assertion: every loaded rule references a drug the evaluation can involve."""
    _account, patient = await _account_and_patient(db)
    metformin = await _add_current_med(db, patient, "Metformin")
    warfarin = await _add_current_med(db, patient, "Warfarin")
    aspirin = await _vocab(db, "Aspirin")

    ctx = await SafetyService(db).build_context(
        patient.id, proposed_reference_ids=[aspirin.reference_id]
    )

    in_play = {metformin.reference_id, warfarin.reference_id, aspirin.reference_id}
    for rule in ctx.interaction_rules:
        assert {rule.drug_a_reference_id, rule.drug_b_reference_id} <= in_play, (
            f"loaded an interaction rule ({rule.drug_a_reference_id}, "
            f"{rule.drug_b_reference_id}) that cannot fire for these drugs"
        )
    for ci in ctx.contraindication_rules:
        assert ci.drug_reference_id in in_play, (
            f"loaded a contraindication for {ci.drug_reference_id}, which is not in play"
        )


async def test_the_proposed_drugs_own_rules_are_still_loaded(db):
    """Scoping must not narrow so far that the proposal's own rules disappear.

    Warfarin + aspirin is a curated interacting pair; the proposed drug is not yet a current
    medication, so only ``proposed_reference_ids`` can bring its rules into scope.
    """
    _account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Warfarin")
    aspirin = await _vocab(db, "Aspirin")

    ctx = await SafetyService(db).build_context(
        patient.id, proposed_reference_ids=[aspirin.reference_id]
    )

    pairs = {
        frozenset((r.drug_a_reference_id, r.drug_b_reference_id)) for r in ctx.interaction_rules
    }
    warfarin = await _vocab(db, "Warfarin")
    assert frozenset((warfarin.reference_id, aspirin.reference_id)) in pairs, (
        "the warfarin/aspirin interaction rule was scoped out of the context that is supposed "
        "to catch it"
    )


async def test_a_single_medication_costs_no_interaction_query(db, engine):
    """One drug cannot pair with anything, so the interaction table is not touched at all."""
    _account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Metformin")

    service = SafetyService(db)
    db.expunge_all()
    with counting_queries(engine) as counter:
        await service.build_context(patient.id)

    touched = [s for s in counter["statements"] if "drug_interactions" in s]
    assert touched == [], f"queried drug_interactions for a single-drug patient: {touched}"


# ---------------------------------------------------- drug-vocabulary scaling
# The vocabulary is the third reference table, and the one the earlier scoping work did not
# reach. It is also the one that grows fastest: interactions and contraindications are curated
# pairs, but the vocabulary is every Indian brand name in the market — tens of thousands of
# rows — and it is read for every medication and every allergy of every safety check.


async def _add_vocabulary_rows(db, count: int) -> None:
    """Insert `count` drugs no test patient is ever on, to stand in for corpus growth."""
    for i in range(count):
        db.add(
            DrugVocabulary(
                brand_name=f"NoiseBrand{i:05d}",
                generic_name=f"NoiseGeneric{i:05d}",
                reference_id=f"NOISE-VOCAB-{i:05d}",
                drug_class="synthetic",
                source="test",
            )
        )
    await db.flush()


async def _vocabulary_rows_read(db, account, patient) -> int:
    """Vocabulary rows one ``active_flags`` call materialises.

    Counting statements is not enough here: the regression this guards is a *single* query
    that returns the entire corpus, which is indistinguishable from a targeted one in a
    statement count. Row counts are not available either — DBAPI ``cursor.rowcount`` is -1 for
    SELECTs on SQLite, so a counter built on it silently measures zero and passes whatever
    happens. The session's identity map is the reliable measure: every row the ORM hydrates
    lands in it, so its DrugVocabulary population after a cold start is exactly what the
    request pulled out of that table.
    """
    service = SafetyService(db)
    db.expunge_all()
    await service.active_flags(account_id=account.id, patient_id=patient.id)
    return sum(1 for obj in db.identity_map.values() if isinstance(obj, DrugVocabulary))


async def test_a_safety_check_does_not_read_the_whole_drug_vocabulary(db, engine):
    """1000+ unrelated drugs must not change what one patient's safety check reads.

    This is the assertion the round-14 reference-data scoping did not cover. Growing the
    vocabulary from 50 to 1050 rows previously grew every safety check by 1000 hydrated ORM
    objects, because resolution answered even exact reference-id lookups out of a
    full-corpus in-memory index.
    """
    _account, patient = await _account_and_patient(db)
    for generic in ("Metformin", "Warfarin", "Atorvastatin"):
        await _add_current_med(db, patient, generic)

    before = await _vocabulary_rows_read(db, _account, patient)
    await _add_vocabulary_rows(db, 1000)
    after = await _vocabulary_rows_read(db, _account, patient)

    assert after == before, (
        f"one safety check read {before} vocabulary rows on a 50-drug corpus and {after} "
        f"after 1000 unrelated drugs were added — resolution is loading the whole vocabulary, "
        f"so the hot safety path scales with market coverage instead of with the patient"
    )


async def test_active_flags_query_count_is_flat_in_medication_count_at_corpus_scale(db, engine):
    """Reference-id resolution must be batched, not one query per current medication."""
    _account, patient = await _account_and_patient(db)
    await _add_vocabulary_rows(db, 1000)
    await _add_current_med(db, patient, "Metformin")
    await _add_current_med(db, patient, "Atorvastatin")
    baseline = await _flags_select_count(db, engine, _account, patient)

    for generic in ("Amlodipine", "Warfarin", "Paracetamol"):
        await _add_current_med(db, patient, generic)
    with_five = await _flags_select_count(db, engine, _account, patient)

    assert with_five == baseline, (
        f"active_flags went from {baseline} to {with_five} SELECTs when current medications "
        "went from 2 to 5 — the per-drug vocabulary lookup is not batched"
    )


async def test_merging_a_long_prescription_does_not_read_the_whole_vocabulary_per_line(db, engine):
    """Ingestion resolves a name per line; at corpus scale that must stay one query."""
    from app.services.graph_service import GraphService

    _account, patient = await _account_and_patient(db)
    await _add_vocabulary_rows(db, 1000)
    await db.commit()

    entities = [
        {"entity_type": "medication", "fields": {"brand_name_raw": name, "dose": "500mg"}}
        for name in ("Crocin", "Glycomet", "Ecosprin", "Metformin", "Atorvastatin")
    ] + [{"entity_type": "allergy", "fields": {"allergen_name": "Penicillin"}}]

    db.expunge_all()
    with counting_queries(engine) as counter:
        await GraphService(db).merge_entities(patient=patient, document=None, entities=entities)

    vocabulary_reads = [
        s
        for s in counter["statements"]
        if "FROM drug_vocabulary" in s and s.lstrip().upper().startswith("SELECT")
    ]
    assert len(vocabulary_reads) <= 2, (
        f"merging 6 lines cost {len(vocabulary_reads)} vocabulary reads; the batched prefetch "
        f"should make it one (plus at most one fuzzy corpus load): {vocabulary_reads}"
    )


async def test_a_patient_on_no_medications_touches_neither_reference_table(db, engine):
    """An empty medication list with no proposal leaves nothing for either table to match."""
    _account, patient = await _account_and_patient(db)

    service = SafetyService(db)
    db.expunge_all()
    with counting_queries(engine) as counter:
        ctx = await service.build_context(patient.id)

    assert ctx.interaction_rules == []
    assert ctx.contraindication_rules == []
    touched = [
        s
        for s in counter["statements"]
        if "drug_interactions" in s or "FROM contraindications" in s
    ]
    assert touched == [], f"queried reference tables with no drugs in play: {touched}"


async def test_rules_are_loaded_for_every_proposed_drug_not_just_one(db):
    """The scope must widen to all the drugs a text names, not to the first of them.

    ``SafetyService.screen_text`` hands ``build_context`` every drug a guideline management
    option names, because a guideline sentence routinely names several ("an ACE inhibitor or ARB
    (e.g. enalapril/telmisartan)"). A rule for a drug outside the loaded scope cannot fire, so a
    parameter that quietly kept only one of them would silently drop the others' hard blocks
    while every query-count assertion above still passed.
    """
    _account, patient = await _account_and_patient(db)
    metformin = await _vocab(db, "Metformin")
    enalapril = await _vocab(db, "Enalapril")

    ctx = await SafetyService(db).build_context(
        patient.id, proposed_reference_ids=[metformin.reference_id, enalapril.reference_id]
    )

    scoped = {ci.drug_reference_id for ci in ctx.contraindication_rules}
    assert metformin.reference_id in scoped
    assert enalapril.reference_id in scoped, (
        "the second proposed drug's contraindication rules were never loaded, so its hard "
        "blocks could not fire"
    )


async def _screen_select_count(db, engine, patient, texts: list[str]) -> int:
    """SELECT count for screening ``texts`` against one patient, production-shaped."""
    with counting_queries(engine) as counter:
        service = SafetyService(db)
        db.expunge_all()
        counter["statements"].clear()
        for text in texts:
            await service.screen_text(patient.id, text)
        return len([s for s in counter["statements"] if s.lstrip().upper().startswith("SELECT")])


async def test_screening_more_management_options_does_not_re_read_the_chart(db, engine):
    """The patient half of the safety context is the same for every option screened.

    ``drug_safety_check`` screens each management option the run produced, and each screen went
    through a fresh ``build_context`` — re-reading the current medications, the active
    allergies, their vocabulary rows, the conditions and the latest eGFR every time, for a
    patient whose chart cannot have changed between two options of the same run. Only the two
    reference-rule loads legitimately depend on which drugs the option names.

    This runs on the path that streams to a waiting clinician, so the growth is in front of a
    progress spinner rather than in a batch job.
    """
    _account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Warfarin")
    await _add_allergy(db, patient, "Ibuprofen")
    await _add_condition(db, patient, "Chronic Kidney Disease")

    one = await _screen_select_count(db, engine, patient, ["Consider Metformin."])
    four = await _screen_select_count(
        db,
        engine,
        patient,
        [
            "Consider Metformin.",
            "Consider Aspirin.",
            "Consider Paracetamol.",
            "Consider Enalapril.",
        ],
    )

    # An extra option costs exactly its two reference-rule loads (interactions and
    # contraindications for the drugs it names) and nothing else. Five reads per option of a
    # chart that cannot have changed is the regression this pins.
    assert four - one <= 3 * 2, (
        f"screening 4 options cost {four} selects vs {one} for a single option — the "
        "patient-scoped half of the safety context is being re-read per option"
    )


async def test_a_fresh_safety_service_sees_a_changed_chart(db, engine):
    """The patient-facts memo lasts one unit of work, not one process.

    ``_patient_facts`` caches on the service instance, which is what makes screening several
    management options cheap. That is only sound because every construction site builds a fresh
    service per request or per reasoning run — so a medication added after a check must be
    visible to the next one. Pins the lifetime that assumption rests on.
    """
    _account, patient = await _account_and_patient(db)
    await _add_current_med(db, patient, "Warfarin")

    before = await SafetyService(db).build_context(patient.id)
    await _add_current_med(db, patient, "Aspirin")
    after = await SafetyService(db).build_context(patient.id)

    assert {m.generic_name for m in before.current_meds} == {"Warfarin"}
    assert {m.generic_name for m in after.current_meds} == {"Warfarin", "Aspirin"}


# ------------------------------------------------------------------------------------------
# Collection-size flatness.
#
# Everything above scales one patient's *record* and holds the collection at one row. That is
# the growth an individual chart drives, and it is not the growth a list endpoint has. A list
# read whose per-item cost is a query looks perfectly flat under the matrix above — the patient
# whose record grew is still one row in it — and degrades with how long the deployment has been
# in service instead. These measure the other axis: same record, more rows in the list.
# ------------------------------------------------------------------------------------------


async def _account_id(db) -> uuid.UUID:
    from sqlalchemy import select as _select

    return (await db.execute(_select(Account.id))).scalars().first()


async def test_patient_list_query_count_is_flat_in_the_number_of_patients(db, auth_client, engine):
    """A clinician's panel grows for years; the list read must not grow with it.

    The record-size matrix cannot see this: it pages one patient's rows and leaves the account
    holding a handful of charts, so a per-patient query in the list would have been measured as
    flat throughout.
    """
    counts = []
    for target in (2, 12):
        while len((await auth_client.get("/api/v1/patients?limit=100")).json()["items"]) < target:
            await create_patient(auth_client, full_name=f"panel-{uuid.uuid4().hex[:8]}")
        db.expunge_all()
        with counting_queries(engine) as counter:
            resp = await auth_client.get("/api/v1/patients?limit=100")
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["items"]) >= target
        counts.append(counter["n"])

    assert counts[0] == counts[1], (
        f"listing 2 patients cost {counts[0]} queries and listing 12 cost {counts[1]} — the "
        "patient list is querying per row"
    )


async def test_patient_search_query_count_is_flat_in_the_number_of_patients(
    db, auth_client, engine
):
    """Search decrypts and filters in Python, which must stay one read of the panel.

    ``full_name`` and ``phone`` are non-deterministic ciphertext, so search cannot be a SQL
    ``ILIKE`` and loads the account's patients to filter after decryption. That design makes a
    per-row query especially easy to add by accident and especially invisible: the decryption
    loop is already the expensive part.
    """

    async def _matches() -> int:
        resp = await auth_client.post("/api/v1/patients/search", json={"search": "seek-"})
        return int(resp.json()["pagination"]["total"])

    counts = []
    for target in (2, 12):
        while await _matches() < target:
            await create_patient(auth_client, full_name=f"seek-{uuid.uuid4().hex[:8]}")
        db.expunge_all()
        with counting_queries(engine) as counter:
            resp = await auth_client.post("/api/v1/patients/search", json={"search": "seek-"})
        assert resp.status_code == 200, resp.text
        counts.append(counter["n"])

    assert counts[0] == counts[1], (
        f"searching a 2-patient panel cost {counts[0]} queries and a 12-patient panel "
        f"{counts[1]} — the decrypt-and-filter loop is issuing a query per patient"
    )


async def test_document_list_query_count_is_flat_in_the_number_of_documents(
    db, auth_client, engine
):
    """The chart's document list also closes out stalled extractions; that must stay batched.

    Opening the chart is what reclaims documents whose extraction was interrupted, so this read
    does more than select rows — which is exactly the kind of per-item work that turns into a
    query per document without anyone meaning it to.
    """
    from app.models.document import Document

    account_id = await _account_id(db)
    counts = []
    for n in (1, 10):
        patient = await create_patient(auth_client, full_name=f"doclist-{n}")
        for i in range(n):
            db.add(
                Document(
                    patient_id=uuid.UUID(patient["id"]),
                    account_id=account_id,
                    file_name=f"doc{i}.pdf",
                    file_type="pdf",
                    file_size_bytes=10,
                    storage_path=f"/tmp/doclist-{n}-{i}.pdf",
                    storage_hash_sha256=uuid.uuid4().hex * 2,
                    extraction_status="completed",
                )
            )
        await db.commit()
        db.expunge_all()
        with counting_queries(engine) as counter:
            resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/documents")
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == n
        counts.append(counter["n"])

    assert counts[0] == counts[1], (
        f"listing 1 document cost {counts[0]} queries and listing 10 cost {counts[1]}"
    )


async def test_safety_report_register_query_count_is_flat_in_the_number_of_reports(
    db, auth_client, engine
):
    """The adverse-event register is append-only and unpaged; a per-row query is unbounded."""
    from app.models.validation import SafetyReport

    account_id = await _account_id(db)
    counts = []
    for n in (1, 10):
        for i in range(n):
            db.add(
                SafetyReport(
                    account_id=account_id,
                    category="near_miss",
                    severity="near_miss",
                    description=f"register row {i}",
                )
            )
        await db.commit()
        db.expunge_all()
        with counting_queries(engine) as counter:
            resp = await auth_client.get("/api/v1/safety-reports")
        assert resp.status_code == 200, resp.text
        counts.append(counter["n"])

    assert counts[0] == counts[1], (
        f"a register of 1 report cost {counts[0]} queries and one of 11 cost {counts[1]}"
    )


async def test_validation_run_list_query_count_is_flat_in_the_number_of_runs(
    db, auth_client, engine
):
    """The SaMD evidence trail: also append-only, also unpaged, also never pruned."""
    from app.models.validation import ValidationRun

    account_id = await _account_id(db)
    counts = []
    for n in (1, 10):
        for i in range(n):
            db.add(ValidationRun(account_id=account_id, vignette_count=1, notes=f"run {i}"))
        await db.commit()
        db.expunge_all()
        with counting_queries(engine) as counter:
            resp = await auth_client.get("/api/v1/validation/runs")
        assert resp.status_code == 200, resp.text
        counts.append(counter["n"])

    assert counts[0] == counts[1], (
        f"listing 1 validation run cost {counts[0]} queries and listing 11 cost {counts[1]}"
    )


# ------------------------------------------------------------------------------------------
# Session-scoped flatness.
#
# A reasoning session's output is the third axis, independent of both the chart's size and the
# account's. One run emits a suggestion per differential, per can't-miss diagnosis, per
# investigation and per management option, and these two endpoints are what the Reasoning
# Theatre reads back — including the dissent, which must never be filtered out, so the count
# is not something the UI can trim its way out of.
# ------------------------------------------------------------------------------------------


async def _completed_session(db, auth_client, label: str, *, output_type: str, n: int):
    from app.models.clinical_suggestion import ClinicalSuggestion
    from app.models.reasoning_session import ReasoningSession

    account_id = await _account_id(db)
    patient = await create_patient(auth_client, full_name=label)
    session = ReasoningSession(
        patient_id=uuid.UUID(patient["id"]),
        account_id=account_id,
        presenting_complaint="chest pain for two days",
        status="completed",
    )
    db.add(session)
    await db.flush()
    for i in range(n):
        db.add(
            ClinicalSuggestion(
                session_id=session.id,
                patient_id=uuid.UUID(patient["id"]),
                output_type=output_type,
                title=f"{output_type} {i}",
                body="Guidelines support considering this.",
                autonomy_tier="suggestive",
            )
        )
    await db.commit()
    return session


async def test_suggestion_list_query_count_is_flat_in_the_number_of_suggestions(
    db, auth_client, engine
):
    counts = []
    for n in (1, 12):
        session = await _completed_session(
            db, auth_client, f"sugg-{n}", output_type="differential", n=n
        )
        db.expunge_all()
        with counting_queries(engine) as counter:
            resp = await auth_client.get(f"/api/v1/reasoning/{session.id}/suggestions")
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == n
        counts.append(counter["n"])

    assert counts[0] == counts[1], (
        f"reading back 1 suggestion cost {counts[0]} queries and 12 cost {counts[1]} — the "
        "Reasoning Theatre's result read is querying per suggestion"
    )


async def test_management_option_list_query_count_is_flat_in_the_number_of_options(
    db, auth_client, engine
):
    """Management options carry citations and safety screening, so they are the likelier one."""
    counts = []
    for n in (1, 12):
        session = await _completed_session(
            db, auth_client, f"mgmt-{n}", output_type="management", n=n
        )
        db.expunge_all()
        with counting_queries(engine) as counter:
            resp = await auth_client.get(f"/api/v1/reasoning/{session.id}/management-options")
        assert resp.status_code == 200, resp.text
        counts.append(counter["n"])

    assert counts[0] == counts[1], (
        f"reading back 1 management option cost {counts[0]} queries and 12 cost {counts[1]}"
    )
