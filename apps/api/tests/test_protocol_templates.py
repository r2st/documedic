"""Curated order sets: the content's own invariants, and what applying one does to a chart.

Two halves. The first is about the templates as *data* — that every drug they name resolves,
that nothing in them reads as an instruction, that the keys are unique — because a template is
compiled-in content nobody re-reads and a defect in it repeats on every patient it touches. The
second is the apply path, where the safety engine is what stands between a one-click order set
and a contraindicated prescription.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.order_sets import (
    VERSION,
    all_order_sets,
    get_order_set,
    order_sets_for_condition,
)
from app.models.allergy import Allergy
from app.models.condition import Condition
from app.services.drug_resolver import DrugResolver
from tests.conftest import create_patient

# Conditions and allergies reach a chart through document extraction, not through a write route
# of their own, so the tests below seed them on the shared engine and commit -- the same shape
# ``test_cds_multi_patient_workflow`` uses to put an allergy in front of an HTTP request.


async def _chart_condition(db, patient_id: str, name: str) -> None:
    db.add(
        Condition(
            patient_id=uuid.UUID(patient_id),
            condition_name=name,
            status="active",
            clinician_confirmed=True,
        )
    )
    await db.commit()


async def _chart_allergy(db, patient_id: str, allergen: str, severity: str = "severe") -> None:
    db.add(
        Allergy(
            patient_id=uuid.UUID(patient_id),
            allergen_name=allergen,
            allergen_type="drug",
            status="active",
            severity=severity,
            clinician_confirmed=True,
        )
    )
    await db.commit()


# --- the content ------------------------------------------------------------------------------


def test_there_are_templates_and_their_keys_are_unique():
    """Guard the guard: an empty catalogue would make every check below vacuous, and a repeated
    key would silently shadow one template with another."""
    templates = all_order_sets()
    assert templates
    keys = [order_set.key for order_set in templates]
    assert len(keys) == len(set(keys))


def test_every_item_key_is_unique_within_its_template():
    """Selection is by key. Two items sharing one would make a deselection ambiguous — the
    clinician unticks a medication and an investigation disappears with it."""
    for order_set in all_order_sets():
        keys = list(order_set.item_keys())
        assert len(keys) == len(set(keys)), f"{order_set.key} has duplicate item keys: {keys}"


@pytest.mark.asyncio
async def test_every_medication_named_by_a_template_resolves_against_the_vocabulary(db):
    """The check that keeps a template from charting a drug nothing can evaluate.

    A medication the resolver cannot identify gets no allergy cross-check, no interaction check
    and no contraindication check — so charting it from a template would put a drug on the
    record carrying the *appearance* of having passed the same checks as its neighbours. The
    service refuses at runtime; this is what makes the refusal never happen, and it is the same
    shape as ``test_drug_rule_references``' refusal of a rule naming a drug that is not there.
    """
    resolver = DrugResolver(db)
    unresolved: list[str] = []
    for order_set in all_order_sets():
        for medication in order_set.medications:
            if await resolver.resolve(medication.generic_name) is None:
                unresolved.append(f"{order_set.key}:{medication.generic_name}")
    assert not unresolved, (
        "protocol templates name drugs the vocabulary cannot resolve, so applying them would "
        f"chart an unevaluable medication: {unresolved}"
    )


def test_no_template_reads_as_an_instruction():
    """Critical Safety Rule #4, over content a clinician reads and then acts on in one click.

    Imperatives are matched at the *start of a sentence*, which is the only position they are
    commands in — "guidelines support considering starting" is prose, "Start metformin" is an
    order. Certainty claims are matched anywhere, because they are wrong wherever they sit.
    """
    imperatives = ("give", "administer", "prescribe", "start", "diagnose", "order")
    certainty = ("the patient has ", "the diagnosis is ", "will cure", "is safe")

    offenders: list[str] = []
    for order_set in all_order_sets():
        texts = [order_set.title, order_set.indication]
        texts += [item.label for item in order_set.investigations]
        texts += [item.rationale or "" for item in order_set.investigations]
        texts += [item.note or "" for item in order_set.medications]
        texts += [item.label for item in order_set.follow_ups]
        for text in texts:
            for sentence in re.split(r"(?<=[.!?:])\s+|\n", text):
                first = sentence.strip().lower().split(" ")[0].rstrip(",")
                if first in imperatives:
                    offenders.append(f"{order_set.key}: {sentence.strip()[:60]}")
            offenders += [
                f"{order_set.key}: {phrase}" for phrase in certainty if phrase in text.lower()
            ]
    assert offenders == [], offenders


def test_a_dose_is_only_ever_offered_with_its_unit():
    """A number with no unit is the failure the dose-integrity work exists for: "Levothyroxine
    100" is charted, checked against every rule, and reported clean."""
    for order_set in all_order_sets():
        for medication in order_set.medications:
            if medication.typical_dose is not None:
                assert medication.dose_unit, f"{order_set.key}:{medication.key} has a bare dose"


def test_a_curated_template_claims_no_guideline_sections():
    """``source="curated"`` means the content has no matching corpus document. Attaching
    section ids to one would let the API present curated text as guideline-cited, which is the
    same rule ``app.core.pathways`` states."""
    for order_set in all_order_sets():
        if order_set.source == "curated":
            assert order_set.guideline_section_ids == (), order_set.key


def test_condition_matching_is_substring_in_one_direction_only():
    """A chart's condition is rarely spelled exactly as a template names it, so a long charted
    name matches a short template name — but never the other way round, or a template would
    match a condition merely because the template's own name is long."""
    assert [o.key for o in order_sets_for_condition("Type 2 Diabetes Mellitus (on metformin)")] == [
        "t2dm_initial_workup"
    ]
    assert order_sets_for_condition("Diabetes") == ()
    assert order_sets_for_condition("") == ()


def test_an_unknown_template_key_resolves_to_nothing_rather_than_raising():
    assert get_order_set("no_such_template") is None
    assert get_order_set("  T2DM_INITIAL_WORKUP  ") is not None


# --- the catalogue route ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_catalogue_carries_the_version_that_will_be_stored(auth_client):
    resp = await auth_client.get("/api/v1/order-sets")
    assert resp.status_code == 200, resp.text
    assert {row["version"] for row in resp.json()} == {VERSION}


@pytest.mark.asyncio
async def test_templates_are_suggested_only_from_documented_conditions(auth_client, db):
    """Never from a differential. Matching on the panel's hypotheses would put its suspicion
    one click from being charted as therapy."""
    patient = await create_patient(auth_client)
    empty = await auth_client.get(f"/api/v1/patients/{patient['id']}/order-sets/suggested")
    assert empty.json() == []

    await _chart_condition(db, patient["id"], "Type 2 Diabetes Mellitus")
    matched = await auth_client.get(f"/api/v1/patients/{patient['id']}/order-sets/suggested")
    assert [row["key"] for row in matched.json()] == ["t2dm_initial_workup"]


# --- preview ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_preview_of_a_clean_chart_is_not_blocked(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/preview",
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_blocked"] is False
    assert body["unresolved_medications"] == []
    assert {item["key"] for item in body["investigations"]} >= {"hba1c", "creatinine"}


@pytest.mark.asyncio
async def test_a_documented_allergy_to_a_template_drug_blocks_the_preview(auth_client, db):
    """The check that matters. An order set is one click, and the allergy hard block has to
    stand in front of it exactly as it stands in front of a typed prescription."""
    patient = await create_patient(auth_client)
    await _chart_allergy(db, patient["id"], "Metformin")

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/preview",
        json={},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_blocked"] is True
    blocked = [f for f in body["findings"] if f["is_hard_block"]]
    assert blocked and blocked[0]["check_id"], (
        "a hard block with no check_id cannot be overridden, so it is a dead end"
    )
    # Hard blocks sort first: the list is read top-down.
    assert body["findings"][0]["is_hard_block"] is True


@pytest.mark.asyncio
async def test_deselecting_the_blocked_medication_clears_the_block(auth_client, db):
    """Every item is deselectable, and this is what that is for: the workup is still worth
    ordering when one drug in the template is not usable for this patient."""
    patient = await create_patient(auth_client)
    await _chart_allergy(db, patient["id"], "Metformin")
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/preview",
        json={"selected_keys": ["hba1c", "creatinine", "review_12_weeks"]},
    )
    assert resp.json()["is_blocked"] is False
    assert resp.json()["medications"] == []


@pytest.mark.asyncio
async def test_an_empty_selection_is_refused_rather_than_previewed_as_clean(auth_client):
    """An empty list is almost always a client that failed to send its selection, and a clean
    preview of nothing is the wrong direction to fail in."""
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/preview",
        json={"selected_keys": []},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_a_selection_naming_an_item_the_template_does_not_have_is_refused(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/preview",
        json={"selected_keys": ["hba1c", "not_an_item"]},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_an_unknown_template_is_a_404(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/no_such_template/preview", json={}
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "protocol_template_not_found"


# --- apply ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_applying_charts_the_medications_and_records_the_investigations(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"applied_by": "Dr Priya Nair"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["medication_event_ids"]) == 1
    assert {item["key"] for item in body["ordered_investigations"]} >= {"hba1c", "creatinine"}
    assert body["template_version"] == VERSION

    record = await auth_client.get(f"/api/v1/patients/{patient['id']}/record")
    generics = [med["generic_name"] for med in record.json()["medications"]]
    assert "Metformin" in generics


@pytest.mark.asyncio
async def test_the_charted_medication_takes_its_generic_name_from_the_vocabulary(auth_client):
    """Not from the template's own string. The vocabulary is the authority on a drug's INN, and
    a template spelling it differently must not introduce a second spelling into the chart —
    which is what would then fail to match an allergy recorded against the first."""
    patient = await create_patient(auth_client)
    await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"applied_by": "Dr Priya Nair"},
    )
    record = await auth_client.get(f"/api/v1/patients/{patient['id']}/record")
    charted = [m for m in record.json()["medications"] if m["generic_name"] == "Metformin"]
    assert charted and charted[0]["drug_vocabulary_id"] is not None


@pytest.mark.asyncio
async def test_a_hard_block_refuses_the_whole_application_and_charts_nothing(auth_client, db):
    """Not the blocked line — the application. Half-applying leaves a chart that reads as a
    completed workup with the contraindicated drug quietly missing."""
    patient = await create_patient(auth_client)
    await _chart_allergy(db, patient["id"], "Metformin")

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"applied_by": "Dr Priya Nair"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "hard_block"

    applications = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/order-sets/applications"
    )
    assert applications.json() == []
    record = await auth_client.get(f"/api/v1/patients/{patient['id']}/record")
    assert [m for m in record.json()["medications"] if m["generic_name"] == "Metformin"] == []


@pytest.mark.asyncio
async def test_the_investigations_can_still_be_applied_around_a_blocked_medication(auth_client, db):
    patient = await create_patient(auth_client)
    await _chart_allergy(db, patient["id"], "Metformin")
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"selected_keys": ["hba1c", "creatinine", "urine_acr"], "applied_by": "Dr Nair"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["medication_event_ids"] == []
    assert len(resp.json()["ordered_investigations"]) == 3


@pytest.mark.asyncio
async def test_applying_books_the_follow_up_when_a_provider_is_named(auth_client):
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"applied_by": "Dr Priya Nair", "follow_up_provider_name": "Dr Priya Nair"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["follow_up_appointment_id"] is not None

    diary = await auth_client.get("/api/v1/appointments/diary", params={"days": 90})
    booked = [row for row in diary.json() if row["source_protocol_key"] == "t2dm_initial_workup"]
    assert len(booked) == 1
    assert booked[0]["appointment_type"] == "diabetes_review"


@pytest.mark.asyncio
async def test_omitting_the_follow_up_provider_applies_everything_else_without_booking(
    auth_client,
):
    """The follow-up interval is recorded as selected and nothing goes in the diary — a clinic
    that has not decided whose list the review belongs on must still be able to apply the set."""
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"applied_by": "Dr Priya Nair"},
    )
    assert resp.status_code == 201
    assert resp.json()["follow_up_appointment_id"] is None
    assert "review_12_weeks" in resp.json()["selected_keys"]


@pytest.mark.asyncio
async def test_a_clash_on_the_follow_up_refuses_the_whole_application(auth_client):
    """All-or-nothing, including the diary. A protocol whose drugs were charted and whose review
    was not is a plan with the recall silently missing, and the recall is the part nobody
    notices is absent."""
    patient = await create_patient(auth_client)
    # Occupy the exact slot the template's 84-day follow-up will compute to.
    from app.config import settings
    from app.core.order_sets import get_order_set
    from app.services.protocol_service import _default_follow_up_slot

    template = get_order_set("t2dm_initial_workup")
    assert template is not None
    slot = _default_follow_up_slot(datetime.now(UTC).date(), template.follow_ups[0])
    blocker = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/appointments",
        json={
            "provider_name": "Dr Priya Nair",
            "starts_at": slot.isoformat(),
            "ends_at": (
                slot + timedelta(minutes=settings.protocol_follow_up_duration_minutes)
            ).isoformat(),
        },
    )
    assert blocker.status_code == 201, blocker.text

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"applied_by": "Dr Priya Nair", "follow_up_provider_name": "Dr Priya Nair"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "appointment_conflict"

    record = await auth_client.get(f"/api/v1/patients/{patient['id']}/record")
    assert [m for m in record.json()["medications"] if m["generic_name"] == "Metformin"] == []


@pytest.mark.asyncio
async def test_the_application_records_what_the_clinician_chose_not_to_order(auth_client):
    """Not recoverable from the template plus the charted rows: a deselected investigation
    leaves no trace anywhere else on the chart."""
    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"selected_keys": ["hba1c", "metformin"], "applied_by": "Dr Nair"},
    )
    assert resp.status_code == 201, resp.text
    assert sorted(resp.json()["selected_keys"]) == ["hba1c", "metformin"]

    listed = await auth_client.get(f"/api/v1/patients/{patient['id']}/order-sets/applications")
    assert sorted(listed.json()[0]["selected_keys"]) == ["hba1c", "metformin"]


@pytest.mark.asyncio
async def test_an_application_is_audited_without_naming_the_drugs(auth_client):
    """Counts and curated identifiers. The drug names are the patient's prescribing and live on
    the medication events the application points at, each of which wrote its own safety check."""
    patient = await create_patient(auth_client)
    await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"applied_by": "Dr Priya Nair"},
    )
    audit = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/audit",
        params={"action": "protocol_applied", "limit": 50},
    )
    payload = audit.json()["items"][0]["payload"]
    assert payload["template_key"] == "t2dm_initial_workup"
    assert payload["medications_charted"] == 1
    assert "metformin" not in str(payload).lower()


@pytest.mark.asyncio
async def test_the_stored_version_does_not_move_when_the_curated_content_does(
    auth_client, monkeypatch
):
    """The reason the version is a column. A correction made next month must not rewrite what a
    clinician ordered today."""
    patient = await create_patient(auth_client)
    applied = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"applied_by": "Dr Priya Nair"},
    )
    assert applied.json()["template_version"] == VERSION

    monkeypatch.setattr("app.core.order_sets.VERSION", "9999.99.9")
    listed = await auth_client.get(f"/api/v1/patients/{patient['id']}/order-sets/applications")
    assert listed.json()[0]["template_version"] == VERSION


@pytest.mark.asyncio
async def test_an_application_cannot_be_read_from_another_account(auth_client, second_auth_client):
    patient = await create_patient(auth_client)
    await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"applied_by": "Dr Priya Nair"},
    )
    stolen = await second_auth_client.get(
        f"/api/v1/patients/{patient['id']}/order-sets/applications"
    )
    assert stolen.status_code == 404


@pytest.mark.asyncio
async def test_a_template_naming_an_unresolvable_drug_refuses_the_apply(auth_client, monkeypatch):
    """The runtime backstop for the content test above, for a vocabulary edited after release.

    Charting a drug nothing can identify is the failure this codebase has closed twice: it goes
    on the record carrying the appearance of having passed the same checks as its neighbours.
    """
    from app.core import order_sets as order_sets_module

    original = order_sets_module.get_order_set("t2dm_initial_workup")
    assert original is not None
    broken = order_sets_module.OrderSet(
        key=original.key,
        title=original.title,
        condition_name=original.condition_name,
        source=original.source,
        indication=original.indication,
        investigations=original.investigations,
        medications=(
            order_sets_module.MedicationItem(
                key="unknown_drug",
                generic_name="Zephyrimycin",
                typical_dose="500",
                dose_unit="mg",
                frequency="BD",
            ),
        ),
        follow_ups=original.follow_ups,
    )
    monkeypatch.setitem(order_sets_module._ORDER_SETS, "t2dm_initial_workup", broken)

    patient = await create_patient(auth_client)
    preview = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/preview", json={}
    )
    assert preview.json()["unresolved_medications"] == ["Zephyrimycin"]

    applied = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/apply",
        json={"applied_by": "Dr Nair"},
    )
    assert applied.status_code == 409
    assert applied.json()["code"] == "protocol_template_unusable"


@pytest.mark.asyncio
async def test_a_template_pairing_two_interacting_drugs_is_flagged_before_either_is_charted(
    auth_client, monkeypatch, db
):
    """The pair no per-drug check can see.

    Checking drug A against the chart and drug B against the chart asks about A×chart and
    B×chart. A×B is asked by neither — and a curated set that pairs them would repeat the
    mistake on every patient it is applied to.
    """
    from sqlalchemy import select

    from app.core import order_sets as order_sets_module
    from app.models.drug_vocabulary import DrugInteraction

    pair = (await db.execute(select(DrugInteraction).limit(1))).scalars().first()
    assert pair is not None, "the seeded vocabulary carries no interaction to build this on"

    from app.services.drug_resolver import DrugResolver

    resolver = DrugResolver(db)
    first = await resolver.resolve_reference_id(pair.drug_a_reference_id)
    second = await resolver.resolve_reference_id(pair.drug_b_reference_id)
    assert first is not None and second is not None

    original = order_sets_module.get_order_set("t2dm_initial_workup")
    assert original is not None
    paired = order_sets_module.OrderSet(
        key=original.key,
        title=original.title,
        condition_name=original.condition_name,
        source=original.source,
        indication=original.indication,
        investigations=(),
        medications=(
            order_sets_module.MedicationItem(
                key="drug_a", generic_name=first.generic_name, dose_unit="mg"
            ),
            order_sets_module.MedicationItem(
                key="drug_b", generic_name=second.generic_name, dose_unit="mg"
            ),
        ),
        follow_ups=(),
    )
    monkeypatch.setitem(order_sets_module._ORDER_SETS, "t2dm_initial_workup", paired)

    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/order-sets/t2dm_initial_workup/preview", json={}
    )
    assert resp.status_code == 200, resp.text
    summaries = " ".join(f["summary"] for f in resp.json()["findings"])
    assert first.generic_name in summaries and second.generic_name in summaries

    # ...and no intra-list finding is ever a hard block: there is no persisted check row behind
    # it, so nothing could override one, and a block a clinician cannot pass is a dead end.
    intra = [f for f in resp.json()["findings"] if f["drug"] is None and f["check_id"] is None]
    assert intra and all(not f["is_hard_block"] for f in intra)


@pytest.mark.asyncio
async def test_a_protocol_application_at_the_column_ceiling_fits_a_column_typed_database():
    """SQLite enforces no VARCHAR length; PostgreSQL raises. See ``tests/column_fit.py``."""
    from app.models.protocol_application import ProtocolApplication
    from tests.column_fit import assert_fits_columns

    assert_fits_columns(
        ProtocolApplication(
            account_id=uuid.uuid4(),
            patient_id=uuid.uuid4(),
            template_key="k" * 60,
            template_version="v" * 20,
            template_title="t" * 200,
            applied_by="d" * 200,
            warning_count=0,
        )
    )


def test_every_template_key_fits_the_column_it_is_stored_in():
    """The three-place lesson from the telehealth check type: a value, its column width, and
    whatever constrains it. A template key longer than ``VARCHAR(60)`` would apply fine on
    SQLite and raise on PostgreSQL the first time a clinician used it."""
    from app.models.protocol_application import ProtocolApplication

    limit = ProtocolApplication.__table__.c.template_key.type.length
    too_long = [o.key for o in all_order_sets() if len(o.key) > limit]
    assert not too_long, f"template keys longer than VARCHAR({limit}): {too_long}"

    title_limit = ProtocolApplication.__table__.c.template_title.type.length
    long_titles = [o.key for o in all_order_sets() if len(o.title) > title_limit]
    assert not long_titles, f"template titles longer than VARCHAR({title_limit}): {long_titles}"

    assert len(VERSION) <= ProtocolApplication.__table__.c.template_version.type.length
