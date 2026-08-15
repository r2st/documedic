"""A charted diagnosis must reach the contraindication engine whatever its status column says.

``SafetyService._conditions`` selected ``status == "active"`` and nothing else, so three of the
five statuses ``conditions.status`` permits never reached ``app.core.safety`` at all. Two of
them are live rows:

* ``recurrence`` — what a clinician records when a condition came back. A present diagnosis by
  any clinical reading, and it was dropped, so the rule keyed on it matched nothing and
  ``POST ../drug-safety/check`` answered ``is_blocked: false``.
* ``unknown`` — what ``graph_service._enum`` writes when a document's status field could not be
  read. That choice is deliberate and right ("rather than asserting an active diagnosis nobody
  made"), but the row it produces is a real charted diagnosis, and it was invisible: not a
  block, not a warning, and not even an unevaluated-condition note, because
  ``check_unevaluated_conditions`` only ever sees rows that got past this query.

The failure was the shape this module has now been corrected for repeatedly — a comparison that
could not be attempted, reported as a comparison that passed.

The fix is asymmetric on purpose. ``recurrence`` blocks like ``active``. ``unknown`` goes
through the same near-miss flag as a textually hedged wording ("h/o asthma"): reported, with the
rule named and the reason it was not enforced, because manufacturing a hard block out of an OCR
failure is its own harm.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.safety import (
    CONDITION_RESOLVED_STATUSES,
    ContraindicationRule,
    DrugRef,
    PatientCondition,
    SafetyContext,
    check_contraindications,
    has_hard_block,
)
from app.models.condition import Condition
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

ATENOLOL = DrugRef("ATE-50", "Atenolol", "Beta Blocker")
ASTHMA_RULE = ContraindicationRule(
    drug_reference_id="ATE-50",
    condition_name="Bronchial Asthma",
    severity="absolute",
    description="Beta blockade can precipitate bronchospasm.",
    is_absolute=True,
    contraindication_id="ci-asthma",
)

# Every value conditions.status permits, from the model's own CHECK constraint. Pinned as a
# literal so adding a status to the column without deciding what the safety engine does with it
# fails here rather than silently defaulting to "not consulted".
ALL_STATUSES = ("active", "resolved", "inactive", "recurrence", "unknown")


def _check(status: str | None):
    ctx = SafetyContext(
        conditions=[PatientCondition(condition_name="Asthma", status=status)],
        contraindication_rules=[ASTHMA_RULE],
    )
    return check_contraindications(ATENOLOL, ctx)


# --- the engine ------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["active", "recurrence"])
def test_a_status_the_chart_asserts_as_current_hard_blocks(status: str) -> None:
    """A recurred asthma diagnosis is asthma. It must block a beta blocker like an active one."""
    flags = _check(status)

    assert len(flags) == 1
    assert flags[0].is_hard_block is True
    assert flags[0].severity == "hard_block"
    assert flags[0].details["charted_status"] == status


def test_a_status_the_record_could_not_read_is_reported_not_dropped() -> None:
    """The silent case. "unknown" must produce a flag naming the rule, not an empty list."""
    flags = _check("unknown")

    assert len(flags) == 1, "an unknown-status diagnosis produced no flag at all"
    assert flags[0].check_type == "contraindication"
    assert flags[0].details["evaluated"] is False
    assert flags[0].details["would_hard_block_if_confirmed"] is True
    assert flags[0].details["charted_condition"] == "Asthma"
    assert flags[0].details["charted_status"] == "unknown"
    assert "unconfirmed_status" in flags[0].details["match_basis"]


def test_the_unknown_status_flag_says_the_status_is_why() -> None:
    """A clinician sent to re-read a problem-list line that looks definite learns nothing.

    The flag has to name the column, because setting the status is the fix — not re-wording the
    diagnosis, which is what a bare "the record does not establish it" points at.
    """
    summary = _check("unknown")[0].summary

    assert "unknown" in summary
    assert "status" in summary
    assert "hard block was not applied" in summary


def test_an_unknown_status_does_not_hard_block() -> None:
    """The other direction: an unreadable status must not manufacture a block."""
    flags = _check("unknown")

    assert has_hard_block(flags) is False
    assert flags[0].severity == "warning"


def test_an_established_row_still_wins_over_an_unconfirmed_one() -> None:
    """A chart carrying both blocks, exactly as it does for a hedged wording."""
    ctx = SafetyContext(
        conditions=[
            PatientCondition(condition_name="Asthma", status="unknown"),
            PatientCondition(condition_name="Asthma", status="active"),
        ],
        contraindication_rules=[ASTHMA_RULE],
    )

    flags = check_contraindications(ATENOLOL, ctx)

    assert len(flags) == 1
    assert flags[0].is_hard_block is True


def test_a_caller_with_no_status_column_to_read_is_not_downgraded() -> None:
    """``status=None`` is silence, not doubt.

    Every construction of ``PatientCondition`` outside the service — the reasoning engine's
    snapshots, and the rest of this suite — omits it. Reading that omission as "unconfirmed"
    would quietly turn every hard block that does not come through the database into a warning.
    """
    flags = _check(None)

    assert len(flags) == 1
    assert flags[0].is_hard_block is True
    assert flags[0].details["charted_status"] is None


def test_every_permitted_status_is_either_consulted_or_deliberately_excluded() -> None:
    """No status may fall through to "not consulted" merely by not having been thought about.

    This is the guard the original bug needed. ``recurrence`` was not excluded on purpose — it
    was simply not in the one value the query tested for, and nothing said so. Adding a sixth
    status to the column now fails here until someone decides which of the three treatments it
    gets, rather than silently defaulting to the invisible one.
    """
    from app.core.safety import _CONDITION_PRESENT_STATUSES

    # The third treatment: charted, evaluated, but routed through the near-miss flag instead of
    # enforced. ``_match_condition`` sends every status that is not "present" down it, so this
    # names the ones that are meant to land there.
    routed_to_near_miss = {"unknown"}
    decided = _CONDITION_PRESENT_STATUSES | CONDITION_RESOLVED_STATUSES | routed_to_near_miss

    assert set(ALL_STATUSES) == decided, (
        "a conditions.status value is neither present, resolved, nor routed through the "
        "near-miss flag: " + str(sorted(set(ALL_STATUSES) ^ decided))
    )
    assert _CONDITION_PRESENT_STATUSES.isdisjoint(CONDITION_RESOLVED_STATUSES)


# --- through the service, against the real seeded contraindication table ----------------------


async def _account_and_patient(db) -> tuple[Account, Patient]:
    account = Account(email=f"cs-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Status Patient",
        sex="female",
        date_of_birth=datetime(1990, 4, 11).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account, patient


async def _check_atenolol(db, status: str):
    account, patient = await _account_and_patient(db)
    db.add(Condition(patient_id=patient.id, condition_name="Asthma", status=status))
    await db.flush()
    _vocab, ctx, flags, _ids = await SafetyService(db).check_medication(
        account_id=account.id,
        patient_id=patient.id,
        drug_reference_id=None,
        drug_name="Atenolol",
    )
    return ctx, flags


async def test_a_recurred_asthma_reaches_the_service_and_blocks(db) -> None:
    """The bug as a clinician would have met it: asthma back, atenolol proposed, "not blocked"."""
    ctx, flags = await _check_atenolol(db, "recurrence")

    assert [c.condition_name for c in ctx.conditions] == ["Asthma"]
    assert has_hard_block(flags) is True


async def test_an_unknown_status_asthma_reaches_the_service_and_is_flagged(db) -> None:
    """It must arrive in the context. Silence here is what made the whole row disappear."""
    ctx, flags = await _check_atenolol(db, "unknown")

    assert [c.status for c in ctx.conditions] == ["unknown"]
    related = [f for f in flags if f.check_type == "contraindication"]
    assert len(related) == 1
    assert related[0].details["evaluated"] is False
    assert has_hard_block(flags) is False


@pytest.mark.parametrize("status", ["resolved", "inactive"])
async def test_a_finished_condition_is_still_kept_out_of_the_context(db, status: str) -> None:
    """The widening must not drag conditions the patient no longer has back in."""
    ctx, flags = await _check_atenolol(db, status)

    assert ctx.conditions == []
    assert has_hard_block(flags) is False


async def test_the_endpoint_blocks_a_recurred_condition(auth_client, db) -> None:
    """``is_blocked`` is what the Safety screen renders, and for a recurrence it said false."""
    from tests.conftest import create_patient

    patient = await create_patient(auth_client)
    db.add(
        Condition(patient_id=uuid.UUID(patient["id"]), condition_name="Asthma", status="recurrence")
    )
    await db.commit()

    response = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Atenolol"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_blocked"] is True
    blocks = [f for f in body["flags"] if f["is_hard_block"]]
    assert len(blocks) == 1
    assert blocks[0]["details"]["charted_status"] == "recurrence"


async def test_the_endpoint_surfaces_an_unknown_status_condition(auth_client, db) -> None:
    """Not a block, but never an empty flag list — the clinician has to be told to look."""
    from tests.conftest import create_patient

    patient = await create_patient(auth_client)
    db.add(
        Condition(patient_id=uuid.UUID(patient["id"]), condition_name="Asthma", status="unknown")
    )
    await db.commit()

    response = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Atenolol"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_blocked"] is False
    related = [f for f in body["flags"] if f["check_type"] == "contraindication"]
    assert len(related) == 1, "the chart's only relevant diagnosis produced no flag"
    assert related[0]["details"]["charted_status"] == "unknown"


async def test_an_unknown_status_condition_is_counted_on_the_chart(auth_client, db) -> None:
    """``checked_against.conditions`` is the response's own claim about what it consulted.

    It read 0 for a chart carrying one diagnosis, which is the response asserting the check was
    complete over an empty problem list.
    """
    from tests.conftest import create_patient

    patient = await create_patient(auth_client)
    db.add(
        Condition(patient_id=uuid.UUID(patient["id"]), condition_name="Asthma", status="unknown")
    )
    await db.commit()

    response = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/drug-safety/check",
        json={"drug_name": "Paracetamol"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["checked_against"]["conditions"] == 1
