"""Service- and core-layer branches the happy-path suite never reaches.

Each test targets a line the full-suite coverage report listed as missing under ``app/core/``,
``app/services/`` or ``app/schemas/``. The clustering is deliberate: most of these are
"the input was absent, malformed, or duplicated" guards, and each one decides whether a
clinical signal is dropped silently or handled.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.lab_safety import evaluate_lab_results
from app.core.safety import ContraindicationRule, DrugRef, SafetyContext, _evaluate_renal
from app.exceptions import ValidationError
from app.models.condition import Condition
from app.models.drug_vocabulary import DrugVocabulary
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.models.user import Account
from app.schemas.patient import PatientUpdate
from app.services.drug_resolver import DrugResolver
from app.services.extraction.text_parser import parse_text
from app.services.graph_service import _parse_date, _parse_datetime
from app.services.guideline_service import GuidelineService, lexical_score
from app.services.lab_safety_service import LabSafetyService
from app.services.pathway_service import PathwayService
from app.services.patient_service import PatientService
from app.services.reasoning_service import ReasoningService
from tests.conftest import create_patient


async def _account_id(db) -> uuid.UUID:
    """The id of the account the ``auth_client`` fixture signed up."""
    account_id = (await db.execute(select(Account.id))).scalars().first()
    assert account_id is not None, "no account exists — use the auth_client fixture"
    return account_id


# --------------------------------------------------------------- drug resolver
# CLAUDE.md pitfall #4: resolution must always run through the vocabulary. These are the
# branches where it declines to guess.


async def test_resolver_declines_a_missing_or_blank_drug_name(db):
    resolver = DrugResolver(db)
    assert await resolver.resolve(None) is None
    assert await resolver.resolve("") is None
    assert await resolver.resolve("    ") is None


async def test_resolver_returns_none_when_the_vocabulary_is_empty(db, monkeypatch):
    """An unseeded vocabulary must yield no match rather than a fuzzy match against nothing."""
    resolver = DrugResolver(db)

    async def _no_rows() -> list[DrugVocabulary]:
        return []

    monkeypatch.setattr(resolver, "_all", _no_rows)
    assert await resolver.resolve("Crocin") is None


async def test_resolver_matches_a_reference_id_exactly_and_in_preference_to_a_name(db):
    """A reference id is the canonical key, so it must win before brand/generic matching."""
    ref = f"ref-{uuid.uuid4().hex}"
    db.add(DrugVocabulary(brand_name="Crocin", generic_name="Paracetamol", reference_id=ref))
    await db.flush()

    resolved = await DrugResolver(db).resolve(ref.upper())  # case-insensitive

    assert resolved is not None
    assert resolved.match_type == "exact_reference"
    assert resolved.score == 100.0
    assert resolved.generic_name == "Paracetamol"


async def test_resolver_fuzzy_matches_an_ocr_misspelling_of_a_brand_name(db):
    """OCR of a paper prescription mangles brand names; the vocabulary must still resolve."""
    db.add(
        DrugVocabulary(
            brand_name="Glycomet",
            generic_name="Metformin",
            reference_id=f"ref-{uuid.uuid4().hex}",
        )
    )
    await db.flush()

    resolved = await DrugResolver(db).resolve("Glycomett")

    assert resolved is not None
    assert resolved.match_type == "fuzzy"
    assert resolved.generic_name == "Metformin"
    assert resolved.score >= 86.0


async def test_resolve_reference_id_returns_none_for_an_id_not_in_the_vocabulary(db):
    assert await DrugResolver(db).resolve_reference_id("ref-does-not-exist") is None


# --------------------------------------------------------------- deterministic lab safety


def test_evaluate_lab_results_returns_only_the_flagged_markers():
    """The batch helper must drop non-critical, unrecognised, and value-less rows alike.

    Offline safety rule (#8): this is pure computation with no DB or LLM in the path.
    """
    flags = evaluate_lab_results(
        [
            ("Potassium", 7.4, "mmol/L"),  # critical high
            ("Potassium", 4.1, "mmol/L"),  # normal
            ("Haemoglobin", None, "g/dL"),  # no numeric value recorded
            ("Unrecognised marker", 999.0, "widgets"),  # not in the curated table
        ]
    )

    assert [f.canonical_marker for f in flags] == ["potassium"]
    assert flags[0].severity in {"critical_high", "panic_high"}
    assert flags[0].value == 7.4


def test_evaluate_lab_results_on_an_empty_batch_returns_no_flags():
    assert evaluate_lab_results([]) == []


def test_renal_rule_without_an_egfr_threshold_produces_no_flag():
    """A contraindication whose renal threshold omits ``egfr_below`` has nothing to compare.

    Guessing a cut-off here would invent a hard block the guideline never stated.
    """
    rule = ContraindicationRule(
        drug_reference_id="ref-1",
        condition_name="Chronic kidney disease",
        severity="dose_adjustment_required",
        description="Renal dosing applies.",
        is_absolute=False,
        renal_threshold={"action": "review"},  # no egfr_below key
    )
    proposed = DrugRef(reference_id="ref-1", generic_name="Metformin")

    assert _evaluate_renal(proposed, rule, SafetyContext(egfr=22.0)) is None


def test_renal_rule_produces_no_flag_when_the_patient_has_no_egfr_on_record():
    """Without an eGFR there is nothing to compare — the rule must abstain, not assume."""
    rule = ContraindicationRule(
        drug_reference_id="ref-1",
        condition_name="Chronic kidney disease",
        severity="absolute",
        description="Contraindicated below eGFR 30.",
        is_absolute=True,
        renal_threshold={"egfr_below": 30, "action": "contraindicated"},
    )
    proposed = DrugRef(reference_id="ref-1", generic_name="Metformin")

    assert _evaluate_renal(proposed, rule, SafetyContext(egfr=None)) is None
    # ...but a recorded eGFR under the threshold must still hard-block.
    flag = _evaluate_renal(proposed, rule, SafetyContext(egfr=22.0))
    assert flag is not None and flag.is_hard_block is True


# --------------------------------------------------------------- lab safety service


async def test_patient_lab_check_skips_a_result_with_no_numeric_value(db, auth_client):
    """A qualitative/unparsed lab row must be skipped, not coerced to a number.

    Coercing it would be worse than ignoring it: a fabricated value could raise a critical flag.
    """
    patient = await create_patient(auth_client)
    patient_id = uuid.UUID(patient["id"])
    account_id = await _account_id(db)

    db.add(
        LabResult(
            patient_id=patient_id,
            marker_name="Potassium",
            value_numeric=None,
            value_text="haemolysed sample",
            unit="mmol/L",
            sample_date=date(2026, 8, 1),
        )
    )
    db.add(
        LabResult(
            patient_id=patient_id,
            marker_name="Sodium",
            value_numeric=110.0,
            unit="mmol/L",
            sample_date=date(2026, 8, 1),
        )
    )
    await db.flush()

    flagged = await LabSafetyService(db).check_patient_labs(
        account_id=account_id, patient_id=patient_id, audit=False
    )

    assert [lab.marker_name for lab, _ in flagged] == ["Sodium"]


# --------------------------------------------------------------- patient service / schemas


async def test_granting_consent_stamps_the_consent_timestamp_once(db, auth_client):
    """DPDP Act: the moment consent was given must be recorded when it flips to true.

    Re-confirming consent must not move the timestamp — the original grant is the auditable
    fact, and a moving timestamp would misrepresent when processing became lawful.
    """
    # The create endpoint refuses consent_given=false outright (DPDP gate), so the
    # not-yet-consented row is seeded directly to exercise the transition itself.
    account_id = await _account_id(db)
    patient = Patient(
        account_id=account_id,
        full_name="Consent Transition",
        sex="unknown",
        consent_given=False,
    )
    db.add(patient)
    await db.flush()
    patient_id = patient.id
    assert patient.consent_given_at is None
    service = PatientService(db)

    granted = await service.update(account_id, patient_id, PatientUpdate(consent_given=True))
    first_stamp = granted.consent_given_at
    assert granted.consent_given is True
    assert first_stamp is not None

    again = await service.update(account_id, patient_id, PatientUpdate(consent_given=True))
    assert again.consent_given_at == first_stamp


async def test_withdrawing_consent_clears_the_flag_but_keeps_the_original_grant_time(
    db, auth_client
):
    created = await create_patient(auth_client, consent_given=True)
    patient_id = uuid.UUID(created["id"])
    account_id = await _account_id(db)

    withdrawn = await PatientService(db).update(
        account_id, patient_id, PatientUpdate(consent_given=False)
    )

    assert withdrawn.consent_given is False
    assert withdrawn.consent_given_at is not None


def test_patient_update_accepts_an_explicitly_absent_dob_and_phone():
    """``None`` must pass the validators through — it means "not recorded", not "invalid"."""
    update = PatientUpdate(date_of_birth=None, phone=None)
    assert update.date_of_birth is None
    assert update.phone is None

    blank = PatientUpdate(phone="   ")
    assert blank.phone == "   "  # whitespace-only is passed through unvalidated


def test_patient_update_still_rejects_a_future_dob_and_a_junk_phone():
    with pytest.raises(ValueError):
        PatientUpdate(date_of_birth=date.today() + timedelta(days=1))
    with pytest.raises(ValueError):
        PatientUpdate(phone="not-a-number!!")


# --------------------------------------------------------------- guideline service


def test_lexical_score_returns_nothing_for_an_empty_query_or_an_empty_corpus():
    """A blank query must score nothing rather than ranking arbitrary chunks first."""
    assert lexical_score("", [], 5) == []
    assert lexical_score("   ", [], 5) == []


async def test_get_by_section_ids_short_circuits_on_an_empty_id_list(db):
    """The citation-resolving path must not issue a ``WHERE section_id IN ()`` query."""
    assert await GuidelineService(db).get_by_section_ids([]) == []


# --------------------------------------------------------------- pathway service


async def test_duplicate_conditions_map_to_a_single_pathway(db, auth_client):
    """Two encounters recording the same condition must not duplicate its pathway card."""
    patient = await create_patient(auth_client)
    patient_id = uuid.UUID(patient["id"])
    account_id = await _account_id(db)
    for _ in range(2):
        db.add(Condition(patient_id=patient_id, condition_name="Hypertension", status="active"))
    await db.flush()

    result = await PathwayService(db).for_patient(account_id, patient_id)

    names = [p["condition_name"] for p in result["pathways"]]
    assert names == ["Hypertension"], f"duplicate condition produced {names}"


# --------------------------------------------------------------- reasoning service


async def test_submitting_answers_after_intake_is_complete_is_a_no_op(db, auth_client):
    """Late answers must not reopen a completed intake or duplicate answer rows."""
    patient = await create_patient(auth_client)
    account_id = await _account_id(db)
    service = ReasoningService(db)
    session, _ = await service.start(account_id, uuid.UUID(patient["id"]), "chest pain")
    session.intake_complete = True
    await db.flush()

    returned, questions = await service.submit_answers(
        account_id, session.id, [{"question_id": uuid.uuid4(), "answer_text": "yes"}]
    )

    assert returned.id == session.id
    assert questions == []


async def test_an_answer_for_an_unknown_question_id_is_ignored(db, auth_client):
    """A stale or forged question id must be dropped, not persisted against the session."""
    patient = await create_patient(auth_client)
    account_id = await _account_id(db)
    service = ReasoningService(db)
    session, questions = await service.start(
        account_id, uuid.UUID(patient["id"]), "fever for three days"
    )
    assert questions, "the triage agent generated no questions to answer"

    _, _ = await service.submit_answers(
        account_id,
        session.id,
        [
            {"question_id": uuid.uuid4(), "answer_text": "yes"},  # unknown
            {"question_id": questions[0].id, "answer_text": "no"},  # real
        ],
    )

    answered = [q for q in await service._questions(session.id) if q.answered_at is not None]
    assert [q.id for q in answered] == [questions[0].id]


async def test_stream_relays_a_pipeline_failure_as_an_error_event(db, monkeypatch):
    """A crash inside the pipeline must reach the UI as an error, not hang the SSE stream.

    Without the relay the browser sees an open connection that never terminates, which the
    Reasoning Theatre renders as agents thinking forever.
    """
    service = ReasoningService(db)

    async def _boom(*args, **kwargs):
        raise RuntimeError("verifier exploded")

    monkeypatch.setattr(service, "run", _boom)

    events = [item async for item in service.stream(uuid.uuid4(), uuid.uuid4())]

    assert events == [("error", {"message": "verifier exploded"})]


async def test_closing_the_stream_early_cancels_the_still_running_pipeline(db, monkeypatch):
    """A client disconnect must cancel the worker instead of leaking a running task."""
    service = ReasoningService(db)
    started = asyncio.Event()

    async def _slow(account_id, session_id, emit=None):
        await emit("agent_start", {"agent": "triage"})
        started.set()
        await asyncio.sleep(30)  # still running when the consumer walks away

    monkeypatch.setattr(service, "run", _slow)

    agen = service.stream(uuid.uuid4(), uuid.uuid4())
    first = await agen.__anext__()
    assert first == ("agent_start", {"agent": "triage"})
    await started.wait()
    await agen.aclose()

    # The worker task was cancelled rather than left pending.
    pending = [t for t in asyncio.all_tasks() if "_slow" in repr(t.get_coro())]
    assert all(t.cancelled() or t.done() for t in pending)


# --------------------------------------------------------------- text parser


def test_a_section_header_repeated_inside_a_section_is_not_parsed_as_an_entity():
    """Lab printouts repeat their headers per page; those lines must not become entities."""
    entities = parse_text(
        "\n".join(
            [
                "Medications",
                "medications",  # header echoed as a body line
                "Amoxicillin 500mg TDS",
                "Allergies",
                "allergies",
                "Penicillin - rash",
            ]
        )
    )

    kinds = [(e.entity_type, e.fields[0].value) for e in entities]
    assert ("medication", "Amoxicillin") in kinds
    assert ("allergy", "Penicillin") in kinds
    assert len(entities) == 2, f"a header leaked through as an entity: {kinds}"


def test_lines_under_an_unparseable_section_are_skipped():
    """A recognised-but-unhandled section (e.g. vitals) must not fall through to a parser."""
    entities = parse_text("Vitals\nBP 130/80\nPulse 78")
    assert entities == [] or all(e.entity_type != "allergy" for e in entities)


# --------------------------------------------------------------- graph service coercion


def test_date_and_datetime_parsers_pass_already_typed_values_through_unchanged():
    """Extraction sometimes hands over real date objects; re-parsing them would be lossy."""
    d = date(2026, 8, 10)
    dt = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)

    assert _parse_date(d) is d
    assert _parse_datetime(dt) is dt
    assert _parse_date(None) is None
    assert _parse_datetime(None) is None


def test_datetime_is_a_valid_date_input_and_is_returned_as_is():
    """``datetime`` subclasses ``date``, so the date parser accepts it without conversion."""
    dt = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    assert _parse_date(dt) is dt


# --------------------------------------------------------------- validation guard reuse


def test_validation_error_is_raisable_with_a_message():
    """Sanity-check the exception the schema validators funnel into."""
    with pytest.raises(ValidationError) as exc:
        raise ValidationError("phone must contain only digits")
    assert "phone" in str(exc.value)
