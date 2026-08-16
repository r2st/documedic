"""Reconciling a whole medication list, and the three findings a per-drug check cannot reach.

``POST ../check`` is a good check of one drug against one chart. This suite is about what that
shape structurally cannot answer, and each of the three has its own section below:

* an **omission** — a charted drug missing from the new list. There is no drug to pass to a
  per-drug check, so this is not a check that failed but one that was never called;
* an **intra-list interaction** — two drugs that are both new. Checked singly against the
  chart, each is clean, because neither is on the chart yet;
* an **intra-list duplicate** — one molecule twice in one list, which is what a list assembled
  from two sources routinely looks like.

The fourth section is the identity model, which is where this would most plausibly go quietly
wrong: aspirin is ASP-75 *and* ASP-150 in this vocabulary, so a reconciliation matching on
reference id alone turns a dose change into a fabricated stop plus a fabricated start.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.med_reconciliation import (
    ChartedCurrentMedication,
    ProposedMedication,
    check_high_risk_omissions,
    check_intra_list_duplicates,
    check_intra_list_interactions,
    proposed_reference_ids,
    reconcile,
    reconcile_medications,
)
from app.core.safety import DrugRef, InteractionRule
from app.models.audit_log import AuditLog
from app.models.medication_event import MedicationEvent
from app.models.patient import Patient
from app.models.user import Account
from app.services.med_reconciliation_service import (
    MAX_PROPOSED_MEDICATIONS,
    MedReconciliationService,
    ProposedLine,
)
from tests.conftest import create_patient

# --- Fixtures for the pure layer ----------------------------------------------------------------

WARFARIN = DrugRef(
    reference_id="WARF-5", generic_name="Warfarin", drug_class="Vitamin K antagonist"
)
ASPIRIN_75 = DrugRef(reference_id="ASP-75", generic_name="Aspirin", drug_class="Antiplatelet")
ASPIRIN_150 = DrugRef(reference_id="ASP-150", generic_name="Aspirin", drug_class="Antiplatelet")
CLOPIDOGREL = DrugRef(reference_id="CLO-75", generic_name="Clopidogrel", drug_class="Antiplatelet")
METFORMIN = DrugRef(reference_id="MET-500", generic_name="Metformin", drug_class="Biguanide")
LEVOTHYROXINE = DrugRef(
    reference_id="LT4-50", generic_name="Levothyroxine", drug_class="Thyroid hormone"
)
AMLODIPINE = DrugRef(reference_id="AML-5", generic_name="Amlodipine", drug_class="CCB")
# A fixed-dose combination, exactly as the vocabulary carries one: its own row, plus the
# molecules it contains. Metformin reached only through the component.
GLYCOMET_GP = DrugRef(
    reference_id="MET-GLM-1-500",
    generic_name="Metformin + Glimepiride",
    drug_class="Biguanide + Sulfonylurea",
    components=(METFORMIN, DrugRef(reference_id="GLM-1", generic_name="Glimepiride")),
)

ASPIRIN_WARFARIN = InteractionRule(
    drug_a_reference_id="ASP-75",
    drug_b_reference_id="WARF-5",
    severity="major",
    description="Additive bleeding risk.",
    interaction_id=None,
)


def _p(drug: DrugRef | None, name: str | None = None, **dose) -> ProposedMedication:
    return ProposedMedication(name=name or (drug.generic_name if drug else "?"), drug=drug, **dose)


def _c(drug: DrugRef | None, name: str | None = None, **dose) -> ChartedCurrentMedication:
    return ChartedCurrentMedication(
        name=name or (drug.generic_name if drug else "?"), drug=drug, **dose
    )


def _dispositions(lines) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in lines:
        out.setdefault(line.disposition, []).append(line.label)
    return out


# --- 1. Omission: the finding a per-drug check cannot make ---------------------------------------


def test_a_charted_drug_absent_from_the_proposed_list_is_reported_as_a_stop():
    """The reconciliation error the discipline exists for.

    No per-drug endpoint can produce this. Its input is a drug, and the whole failure is that
    there is no drug — the check was not called, so there is nothing for it to have got wrong.
    """
    lines = reconcile([_p(METFORMIN)], [_c(METFORMIN), _c(WARFARIN)])
    assert _dispositions(lines) == {"continue": ["Metformin"], "stop": ["Warfarin"]}


def test_every_input_line_appears_in_the_output_exactly_once():
    """Totality, which is what makes the table readable as "these two lists, fully compared".

    A reconciliation that consumed a line and emitted nothing would produce the same report
    minus the rows that needed attention.
    """
    proposed = [_p(METFORMIN), _p(ASPIRIN_75), _p(None, "Zerodol-SP")]
    charted = [_c(METFORMIN), _c(WARFARIN), _c(None, "Some unreadable scrawl")]
    lines = reconcile(proposed, charted)

    assert len(lines) == 5, "3 proposed + 2 charted-only"
    assert sorted(line.label for line in lines) == sorted(
        ["Metformin", "Aspirin", "Zerodol-SP", "Warfarin", "Some unreadable scrawl"]
    )


def test_an_omitted_high_risk_drug_is_raised_as_a_flag_as_well_as_a_line():
    """Warfarin that fell off the list sits among nine rows of equal visual weight otherwise."""
    result = reconcile_medications([_p(METFORMIN)], [_c(METFORMIN), _c(WARFARIN)])
    omissions = [f for f in result.flags if f.finding == "high_risk_omission"]
    assert len(omissions) == 1
    assert omissions[0].severity == "critical"
    assert omissions[0].details["drug"] == "Warfarin"
    assert "confirming the omission is deliberate" in omissions[0].summary


def test_an_omitted_ordinary_drug_gets_a_line_but_no_flag():
    """The grading is the point: everything is reported, only the abrupt-stop hazards escalate."""
    result = reconcile_medications([_p(METFORMIN)], [_c(METFORMIN), _c(AMLODIPINE)])
    assert any(line.disposition == "stop" and line.label == "Amlodipine" for line in result.lines)
    assert [f for f in result.flags if f.finding == "high_risk_omission"] == []


@pytest.mark.parametrize(
    "drug_class",
    ["Vitamin K antagonist", "Anticonvulsant", "Corticosteroid", "Thyroid hormone"],
)
def test_each_curated_abrupt_stop_class_escalates(drug_class):
    drug = DrugRef(reference_id="X-1", generic_name="Some drug", drug_class=drug_class)
    lines = reconcile([_p(METFORMIN)], [_c(METFORMIN), _c(drug)])
    assert len(check_high_risk_omissions(lines)) == 1


def test_the_class_match_is_case_insensitive():
    """The vocabulary writes "Vitamin K antagonist"; the curated set is lower-cased."""
    drug = DrugRef(reference_id="X-1", generic_name="Some drug", drug_class="VITAMIN K ANTAGONIST")
    assert len(check_high_risk_omissions(reconcile([_p(METFORMIN)], [_c(drug)]))) == 1


# --- 2. Intra-list interaction: the pair no single-drug check meets ------------------------------


def test_two_proposed_drugs_that_interact_are_flagged_against_each_other():
    """Neither is charted, so neither drug's own check against the chart can find the pair."""
    flags = check_intra_list_interactions([_p(ASPIRIN_75), _p(WARFARIN)], [ASPIRIN_WARFARIN])
    assert len(flags) == 1
    assert flags[0].finding == "intra_list_interaction"
    assert flags[0].severity == "critical", "a major interaction"
    assert sorted(flags[0].details["drugs"]) == ["Aspirin", "Warfarin"]
    assert flags[0].details["both_proposed"] is True


def test_the_interaction_rule_matches_whichever_way_round_the_list_carries_the_pair():
    forward = check_intra_list_interactions([_p(ASPIRIN_75), _p(WARFARIN)], [ASPIRIN_WARFARIN])
    reverse = check_intra_list_interactions([_p(WARFARIN), _p(ASPIRIN_75)], [ASPIRIN_WARFARIN])
    assert len(forward) == len(reverse) == 1


def test_an_interaction_reaches_a_combination_product_through_its_component():
    """A rule keyed on metformin, against a product whose generic name never says "Metformin"."""
    contrast = DrugRef(reference_id="CONTRAST-IODINE", generic_name="Iodinated contrast")
    rule = InteractionRule(
        drug_a_reference_id="CONTRAST-IODINE",
        drug_b_reference_id="MET-500",
        severity="major",
        description="Risk of contrast-induced nephropathy with metformin.",
    )
    flags = check_intra_list_interactions([_p(GLYCOMET_GP), _p(contrast)], [rule])
    assert len(flags) == 1, "matched through the combination's metformin component"


def test_a_drug_does_not_interact_with_itself():
    """Every symmetric rule keyed on one id would otherwise fire against a duplicated line."""
    self_rule = InteractionRule(
        drug_a_reference_id="ASP-75",
        drug_b_reference_id="ASP-75",
        severity="major",
        description="n/a",
    )
    assert check_intra_list_interactions([_p(ASPIRIN_75), _p(ASPIRIN_150)], [self_rule]) == []


def test_the_most_severe_rule_wins_when_a_pair_is_curated_twice():
    minor = InteractionRule("ASP-75", "WARF-5", "minor", "Minor note.")
    major = InteractionRule("ASP-75", "WARF-5", "major", "Additive bleeding risk.")
    flags = check_intra_list_interactions([_p(ASPIRIN_75), _p(WARFARIN)], [minor, major])
    assert len(flags) == 1, "one clinical fact, reported once"
    assert flags[0].severity == "critical"


def test_an_intra_list_interaction_is_never_a_hard_block():
    """There is nothing to block: neither drug has been prescribed yet.

    ``ReconciliationFlag`` carries no ``is_hard_block`` at all, which is the structural form of
    that statement — a hard block is a refusal to proceed and belongs to the per-patient engine,
    where an override with documented reasoning is the way past it.
    """
    contraindicated = InteractionRule("ASP-75", "WARF-5", "contraindicated", "Never combine.")
    flags = check_intra_list_interactions([_p(ASPIRIN_75), _p(WARFARIN)], [contraindicated])
    assert not hasattr(flags[0], "is_hard_block")
    assert flags[0].severity == "critical"


def test_an_unresolved_proposed_line_takes_part_in_no_pair():
    assert check_intra_list_interactions([_p(None, "???"), _p(WARFARIN)], [ASPIRIN_WARFARIN]) == []


def test_the_rule_scope_is_built_from_the_proposed_list_too():
    """The scoping bug that would have made the whole check silently find nothing.

    The service loads interaction rules for "the drugs in play". Scoped to the chart's drugs
    alone, a rule whose *both* sides are new is never loaded, and the intra-list check then
    walks an empty rule table and reports nothing — indistinguishable, in the response, from a
    list with no interactions in it.
    """
    ids = proposed_reference_ids([_p(GLYCOMET_GP), _p(WARFARIN)])
    assert ids == {"MET-GLM-1-500", "MET-500", "GLM-1", "WARF-5"}, "components included"


# --- 3. Intra-list duplication ------------------------------------------------------------------


def test_the_same_molecule_twice_in_one_list_is_critical():
    flags = check_intra_list_duplicates([_p(ASPIRIN_75), _p(ASPIRIN_150)])
    assert len(flags) == 1
    assert flags[0].finding == "intra_list_duplicate"
    assert flags[0].severity == "critical"
    assert flags[0].details["shared_ingredients"] == ["aspirin"]


def test_a_combination_duplicating_a_single_ingredient_product_is_caught():
    """Glycomet GP beside plain Metformin — a doubled metformin dose on one page."""
    flags = check_intra_list_duplicates([_p(GLYCOMET_GP), _p(METFORMIN)])
    assert len(flags) == 1
    assert flags[0].finding == "intra_list_duplicate"
    assert "metformin" in flags[0].details["shared_ingredients"]


def test_two_drugs_of_one_class_are_a_softer_finding_than_a_shared_molecule():
    """Two of a class is frequently deliberate; the same molecule twice is not."""
    flags = check_intra_list_duplicates([_p(ASPIRIN_75), _p(CLOPIDOGREL)])
    assert len(flags) == 1
    assert flags[0].finding == "intra_list_class_overlap"
    assert flags[0].severity == "warning"
    assert "worth confirming" in flags[0].summary


def test_a_shared_molecule_is_not_also_reported_as_a_class_overlap():
    """Aspirin 75 and aspirin 150 share both. One finding, the stronger one."""
    flags = check_intra_list_duplicates([_p(ASPIRIN_75), _p(ASPIRIN_150)])
    assert [f.finding for f in flags] == ["intra_list_duplicate"]


def test_two_unidentified_ingredients_do_not_compare_equal():
    """The empty-reference-id guard, which would otherwise make every unnamed molecule one drug."""
    a = DrugRef(
        reference_id="A-1",
        generic_name="Product A",
        components=(DrugRef(reference_id="", generic_name="Clavulanic acid"),),
    )
    b = DrugRef(
        reference_id="B-1",
        generic_name="Product B",
        components=(DrugRef(reference_id="", generic_name="Trimethoprim"),),
    )
    assert check_intra_list_duplicates([_p(a), _p(b)]) == []


def test_drugs_with_no_class_do_not_all_overlap_with_each_other():
    a = DrugRef(reference_id="A-1", generic_name="Product A")
    b = DrugRef(reference_id="B-1", generic_name="Product B")
    assert check_intra_list_duplicates([_p(a), _p(b)]) == []


# --- 4. Identity: one molecule, several vocabulary rows ------------------------------------------


def test_a_strength_change_is_a_dose_change_not_a_stop_plus_a_start():
    """The bug reference-id-only matching would have shipped.

    Aspirin is ASP-75 *and* ASP-150. Matched on reference id alone, this reconciliation reports
    the patient's aspirin as discontinued and separately as newly started — two fabricated
    findings, one of them in the category the endpoint exists to make trustworthy.
    """
    lines = reconcile(
        [_p(ASPIRIN_150, dose="150", dose_unit="mg", frequency="OD")],
        [_c(ASPIRIN_75, dose="75", dose_unit="mg", frequency="OD")],
    )
    assert len(lines) == 1
    assert lines[0].disposition == "dose_change"
    assert lines[0].charted_dose == "75 mg od"
    assert lines[0].proposed_dose == "150 mg od"


def test_a_combination_continues_a_charted_single_ingredient_product():
    lines = reconcile([_p(GLYCOMET_GP)], [_c(METFORMIN)])
    assert [line.disposition for line in lines] == ["continue"]


def test_two_charted_rows_for_one_drug_are_not_both_claimed_by_one_proposed_line():
    """The chart legitimately holds two rows for one drug — the merge keys on name *and* dose.

    Consuming "the charted metformin" by identity rather than by position would leave the second
    row looking like an omission that the clinician never made.
    """
    lines = reconcile(
        [_p(METFORMIN, dose="500")],
        [_c(METFORMIN, dose="500"), _c(METFORMIN, dose="850")],
    )
    assert _dispositions(lines) == {"continue": ["Metformin"], "stop": ["Metformin"]}


# --- Dose comparison ----------------------------------------------------------------------------


def test_identical_doses_are_a_continue():
    lines = reconcile(
        [_p(METFORMIN, dose="500", dose_unit="mg")], [_c(METFORMIN, dose="500", dose_unit="mg")]
    )
    assert lines[0].disposition == "continue"


def test_dose_comparison_ignores_case_and_whitespace():
    lines = reconcile(
        [_p(METFORMIN, dose=" 500 ", dose_unit="MG", frequency="BD")],
        [_c(METFORMIN, dose="500", dose_unit="mg", frequency="bd")],
    )
    assert lines[0].disposition == "continue", "one prescription typed twice"


def test_a_missing_dose_on_one_side_is_not_a_dose_change():
    """A record that did not write the dose down is a gap in the chart, not a modification.

    Calling it a change would put the clinician's attention on the wrong line — and on a chart
    where most rows came from OCR, on most of the lines.
    """
    lines = reconcile([_p(METFORMIN, dose="500", dose_unit="mg")], [_c(METFORMIN)])
    assert lines[0].disposition == "continue"
    assert lines[0].charted_dose is None
    assert lines[0].proposed_dose == "500 mg", "the blank is still visible in the table"


# --- Unreadable lines are reported, never dropped -----------------------------------------------


def test_an_unresolvable_proposed_name_is_carried_through_as_its_own_disposition():
    lines = reconcile([_p(None, "Zerodol-SP")], [])
    assert lines[0].disposition == "unresolved_proposed"
    assert lines[0].label == "Zerodol-SP"
    assert "not the same as it being new" in lines[0].summary


def test_an_unresolvable_charted_row_is_carried_through_too():
    lines = reconcile([_p(METFORMIN)], [_c(None, "Illegible")])
    assert any(line.disposition == "unresolved_charted" for line in lines)


def test_two_unresolved_lines_do_not_match_each_other():
    """An empty identity must intersect nothing, including another empty one."""
    lines = reconcile([_p(None, "A")], [_c(None, "B")])
    assert _dispositions(lines) == {"unresolved_proposed": ["A"], "unresolved_charted": ["B"]}


# --- Flag ordering ------------------------------------------------------------------------------


def test_flags_are_ordered_with_the_most_severe_first():
    result = reconcile_medications(
        [_p(ASPIRIN_75), _p(CLOPIDOGREL), _p(ASPIRIN_150)],
        [_c(WARFARIN)],
        interaction_rules=[ASPIRIN_WARFARIN],
    )
    severities = [f.severity for f in result.flags]
    assert severities == sorted(severities, key=lambda s: {"critical": 0, "warning": 1}[s])


# --- The service and the route ------------------------------------------------------------------


async def _seeded_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"recon-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Reconciliation Patient",
        sex="male",
        date_of_birth=datetime(1968, 5, 10).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _chart(db, patient: Patient, generic: str, **columns) -> None:
    from app.models.drug_vocabulary import DrugVocabulary

    row = (
        (await db.execute(select(DrugVocabulary).where(DrugVocabulary.generic_name.ilike(generic))))
        .scalars()
        .first()
    )
    assert row is not None, f"seed data is missing {generic}"
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            drug_vocabulary_id=row.id,
            generic_name=row.generic_name,
            event_type="continue",
            is_current=True,
            **columns,
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_the_service_reconciles_against_the_real_chart(db):
    account, patient = await _seeded_patient(db)
    await _chart(db, patient, "Metformin")
    await _chart(db, patient, "Warfarin")

    result = await MedReconciliationService(db).reconcile(
        account_id=account.id,
        patient_id=patient.id,
        proposed=[ProposedLine(name="Glycomet"), ProposedLine(name="Amlong")],
        context="discharge",
    )

    dispositions = _dispositions(result.lines)
    assert dispositions.get("continue") == ["Metformin"], "Glycomet resolves to metformin"
    assert dispositions.get("start") == ["Amlodipine"]
    assert dispositions.get("stop") == ["Warfarin"]
    assert [f.finding for f in result.list_flags] == ["high_risk_omission"]
    assert result.charted_count == 2
    assert result.reconciled_line_count == 3


@pytest.mark.asyncio
async def test_the_service_loads_rules_for_a_pair_that_is_entirely_new(db):
    """End-to-end proof that the rule scope reaches drugs that are on neither chart nor context.

    The chart holds metformin only. Aspirin and warfarin are both proposed, and the rule that
    pairs them is keyed on two reference ids the chart has never seen.
    """
    account, patient = await _seeded_patient(db)
    await _chart(db, patient, "Metformin")

    result = await MedReconciliationService(db).reconcile(
        account_id=account.id,
        patient_id=patient.id,
        proposed=[
            ProposedLine(name="Metformin"),
            ProposedLine(name="Aspirin"),
            ProposedLine(name="Warfarin"),
        ],
        context="admission",
    )
    interactions = [f for f in result.list_flags if f.finding == "intra_list_interaction"]
    assert len(interactions) == 1, "the aspirin/warfarin rule was loaded and applied"
    assert sorted(interactions[0].details["drugs"]) == ["Aspirin", "Warfarin"]


@pytest.mark.asyncio
async def test_chart_level_notes_are_reported_once_not_once_per_proposed_drug(db):
    """A ten-drug list would otherwise carry ten copies of every statement about the chart.

    The subtraction is by value rather than by check type, so this also pins that a genuine
    per-drug ``duplicate_therapy`` — which shares its check type with the chart-level
    duplicate-orders finding — is not swept away with them.
    """
    account, patient = await _seeded_patient(db)
    # An unreadable current medication produces a chart-level `unevaluated_medication` note.
    db.add(
        MedicationEvent(
            patient_id=patient.id,
            brand_name_raw="Squiggle-XR",
            event_type="continue",
            is_current=True,
        )
    )
    await db.flush()

    result = await MedReconciliationService(db).reconcile(
        account_id=account.id,
        patient_id=patient.id,
        proposed=[
            ProposedLine(name="Metformin"),
            ProposedLine(name="Aspirin"),
            ProposedLine(name="Amlodipine"),
        ],
        context="admission",
    )
    unevaluated = [
        f for f in result.safety_findings if f.flag.check_type == "unevaluated_medication"
    ]
    assert len(unevaluated) == 1, "once for the chart, not once per proposed drug"
    assert unevaluated[0].drug is None, "chart-level findings name no drug"


@pytest.mark.asyncio
async def test_a_hard_block_carries_the_check_id_an_override_needs(db):
    """Critical Safety Rule #3: the only way past a hard block is an override naming the check.

    A reconciliation that raised blocks with no id would push clinicians back to the single-drug
    screen to get one — which is the screen this endpoint exists to spare them.
    """
    from app.models.allergy import Allergy

    account, patient = await _seeded_patient(db)
    db.add(
        Allergy(
            patient_id=patient.id,
            allergen_name="Aspirin",
            allergen_type="drug",
            status="active",
            severity="life_threatening",
        )
    )
    await db.flush()

    result = await MedReconciliationService(db).reconcile(
        account_id=account.id,
        patient_id=patient.id,
        proposed=[ProposedLine(name="Aspirin")],
        context="admission",
    )
    blocks = [f for f in result.safety_findings if f.flag.is_hard_block]
    assert blocks, "a documented aspirin allergy against proposed aspirin"
    assert result.has_hard_block
    assert all(f.check_id is not None for f in blocks), "overridable"


@pytest.mark.asyncio
async def test_an_empty_proposed_list_is_refused_rather_than_read_as_stop_everything(db):
    """The likelier cause is a client that failed to send its list.

    Answered rather than refused, it would render as every current medication flagged as an
    omission — a screenful of critical findings produced by a serialisation bug, which is how a
    clinician learns to dismiss them.
    """
    from app.exceptions import ValidationError

    account, patient = await _seeded_patient(db)
    await _chart(db, patient, "Warfarin")
    with pytest.raises(ValidationError):
        await MedReconciliationService(db).reconcile(
            account_id=account.id,
            patient_id=patient.id,
            proposed=[],
            context="discharge",
        )


@pytest.mark.asyncio
async def test_the_list_length_is_bounded(db):
    from app.exceptions import ValidationError

    account, patient = await _seeded_patient(db)
    with pytest.raises(ValidationError):
        await MedReconciliationService(db).reconcile(
            account_id=account.id,
            patient_id=patient.id,
            proposed=[ProposedLine(name="Metformin")] * (MAX_PROPOSED_MEDICATIONS + 1),
            context="admission",
        )


@pytest.mark.asyncio
async def test_reconciling_another_accounts_chart_is_refused(db):
    from app.exceptions import NotFoundError

    _account, patient = await _seeded_patient(db)
    other = Account(email=f"other-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(other)
    await db.flush()

    with pytest.raises(NotFoundError):
        await MedReconciliationService(db).reconcile(
            account_id=other.id,
            patient_id=patient.id,
            proposed=[ProposedLine(name="Metformin")],
            context="admission",
        )


@pytest.mark.asyncio
async def test_the_audit_entry_records_the_transition_and_no_drug_names(db):
    account, patient = await _seeded_patient(db)
    await _chart(db, patient, "Warfarin")

    await MedReconciliationService(db).reconcile(
        account_id=account.id,
        patient_id=patient.id,
        proposed=[ProposedLine(name="Metformin")],
        context="transfer",
    )
    await db.flush()

    row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "medications_reconciled")))
        .scalars()
        .one()
    )
    assert row.payload["context"] == "transfer"
    assert row.payload["dispositions"] == {"start": 1, "stop": 1}
    assert row.payload["charted_count"] == 1
    serialised = str(row.payload).lower()
    assert "warfarin" not in serialised and "metformin" not in serialised


# --- The HTTP surface ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_route_returns_a_reconciliation(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/reconcile",
        json={
            "context": "discharge",
            "medications": [{"name": "Ecosprin", "dose": "75", "dose_unit": "mg"}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["context"] == "discharge"
    assert body["proposed_count"] == 1
    assert body["reconciled_count"] == 1
    assert body["lines"][0]["disposition"] == "start"
    assert body["offline_capable"] is True


@pytest.mark.asyncio
async def test_an_unresolvable_name_does_not_fail_the_whole_request(auth_client):
    """Deliberately different from ``POST ../check``, which 422s on an unresolved name.

    There the request *was* the one drug. Here, refusing eleven lines because one is an unseeded
    brand leaves the clinician with no reconciliation — and the other ten are where the
    omissions are.
    """
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/reconcile",
        json={
            "context": "admission",
            "medications": [{"name": "Metformin"}, {"name": "Zerodol-SP-Forte-XR"}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unresolved_proposed"] == ["Zerodol-SP-Forte-XR"]
    assert body["proposed_count"] == 2
    assert body["reconciled_count"] == 1, "the unresolved line was compared against nothing"


@pytest.mark.asyncio
async def test_the_route_rejects_an_empty_list_and_an_unknown_context(auth_client):
    patient = await create_patient(auth_client)
    for payload in (
        {"context": "discharge", "medications": []},
        {"context": "ward_round", "medications": [{"name": "Metformin"}]},
    ):
        resp = await auth_client.post(
            f"/api/v1/patients/{patient['id']}/drug-safety/reconcile", json=payload
        )
        assert resp.status_code == 422, payload


@pytest.mark.asyncio
async def test_the_route_bounds_the_list_length(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/reconcile",
        json={
            "context": "admission",
            "medications": [{"name": "Metformin"}] * (MAX_PROPOSED_MEDICATIONS + 1),
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_another_accounts_chart_is_not_reconcilable_over_http(
    auth_client, second_auth_client
):
    patient = await create_patient(auth_client)
    resp = await second_auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/reconcile",
        json={"context": "admission", "medications": [{"name": "Metformin"}]},
    )
    assert resp.status_code == 404
