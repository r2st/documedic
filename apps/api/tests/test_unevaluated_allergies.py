"""A documented allergy the vocabulary cannot identify must not read as no allergy.

``check_allergies`` fires on three things: the allergy's resolved reference id, its resolved
drug class, and its raw charted name compared to the proposed drug's INN. An allergen the
vocabulary cannot identify — an Indian brand the seed data has not been given, a combination
product, an OCR'd scrawl — has neither of the first two, so only the third is left, and the
third only fires when the chart happens to have written the generic name.

So a chart saying "allergic to Calpol 650" and a proposal of paracetamol produced exactly the
empty flag list of a chart with no allergies on it: Critical Safety Rule #3, the one rule that
admits no override without documented reasoning, failing open because nobody seeded the brand.
This is CLAUDE.md pitfall #4 ("an allergy to Crocin must match against Paracetamol") in the one
direction the vocabulary pipeline cannot itself close.

The allergy counterpart of ``test_unevaluated_medications``, and the higher-stakes half.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.safety import (
    DrugRef,
    PatientAllergy,
    SafetyContext,
    check_unevaluated_allergies,
    evaluate_drug_safety,
)
from app.models.allergy import Allergy
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

from .conftest import create_patient

# A real Indian paracetamol brand that the shipped vocabulary has not been seeded with, written
# the way a prescription writes it.
_UNSEEDED_BRAND = "Calpol 650"

pytestmark = pytest.mark.asyncio


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"unev-allergy-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Unevaluated Allergy Patient",
        sex="female",
        date_of_birth=datetime(1974, 4, 8).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _add_allergy(db, patient: Patient, name: str, *, status: str = "active") -> None:
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name=name,
            allergen_type="drug",
            status=status,
            drug_vocabulary_id=None,
        )
    )
    await db.flush()


# --- the pure check -------------------------------------------------------------------------


@pytest.mark.parametrize("ctx", [SafetyContext(), SafetyContext(unresolved_allergies=[])])
async def test_a_chart_with_nothing_unidentified_raises_nothing(ctx: SafetyContext) -> None:
    """The flag reports incompleteness, so a complete chart must stay silent."""
    assert check_unevaluated_allergies(ctx) == []


@pytest.mark.parametrize("blank", [[""], ["   "], ["", "  "]])
async def test_a_blank_allergen_name_is_not_reported(blank) -> None:
    assert check_unevaluated_allergies(SafetyContext(unresolved_allergies=blank)) == []


async def test_the_flag_names_the_allergen_and_does_not_block() -> None:
    """It must name the entry: the clinician's next action is to go and read that line."""
    ctx = SafetyContext(unresolved_allergies=[_UNSEEDED_BRAND, "??? (illegible)", _UNSEEDED_BRAND])

    flags = check_unevaluated_allergies(ctx)

    assert len(flags) == 1, "one statement about the chart, not one per allergen"
    flag = flags[0]
    assert flag.check_type == "unevaluated_allergy"
    assert flag.severity == "warning"
    assert flag.is_hard_block is False
    assert flag.details["unresolved_allergies"] == sorted({_UNSEEDED_BRAND, "??? (illegible)"})
    assert flag.details["evaluated"] is False
    assert _UNSEEDED_BRAND in flag.summary
    assert "not the same as" in flag.summary


async def test_the_flag_is_not_folded_into_the_per_drug_evaluation() -> None:
    """A statement about the chart, so the callers that answer about a chart append it once."""
    ctx = SafetyContext(unresolved_allergies=[_UNSEEDED_BRAND])

    assert evaluate_drug_safety(DrugRef("PARA-500", "Paracetamol", "Analgesic"), ctx) == []


async def test_an_identified_allergy_still_hard_blocks() -> None:
    """The fix must not dilute the block it exists to protect."""
    ctx = SafetyContext(
        allergies=[
            PatientAllergy(
                allergen_name="Crocin",
                drug_reference_id="PARA-500",
                drug_class="Analgesic",
                allergy_id="a1",
            )
        ]
    )

    flags = evaluate_drug_safety(DrugRef("PARA-500", "Paracetamol", "Analgesic"), ctx)

    assert [f.is_hard_block for f in flags] == [True]


# --- through the service ---------------------------------------------------------------------


async def test_an_unidentifiable_allergen_reaches_the_context_as_unresolved(db) -> None:
    _, patient = await _account_and_patient(db)
    await _add_allergy(db, patient, _UNSEEDED_BRAND)

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert [a.drug_reference_id for a in ctx.allergies] == [None]
    assert ctx.unresolved_allergies == [_UNSEEDED_BRAND]


async def test_a_resolvable_brand_allergy_is_not_reported_as_unidentified(db) -> None:
    """The vocabulary pipeline still does its job, and what it resolves is not flagged."""
    _, patient = await _account_and_patient(db)
    await _add_allergy(db, patient, "Crocin")

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.unresolved_allergies == []
    assert ctx.allergies[0].drug_reference_id is not None


async def test_a_non_drug_allergen_is_not_reported_as_unidentified(db) -> None:
    """A dust allergy was never going to resolve to a drug, and saying so is noise."""
    _, patient = await _account_and_patient(db)
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name="House dust mite",
            allergen_type="environmental",
            status="active",
        )
    )
    await db.flush()

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.unresolved_allergies == []


async def test_an_allergy_of_unknown_status_is_still_read(db) -> None:
    """Of the four statuses only resolved/refuted say the allergy is not a live concern.

    An allergy nobody has been able to confirm is exactly the one a hard block exists for;
    excluding it is the same fail-open shape as an allergen that cannot be identified.
    """
    _, patient = await _account_and_patient(db)
    await _add_allergy(db, patient, "Crocin", status="unknown")

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert [a.allergen_name for a in ctx.allergies] == ["Crocin"]


@pytest.mark.parametrize("status", ["resolved", "refuted"])
async def test_a_resolved_or_refuted_allergy_stays_out(db, status: str) -> None:
    _, patient = await _account_and_patient(db)
    await _add_allergy(db, patient, "Crocin", status=status)

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.allergies == []


async def test_checking_a_drug_against_an_unidentified_allergy_says_so(db) -> None:
    """The whole point, end to end: paracetamol proposed to a patient allergic to Calpol."""
    account, patient = await _account_and_patient(db)
    await _add_allergy(db, patient, _UNSEEDED_BRAND)

    _vocab, ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Paracetamol",
    )

    unevaluated = [f for f in flags if f.check_type == "unevaluated_allergy"]
    assert len(unevaluated) == 1
    assert _UNSEEDED_BRAND in unevaluated[0].details["unresolved_allergies"]
    assert unevaluated[0].is_hard_block is False


async def test_the_unevaluated_allergy_flag_is_persisted(db) -> None:
    """ "The system told me an allergy was not cross-checked" has to survive the request."""
    from app.models.drug_safety_check import DrugSafetyCheck

    account, patient = await _account_and_patient(db)
    await _add_allergy(db, patient, _UNSEEDED_BRAND)

    await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Paracetamol",
    )

    rows = (
        (
            await db.execute(
                select(DrugSafetyCheck).where(
                    DrugSafetyCheck.patient_id == patient.id,
                    DrugSafetyCheck.check_type == "unevaluated_allergy",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].severity == "warning"
    assert rows[0].details["unresolved_allergies"] == [_UNSEEDED_BRAND]


async def test_the_flag_is_raised_once_per_chart_not_once_per_named_drug(db) -> None:
    """``screen_text`` evaluates every drug a guideline sentence names against one context."""
    _, patient = await _account_and_patient(db)
    await _add_allergy(db, patient, _UNSEEDED_BRAND)
    service = SafetyService(db)

    screened = await service.screen_text(patient.id, "Consider Metformin or Glimepiride.")

    assert [f for f in screened if f.check_type == "unevaluated_allergy"] == []
    completeness = await service.chart_completeness_flags(patient.id)
    assert [f.check_type for f in completeness] == ["unevaluated_allergy"]


# --- through the API --------------------------------------------------------------------------


async def _chart_with_unidentified_allergy(auth_client, db, *names: str) -> dict:
    patient = await create_patient(auth_client)
    for name in names:
        db.add(
            Allergy(
                patient_id=uuid.UUID(patient["id"]),
                allergen_name=name,
                allergen_type="drug",
                status="active",
            )
        )
    await db.commit()
    return patient


async def test_the_check_response_counts_the_unidentified_allergies(auth_client, db) -> None:
    patient = await _chart_with_unidentified_allergy(auth_client, db, _UNSEEDED_BRAND)

    response = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Paracetamol"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checked_against"]["allergies"] == 1
    assert body["checked_against"]["unresolved_allergies"] == 1
    assert "unevaluated_allergy" in {f["check_type"] for f in body["flags"]}
    assert body["is_blocked"] is False


async def test_the_flags_endpoint_reports_the_unidentified_allergy_once(auth_client, db) -> None:
    patient = await _chart_with_unidentified_allergy(
        auth_client, db, _UNSEEDED_BRAND, "??? (illegible)"
    )

    response = await auth_client.get(f"/api/v1/patients/{patient['id']}/drug-safety/flags")

    assert response.status_code == 200, response.text
    unevaluated = [f for f in response.json()["flags"] if f["check_type"] == "unevaluated_allergy"]
    assert len(unevaluated) == 1
    assert sorted(unevaluated[0]["details"]["unresolved_allergies"]) == sorted(
        [_UNSEEDED_BRAND, "??? (illegible)"]
    )
