"""A medication the resolver cannot read is reported as unevaluated, not as evaluated-and-fine.

``SafetyService._current_meds`` drops any current-medication row whose name resolves to no
vocabulary entry, because an unresolved drug cannot have rules evaluated against it. That part
is right — a guess is worse than no answer (CLAUDE.md pitfall #4). What was wrong is that the
drop was silent: every rule keyed on ``current_meds`` then had nothing to say, and the response
came back with an empty flag list, which is the same response as a chart with nothing to find.

The shape this codebase already refuses twice — an unresolved *proposed* drug is a 422 rather
than an unchecked pass, and a renal rule with no eGFR reports itself unevaluated rather than
passed — reaching the third place a check can be incomplete without saying so.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.safety import (
    DrugRef,
    SafetyContext,
    check_unevaluated_medications,
    evaluate_drug_safety,
)
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

# The two halves of the textbook major interaction. The chart carries one of them under a name
# nothing resolves to, and the clinician proposes the other.
#
# "Warf 5mg" would resolve — the fuzzy tier's partial-ratio path scores a query that merely
# *contains* a candidate name at 90, which is deliberate and load-bearing for the prescription
# shorthand this product reads. This is a garbling that clears nothing: 77 against its nearest
# candidate, below the 86 acceptance threshold.
_GARBLED = "Wrfrn 5mg (illegible)"


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"unev-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Unevaluated Med Patient",
        sex="male",
        date_of_birth=datetime(1961, 7, 2).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _add_unresolvable_med(db, patient: Patient, name: str = _GARBLED) -> None:
    """A current medication with no vocabulary link and a name nothing matches.

    Exactly what an approved extraction produces from a handwritten line the OCR could not
    read, or from a brand the vocabulary has not been seeded with yet.
    """
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            drug_vocabulary_id=None,
            generic_name=name,
            event_type="continue",
            is_current=True,
        )
    )
    await db.flush()


# --- the pure check -------------------------------------------------------------------------


def test_a_chart_with_nothing_unreadable_raises_nothing() -> None:
    """The flag exists to report incompleteness, so a complete chart must stay silent.

    An unclearable warning on every check is the thing that teaches clinicians to click past
    the real ones.
    """
    ctx = SafetyContext(current_meds=[DrugRef("MET-500", "Metformin", "Biguanide")])

    assert check_unevaluated_medications(ctx) == []
    assert check_unevaluated_medications(SafetyContext()) == []


@pytest.mark.parametrize("blank", [[""], ["   "], ["", "  "]])
def test_a_blank_medication_name_is_not_reported_as_an_unreadable_one(blank) -> None:
    """A row with no name at all is nothing to tell the clinician to go and read."""
    assert check_unevaluated_medications(SafetyContext(unresolved_current_meds=blank)) == []


def test_the_flag_names_what_could_not_be_read_and_does_not_block() -> None:
    """Warning, never a hard block — and it must name the entry, since the clinician's next
    action is to go and read that line on the chart.

    Blocking every prescription on a chart with one unreadable medication line would be
    clinically wrong; saying nothing is worse. The middle is a flag that says which line.
    """
    ctx = SafetyContext(unresolved_current_meds=[_GARBLED, "Tab. ???", _GARBLED])

    flags = check_unevaluated_medications(ctx)

    assert len(flags) == 1, "one statement about the chart, not one per unreadable row"
    flag = flags[0]
    assert flag.check_type == "unevaluated_medication"
    assert flag.severity == "warning"
    assert flag.is_hard_block is False
    assert flag.details["unresolved_medications"] == sorted({_GARBLED, "Tab. ???"})
    assert flag.details["evaluated"] is False
    assert _GARBLED in flag.summary
    # The distinction the whole flag exists to draw.
    assert "not the same as" in flag.summary


def test_the_flag_is_not_folded_into_the_per_drug_evaluation() -> None:
    """``evaluate_drug_safety`` answers about the proposed drug; this is about the chart.

    ``screen_text`` runs it once per drug a guideline sentence names and ``active_flags`` once
    per current medication, so folding it in would repeat one chart-level warning several times
    in a single response.
    """
    # ``age_years`` is given so the assertion below stays an empty list: aspirin carries a
    # paediatric caution, and an age-based check runs against a record with no date of birth by
    # reporting itself unevaluated. That flag would be correct here and unrelated to what this
    # test is about, so the chart is made complete on the age axis instead of the assertion being
    # loosened to tolerate it.
    ctx = SafetyContext(unresolved_current_meds=[_GARBLED], age_years=45)

    flags = evaluate_drug_safety(DrugRef("ASP-75", "Aspirin", "Antiplatelet"), ctx)

    assert [f.check_type for f in flags] == []


# --- through the service ---------------------------------------------------------------------


async def test_an_unreadable_current_medication_reaches_the_context_as_unresolved(db) -> None:
    """It is dropped from ``current_meds`` — and carried out alongside it, which is the fix.

    Dropping it is correct. Dropping it without trace is what made "no rules matched" and "no
    rules ran" the same answer.
    """
    _, patient = await _account_and_patient(db)
    await _add_unresolvable_med(db, patient)

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.current_meds == []
    assert ctx.unresolved_current_meds == [_GARBLED]


async def test_a_resolvable_medication_is_not_reported_as_unreadable(db) -> None:
    """The fallback name-resolution path still works, and does not now flag what it resolved."""
    _, patient = await _account_and_patient(db)
    # Unlinked, but the brand name resolves through the vocabulary to its INN.
    await _add_unresolvable_med(db, patient, "Glycomet")

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert [m.generic_name for m in ctx.current_meds] == ["Metformin"]
    assert ctx.unresolved_current_meds == []


async def test_checking_a_drug_against_an_unreadable_chart_says_so(db) -> None:
    """The whole point, end to end.

    Warfarin is on the chart under a name nothing resolves to; the clinician proposes aspirin.
    The interaction rule is never looked up, because the drug it would fire against is not in
    ``current_meds`` — so before this the verdict was an empty flag list, indistinguishable
    from a chart with no interacting drug on it at all.
    """
    account, patient = await _account_and_patient(db)
    await _add_unresolvable_med(db, patient)

    _vocab_row, ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Aspirin",
    )

    assert ctx.current_meds == []
    unevaluated = [f for f in flags if f.check_type == "unevaluated_medication"]
    assert len(unevaluated) == 1
    assert _GARBLED in unevaluated[0].details["unresolved_medications"]
    # Still not a block: the clinician decides, having been told what was not read.
    assert unevaluated[0].is_hard_block is False


async def test_the_unevaluated_flag_is_persisted_to_the_audit_trail(db) -> None:
    """It is a check result like any other, so it is written as one.

    "The system told me part of the chart was unreadable" has to be answerable from the record
    afterwards, not only visible in the response at the time.
    """
    from app.models.drug_safety_check import DrugSafetyCheck

    account, patient = await _account_and_patient(db)
    await _add_unresolvable_med(db, patient)

    await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Aspirin",
    )

    rows = (
        (
            await db.execute(
                select(DrugSafetyCheck).where(
                    DrugSafetyCheck.patient_id == patient.id,
                    DrugSafetyCheck.check_type == "unevaluated_medication",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].severity == "warning"
    assert rows[0].details["unresolved_medications"] == [_GARBLED]


async def test_the_flag_is_raised_once_per_chart_not_once_per_named_drug(db) -> None:
    """``screen_text`` evaluates every drug a guideline sentence names against one context.

    It must not put the same chart-level warning on the card once per drug, which is why the
    check is appended by the callers that answer about a chart rather than folded into
    ``evaluate_drug_safety``.
    """
    _, patient = await _account_and_patient(db)
    await _add_unresolvable_med(db, patient)
    service = SafetyService(db)

    screened = await service.screen_text(patient.id, "Consider Metformin or Glimepiride.")

    assert [f for f in screened if f.check_type == "unevaluated_medication"] == []
    assert len(await service.chart_completeness_flags(patient.id)) == 1


# --- through the API ---------------------------------------------------------------------


async def _chart_with_unreadable_lines(auth_client, db, *names: str) -> dict:
    """A patient owned by the authenticated account, carrying medication rows nothing resolves.

    Inserted directly rather than posted: clinical content only enters the graph through an
    approved document extraction, and what is under test here is the safety response, not the
    ingestion path.
    """
    from tests.conftest import create_patient

    patient = await create_patient(auth_client)
    for name in names:
        db.add(
            MedicationEvent(
                patient_id=uuid.UUID(patient["id"]),
                drug_vocabulary_id=None,
                generic_name=name,
                event_type="continue",
                is_current=True,
            )
        )
    await db.commit()
    return patient


async def test_the_check_response_counts_what_it_could_not_read(auth_client, db) -> None:
    """``checked_against`` reports what the check ran against, and it under-reported.

    ``current_medications`` counts only the rows that resolved, so a chart losing a line to the
    resolver showed a smaller medication list with nothing saying why.
    """
    patient = await _chart_with_unreadable_lines(auth_client, db, _GARBLED)

    response = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Aspirin"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checked_against"]["unresolved_medications"] == 1
    assert body["checked_against"]["current_medications"] == 0
    assert "unevaluated_medication" in {f["check_type"] for f in body["flags"]}
    # A warning, so the proposal is not blocked by it.
    assert body["is_blocked"] is False


async def test_the_flags_endpoint_reports_the_unreadable_line_once(auth_client, db) -> None:
    """``GET ../flags`` is one flat list for the whole chart, so this belongs in it once."""
    patient = await _chart_with_unreadable_lines(auth_client, db, _GARBLED, "Tab. ??? 10mg")

    response = await auth_client.get(f"/api/v1/patients/{patient['id']}/drug-safety/flags")

    assert response.status_code == 200, response.text
    unevaluated = [
        f for f in response.json()["flags"] if f["check_type"] == "unevaluated_medication"
    ]
    assert len(unevaluated) == 1
    assert sorted(unevaluated[0]["details"]["unresolved_medications"]) == sorted(
        [_GARBLED, "Tab. ??? 10mg"]
    )
