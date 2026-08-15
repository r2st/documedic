"""What the chart documented about a reaction has to reach the flag that blocks on it.

``allergies.severity`` and ``allergies.reaction_description`` have been columns since the first
migration, and until now nothing read them. The safety engine loaded an allergy as a name, a
reference id and a drug class, so every allergy conflict this product has ever raised said
"documented allergy to X" and stopped there — the clinician deciding whether to override had to
leave the safety screen and go and read the allergy list to find out whether the last reaction
was a rash or an airway.

Two consequences, and the second is a behaviour change:

* A hard block a clinician cannot weigh on the screen it appears on is a hard block they weigh
  from memory. The severity and the reaction are now quoted in the flag's own sentence.
* Cross-reactivity is graded here as dismissible because it is *uncommon* — a few percent for
  the penicillin/cephalosporin family. That number says nothing about what is being risked, and
  a few percent of anaphylaxis is not the decision a few percent of a rash is. A documented
  ``life_threatening`` reaction now promotes the cross-reactivity finding to a hard block, which
  does not stop the prescription: it routes it through the override path, so the reasoning
  lands on the chart.

The direct and same-class matches are unchanged in grade. They are hard blocks at every
documented severity including ``mild`` and including no severity at all — Critical Safety Rule
#3 does not grade its blocks by how bad the first exposure happened to be, and a mild rash
recorded once is not a promise about the next one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.safety import (
    DrugRef,
    PatientAllergy,
    SafetyContext,
    check_allergies,
    evaluate_drug_safety,
)
from app.models.allergy import Allergy
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

pytestmark = pytest.mark.asyncio

# A penicillin the vocabulary knows, and a cephalosporin: the canonical cross-reactive pair.
_AMOXICILLIN = DrugRef("AMOX-500", "Amoxicillin", "Penicillin")
_CEFTRIAXONE = DrugRef("CEFT-1G", "Ceftriaxone", "Cephalosporin")


def _allergy(**overrides) -> PatientAllergy:
    fields = {
        "allergen_name": "Penicillin",
        "drug_class": "Penicillin",
        "allergy_id": "a1",
    }
    fields.update(overrides)
    return PatientAllergy(**fields)


# --- the documented reaction reaches the flag ------------------------------------------------


async def test_a_direct_match_quotes_the_severity_and_the_reaction() -> None:
    """The one fact the override decision turns on, in the sentence that announces the block."""
    ctx = SafetyContext(
        allergies=[
            _allergy(
                allergen_name="Amoxicillin",
                drug_reference_id="AMOX-500",
                severity="life_threatening",
                reaction="anaphylaxis, airway involvement",
            )
        ]
    )

    flags = check_allergies(_AMOXICILLIN, ctx)

    assert len(flags) == 1
    flag = flags[0]
    assert flag.is_hard_block is True
    assert "life-threatening: anaphylaxis, airway involvement" in flag.summary
    assert flag.details["documented_severity"] == "life_threatening"
    assert flag.details["documented_reaction"] == "anaphylaxis, airway involvement"


async def test_a_severity_with_no_reaction_text_still_reads() -> None:
    ctx = SafetyContext(
        allergies=[
            _allergy(allergen_name="Amoxicillin", drug_reference_id="AMOX-500", severity="moderate")
        ]
    )

    flag = check_allergies(_AMOXICILLIN, ctx)[0]

    assert "(moderate)" in flag.summary
    assert "documented_reaction" not in flag.details


async def test_a_reaction_with_no_severity_still_reads() -> None:
    ctx = SafetyContext(
        allergies=[
            _allergy(
                allergen_name="Amoxicillin",
                drug_reference_id="AMOX-500",
                reaction="urticarial rash",
            )
        ]
    )

    flag = check_allergies(_AMOXICILLIN, ctx)[0]

    assert "(documented reaction: urticarial rash)" in flag.summary
    assert "documented_severity" not in flag.details


async def test_an_allergy_documented_as_a_bare_name_reads_exactly_as_before() -> None:
    """No empty parenthesis: nothing was recorded, so the flag must not imply anything was."""
    ctx = SafetyContext(
        allergies=[_allergy(allergen_name="Amoxicillin", drug_reference_id="AMOX-500")]
    )

    flag = check_allergies(_AMOXICILLIN, ctx)[0]

    assert "()" not in flag.summary
    assert "Documented allergy to Amoxicillin conflicts with" in flag.summary
    assert "documented_severity" not in flag.details
    assert "documented_reaction" not in flag.details


@pytest.mark.parametrize("blank", ["", "   ", None])
async def test_a_blank_reaction_is_not_reported_as_one(blank) -> None:
    ctx = SafetyContext(
        allergies=[
            _allergy(allergen_name="Amoxicillin", drug_reference_id="AMOX-500", reaction=blank)
        ]
    )

    flag = check_allergies(_AMOXICILLIN, ctx)[0]

    assert "documented_reaction" not in flag.details


async def test_an_unrecorded_severity_is_named_as_unrecorded() -> None:
    """``unknown`` is a value ``ck_allergies_severity`` admits, and it means nobody looked."""
    ctx = SafetyContext(
        allergies=[
            _allergy(allergen_name="Amoxicillin", drug_reference_id="AMOX-500", severity="unknown")
        ]
    )

    flag = check_allergies(_AMOXICILLIN, ctx)[0]

    assert "severity not recorded" in flag.summary


# --- the grade the documented severity buys ---------------------------------------------------


async def test_cross_reactivity_stays_dismissible_at_ordinary_severities() -> None:
    """The alarm-fatigue argument in the module docstring, pinned in both directions."""
    for severity in (None, "mild", "moderate", "severe", "unknown"):
        ctx = SafetyContext(allergies=[_allergy(severity=severity)])

        flags = check_allergies(_CEFTRIAXONE, ctx)

        assert len(flags) == 1, severity
        assert flags[0].severity == "critical", severity
        assert flags[0].is_hard_block is False, severity
        assert "considering an alternative" in flags[0].summary, severity


async def test_a_life_threatening_reaction_makes_cross_reactivity_a_hard_block() -> None:
    ctx = SafetyContext(allergies=[_allergy(severity="life_threatening", reaction="anaphylaxis")])

    flags = check_allergies(_CEFTRIAXONE, ctx)

    assert len(flags) == 1
    flag = flags[0]
    assert flag.severity == "hard_block"
    assert flag.is_hard_block is True
    assert flag.details["match_type"] == "cross_reactivity"
    assert "life-threatening" in flag.summary
    assert "recorded reasoning is required" in flag.summary


async def test_the_promoted_block_still_names_the_class_pair() -> None:
    """Promoting the grade must not cost the explanation of *why* the two are related."""
    ctx = SafetyContext(allergies=[_allergy(severity="life_threatening")])

    flag = check_allergies(_CEFTRIAXONE, ctx)[0]

    assert flag.details["allergen_class"] == "Penicillin"
    assert flag.details["proposed_drug_class"] == "Cephalosporin"
    assert "recognised cross-reactivity" in flag.summary


@pytest.mark.parametrize("severity", [None, "mild", "unknown", "life_threatening"])
async def test_a_direct_match_hard_blocks_at_every_documented_severity(severity) -> None:
    """Rule #3 does not grade its blocks by how bad the first exposure was."""
    ctx = SafetyContext(
        allergies=[
            _allergy(allergen_name="Amoxicillin", drug_reference_id="AMOX-500", severity=severity)
        ]
    )

    flags = evaluate_drug_safety(_AMOXICILLIN, ctx)

    assert [f.is_hard_block for f in flags] == [True]


async def test_severity_is_read_case_and_space_insensitively() -> None:
    """The column is checked, not normalised, and OCR-fed writers are not tidy."""
    ctx = SafetyContext(allergies=[_allergy(severity="  Life_Threatening ")])

    assert check_allergies(_CEFTRIAXONE, ctx)[0].is_hard_block is True


async def test_a_severity_the_engine_does_not_know_does_not_promote() -> None:
    """A value outside the constraint's vocabulary is not evidence of anything."""
    ctx = SafetyContext(allergies=[_allergy(severity="catastrophic")])

    flags = check_allergies(_CEFTRIAXONE, ctx)

    assert flags[0].is_hard_block is False
    assert flags[0].details["documented_severity"] == "catastrophic"


# --- through the service ----------------------------------------------------------------------


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"allergy-sev-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Allergy Severity Patient",
        sex="female",
        date_of_birth=datetime(1980, 2, 2).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def test_the_service_carries_severity_and_reaction_into_the_context(db) -> None:
    """The columns were loaded and dropped on the floor; this is the wire that was missing."""
    _, patient = await _account_and_patient(db)
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name="Penicillin",
            allergen_type="drug",
            status="active",
            severity="life_threatening",
            reaction_description="anaphylaxis",
        )
    )
    await db.flush()

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert [(a.severity, a.reaction) for a in ctx.allergies] == [
        ("life_threatening", "anaphylaxis")
    ]


async def test_an_unresolvable_allergen_still_carries_its_documented_severity(db) -> None:
    """How badly the patient reacted is on the row and does not depend on the vocabulary."""
    _, patient = await _account_and_patient(db)
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name="Zyxomycin (illegible)",
            allergen_type="drug",
            status="active",
            severity="severe",
            reaction_description="angioedema",
        )
    )
    await db.flush()

    ctx = await SafetyService(db)._patient_facts(patient.id)

    assert ctx.unresolved_allergies == ["Zyxomycin (illegible)"]
    assert [(a.severity, a.reaction) for a in ctx.allergies] == [("severe", "angioedema")]
