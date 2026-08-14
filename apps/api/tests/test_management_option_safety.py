"""Guideline management options are screened against the patient before the clinician sees them.

Critical Safety Rule #3 says a suggested medication that conflicts with a documented allergy or a
known contraindication must be hard-blocked. Until this existed, the rule was enforced only for
drugs a clinician *proposed* through ``POST /drug-safety/check``, and for drugs already on the
chart. The reasoning pipeline's own output was not screened at all: ``guideline_rag`` drafts
management options from retrieved guideline text, and the deterministic safety node ran
``evaluate_safety("")`` — the patient's current medications — and nothing else.

That gap is reachable from the shipped corpus, not from a contrived one. ``data/guidelines``
contains "Paracetamol is preferred for fever" (dengue) and "Metformin is the preferred first-line
pharmacotherapy ... unless contraindicated (e.g. eGFR below 30)" (type 2 diabetes), and both drugs
are in ``data/drugs``. So a patient with a documented paracetamol allergy could be shown a
guideline-cited option recommending paracetamol, presented exactly like one that had been checked.

The screen is deliberately whole-name matching only, never the fuzzy tier: fuzzy matching earns
its place on a name a human typed, but run across a paragraph of prose it invents conflicts, and a
spurious hard block on a correct recommendation teaches clinicians to click past hard blocks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.agents import graph
from app.agents.context import ReasoningContext
from app.agents.state import CaseState, GuidelineChunkRef, ManagementOption
from app.agents.synthesis import build_suggestions
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.models.derived_marker import DerivedMarker
from app.models.drug_vocabulary import DrugVocabulary
from app.models.patient import Patient
from app.models.user import Account
from app.services.reasoning_service import ReasoningService
from app.services.safety_service import SafetyService

pytestmark = pytest.mark.asyncio

# Straight out of data/guidelines/icmr_stw.json — the text a clinician actually sees, in the
# shape guideline_rag's deterministic grounding path builds it.
DENGUE_OPTION = (
    "Guidelines support considering: Supportive management — Management is largely supportive "
    "with careful isotonic fluid therapy titrated to the plasma-leak phase. Paracetamol is "
    "preferred for fever; NSAIDs and aspirin are best avoided because of bleeding risk."
)
DIABETES_OPTION = (
    "Guidelines support considering: Pharmacotherapy — Metformin is the preferred first-line "
    "pharmacotherapy alongside lifestyle modification, unless contraindicated (e.g. eGFR below "
    "30)."
)


async def _account_and_patient(db) -> tuple[uuid.UUID, Patient]:
    account_id = (await db.execute(select(Account.id))).scalars().first()
    if account_id is None:
        account = Account(email="mgmt@example.com", password_hash="x", display_name="Dr Mgmt")
        db.add(account)
        await db.flush()
        account_id = account.id
    patient = Patient(
        account_id=account_id,
        full_name="Management Patient",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account_id, patient


async def _vocab(db, reference_id: str) -> DrugVocabulary:
    return (
        (
            await db.execute(
                select(DrugVocabulary).where(DrugVocabulary.reference_id == reference_id)
            )
        )
        .scalars()
        .first()
    )


async def _allergy(db, patient: Patient, *, allergen_name: str, reference_id: str) -> None:
    vocab = await _vocab(db, reference_id)
    assert vocab is not None, f"seed vocabulary is missing {reference_id}"
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name=allergen_name,
            allergen_type="drug",
            status="active",
            drug_vocabulary_id=vocab.id,
        )
    )
    await db.flush()


# --------------------------------------------------------------------------- the screen itself


async def test_a_documented_allergy_hard_blocks_the_drug_a_guideline_option_names(db):
    """The dengue workflow recommends paracetamol; this patient is allergic to it."""
    _, patient = await _account_and_patient(db)
    await _allergy(db, patient, allergen_name="Paracetamol", reference_id="PCM-500")

    flags = await SafetyService(db).screen_text(patient.id, DENGUE_OPTION)

    blocks = [f for f in flags if f.is_hard_block and f.check_type == "allergy_conflict"]
    assert blocks, (
        "a guideline option naming paracetamol reached a paracetamol-allergic patient with no "
        f"hard block; flags were {[(f.check_type, f.severity) for f in flags]}"
    )
    assert "Paracetamol" in blocks[0].summary


async def test_an_indian_brand_allergy_still_blocks_the_generic_the_guideline_names(db):
    """ "Crocin" must reach Paracetamol through the vocabulary, not by string equality.

    CLAUDE.md pitfall #4. The allergy is recorded under the brand the patient reported; the
    guideline names the INN. Nothing matches textually — the vocabulary is what connects them.
    """
    _, patient = await _account_and_patient(db)
    await _allergy(db, patient, allergen_name="Crocin", reference_id="PCM-500")

    flags = await SafetyService(db).screen_text(patient.id, DENGUE_OPTION)

    assert any(f.is_hard_block and f.check_type == "allergy_conflict" for f in flags)


async def test_a_renal_contraindication_fires_on_the_drug_a_guideline_option_names(db):
    """The diabetes workflow recommends metformin; this patient's eGFR is 22.

    The guideline text itself says "unless contraindicated (e.g. eGFR below 30)" — which is
    exactly the judgement the system is meant to make for this patient, and did not.
    """
    _, patient = await _account_and_patient(db)
    db.add(
        DerivedMarker(
            patient_id=patient.id,
            marker_name="eGFR",
            value_numeric=Decimal("22"),
            unit="mL/min/1.73m2",
            formula_name="CKD-EPI 2021",
            formula_version="2021",
            input_values={},
            computed_at=datetime.now(UTC),
        )
    )
    await db.flush()

    flags = await SafetyService(db).screen_text(patient.id, DIABETES_OPTION)

    assert any(f.is_hard_block and f.check_type == "renal_dose" for f in flags), (
        "metformin named in a management option was not evaluated against the patient's eGFR; "
        f"flags were {[(f.check_type, f.severity) for f in flags]}"
    )


async def test_every_drug_an_option_names_is_evaluated_not_just_the_first(db):
    """A guideline sentence naming several drugs must not be reduced to one of them.

    This is the opposite call from ``_reject_if_multiple_drugs``, which refuses a multi-drug
    string at the clinician-proposal boundary. There, picking one of two is a wrong answer that
    reads as a completed check. Here, naming several drugs is what guideline prose normally
    does, and refusing would turn "this option mentions two drugs" into no check at all.
    """
    _, patient = await _account_and_patient(db)
    await _allergy(db, patient, allergen_name="Aspirin", reference_id="ASP-75")

    # Paracetamol is named first in the sentence; aspirin — the conflict — is named last.
    flags = await SafetyService(db).screen_text(patient.id, DENGUE_OPTION)

    assert any(f.is_hard_block and "Aspirin" in f.summary for f in flags), (
        f"the later-named drug was dropped; flags were {[f.summary for f in flags]}"
    )


async def test_text_naming_no_known_drug_is_screened_clean(db):
    _, patient = await _account_and_patient(db)
    await _allergy(db, patient, allergen_name="Paracetamol", reference_id="PCM-500")

    assert await SafetyService(db).screen_text(patient.id, "Advise rest and oral fluids.") == []
    assert await SafetyService(db).screen_text(patient.id, "") == []


async def test_a_near_miss_spelling_is_not_fuzzy_matched_into_a_conflict(db):
    """Prose is not a drug name field. Only whole-name matches may raise a conflict here.

    ``resolve`` accepts a fuzzy match at 86, which is right for "Tab. Crocin 500 BD" — a name a
    human typed. Applied to a paragraph it would manufacture hard blocks against drugs the text
    never named, and a hard block a clinician learns to dismiss is worse than no hard block.
    """
    _, patient = await _account_and_patient(db)
    await _allergy(db, patient, allergen_name="Paracetamol", reference_id="PCM-500")

    flags = await SafetyService(db).screen_text(
        patient.id, "Guidelines support considering paracetamolol for fever."
    )
    assert flags == []


# ----------------------------------------------------------------- the node, and what it emits


def _ctx(evaluate) -> tuple[ReasoningContext, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    return ReasoningContext(evaluate_safety=evaluate, emit=emit), events


def _option(text: str) -> ManagementOption:
    return ManagementOption(
        text=text,
        citations=[
            GuidelineChunkRef(
                section_id="STW-DENGUE-1",
                source="icmr",
                document_title="Dengue",
                heading="Supportive management",
                snippet=text[:80],
                score=0.9,
                corpus_version="v1",
            )
        ],
    )


async def test_the_safety_node_screens_every_management_option(db):
    """The node asks the evaluator about each option's text, not only about current meds."""
    asked: list[str] = []

    def evaluate(text: str) -> list[dict]:
        asked.append(text)
        if "Paracetamol" not in text:
            return []
        return [
            {
                "check_type": "allergy_conflict",
                "severity": "hard_block",
                "is_hard_block": True,
                "summary": "Documented allergy to Paracetamol conflicts with Paracetamol.",
                "details": {},
            }
        ]

    ctx, events = _ctx(evaluate)
    state = CaseState(patient_id="p1", presenting_complaint="Fever with rash for four days")
    state.management_options = [_option(DENGUE_OPTION), _option("Advise rest and oral fluids.")]

    await graph.drug_safety_check(state, ctx)

    assert "" in asked, "the current-medication pass must still run"
    assert DENGUE_OPTION in asked, "the management option was never screened"
    assert state.management_options[0].has_hard_block()
    assert not state.management_options[1].safety_flags
    assert [b.check_type for b in state.hard_blocks] == ["allergy_conflict"]

    emitted = dict(events)["drug_safety"]
    assert emitted["management_flags"], "the Reasoning Theatre stream must carry the conflict"


async def test_one_conflict_is_reported_once_even_when_both_passes_find_it(db):
    """An option naming a drug the patient is already on raises the same block twice."""
    duplicate = {
        "check_type": "allergy_conflict",
        "severity": "hard_block",
        "is_hard_block": True,
        "summary": "Documented allergy to Paracetamol conflicts with Paracetamol.",
        "details": {},
    }

    ctx, _ = _ctx(lambda _text: [duplicate])
    state = CaseState(patient_id="p1", presenting_complaint="Fever")
    state.management_options = [_option(DENGUE_OPTION)]

    await graph.drug_safety_check(state, ctx)

    assert len(state.hard_blocks) == 1, "the clinician must not see the same block twice"


async def test_a_conflicted_option_is_escalated_and_labelled_in_the_synthesis_payload(db):
    """A conflict with the patient's own record outranks how well the option is cited.

    A perfectly-cited guideline recommendation for a drug the patient is allergic to is the most
    dangerous output this pipeline can produce, precisely because its presentation says it was
    checked. It must not go out at the case tier wearing the ordinary title.
    """
    state = CaseState(patient_id="p1", presenting_complaint="Fever")
    state.autonomy_tier = "suggestive"
    clean, conflicted = _option("Advise rest and oral fluids."), _option(DENGUE_OPTION)
    conflicted.safety_flags = [
        {
            "check_type": "allergy_conflict",
            "severity": "hard_block",
            "is_hard_block": True,
            "summary": "Documented allergy to Paracetamol conflicts with Paracetamol.",
            "details": {},
        }
    ]
    state.management_options = [clean, conflicted]

    management = [s for s in build_suggestions(state) if s["output_type"] == "management"]
    by_body = {s["body"]: s for s in management}

    assert by_body[clean.text]["autonomy_tier"] == "suggestive"
    assert by_body[clean.text]["title"] == "Guideline-supported management option"

    flagged = by_body[conflicted.text]
    assert flagged["autonomy_tier"] == "flag_for_review"
    assert flagged["is_hard_block"] is True
    assert "conflicts with this patient's record" in flagged["title"]
    assert flagged["evidence"]["safety_flags"] == conflicted.safety_flags
    # The citations are still shown: the guideline said what it said, and hiding the grounding
    # would leave the clinician unable to judge the conflict.
    assert flagged["citations"]


# ------------------------------------------------------------------------------- wired for real


async def test_the_injected_evaluator_answers_both_questions(db):
    """``ReasoningService`` must return an evaluator that actually screens text.

    The seam took a drug name from the beginning and returned the current-medication constant
    regardless of it, so this is the assertion that keeps it wired.
    """
    account_id, patient = await _account_and_patient(db)
    await _allergy(db, patient, allergen_name="Paracetamol", reference_id="PCM-500")
    db.add(Condition(patient_id=patient.id, condition_name="Dengue fever", status="active"))
    await db.flush()

    evaluate = await ReasoningService(db)._safety_evaluator(account_id, patient.id)

    from app.agents.context import resolve_safety

    current = await resolve_safety(evaluate(""))
    screened = await resolve_safety(evaluate(DENGUE_OPTION))

    assert current == [], "no current medications, so nothing to flag on the first pass"
    assert any(f["is_hard_block"] for f in screened), (
        "the evaluator ignored the option text it was handed"
    )
