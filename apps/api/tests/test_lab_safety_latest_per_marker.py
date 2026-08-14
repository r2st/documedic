"""Which row the panic-value screen actually evaluates, when a marker has more than one.

``LabSafetyService.check_patient_labs`` takes the most recent result *per marker* and tests
only that one against the critical thresholds. Every other test of this path gives a patient
one result per marker, so "per marker" is never exercised as a decision -- and it is a decision
with a clinical consequence, because the row that loses is the row that is not screened.

Two ways it went wrong, both reachable from ordinary data:

* **The same marker under two spellings.** Markers come out of OCR of whatever layout each lab
  prints, so one patient accumulates "Potassium", "POTASSIUM" and " potassium". The grouping
  was case- and whitespace-insensitive but the sort was not, so the spellings formed separate
  runs, the first run's newest row claimed the shared key, and later spellings were skipped as
  already seen. A panic value recorded in a different case than an older normal one was
  dropped before it was ever evaluated.
* **Two results from one draw.** A re-run, or the same panel entered from two documents. With
  nothing to break the tie, which one counts as "latest" is whatever the planner returns
  first -- a coin flip between flagging a critical potassium and not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.lab_safety import CriticalLabFlag
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.models.user import Account
from app.services.lab_safety_service import LabSafetyService

pytestmark = pytest.mark.asyncio

# Above the 6.5 mmol/L panic threshold; 4.0 is comfortably inside the reference range.
PANIC_K = 7.4
NORMAL_K = 4.0


async def _account_and_patient(db) -> tuple[uuid.UUID, Patient]:
    account_id = (await db.execute(select(Account.id))).scalars().first()
    if account_id is None:
        account = Account(
            email="lab-latest@example.com", password_hash="x", display_name="Dr Latest"
        )
        db.add(account)
        await db.flush()
        account_id = account.id
    patient = Patient(
        account_id=account_id,
        full_name="Marker Patient",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account_id, patient


def _potassium(patient_id: uuid.UUID, name: str, value: float, when: datetime | None) -> LabResult:
    return LabResult(
        patient_id=patient_id,
        marker_name=name,
        value_numeric=value,
        unit="mmol/L",
        sample_date=when,
    )


async def _flags(
    db, account_id: uuid.UUID, patient: Patient
) -> list[tuple[LabResult, CriticalLabFlag]]:
    return await LabSafetyService(db).check_patient_labs(
        account_id=account_id, patient_id=patient.id, audit=False
    )


@pytest.mark.parametrize(
    ("older_name", "newer_name"),
    [
        ("Potassium", "potassium"),
        ("potassium", "POTASSIUM"),
        ("Potassium", " Potassium "),
        ("POTASSIUM", "Potassium"),
    ],
)
async def test_a_newer_panic_value_is_found_whatever_case_it_was_recorded_in(
    db, older_name, newer_name
):
    """The bug in its most dangerous direction: newer value critical, older value normal.

    Parametrised over the orderings that matter, because the failure depended on how the two
    spellings sorted against each other -- fixing only the pair that happened to be tested
    would leave the mirror image broken.
    """
    account_id, patient = await _account_and_patient(db)
    db.add(_potassium(patient.id, older_name, NORMAL_K, datetime(2026, 1, 1, tzinfo=UTC)))
    db.add(_potassium(patient.id, newer_name, PANIC_K, datetime(2026, 6, 1, tzinfo=UTC)))
    await db.commit()

    flagged = await _flags(db, account_id, patient)

    assert len(flagged) == 1, flagged
    lab, flag = flagged[0]
    assert float(lab.value_numeric) == PANIC_K
    assert flag.severity == "panic_high"


@pytest.mark.parametrize(
    ("older_name", "newer_name"),
    [("Potassium", "potassium"), ("potassium", "POTASSIUM")],
)
async def test_a_resolved_panic_value_is_not_re_raised_from_an_older_row(
    db, older_name, newer_name
):
    """The same fix in the other direction, which matters just as much.

    Over-flagging is its own safety failure: a screen that keeps raising a potassium the
    clinician has already treated and re-drawn is a screen they learn to dismiss. The newer
    normal result must win over the older critical one, whatever case either was recorded in.
    """
    account_id, patient = await _account_and_patient(db)
    db.add(_potassium(patient.id, older_name, PANIC_K, datetime(2026, 1, 1, tzinfo=UTC)))
    db.add(_potassium(patient.id, newer_name, NORMAL_K, datetime(2026, 6, 1, tzinfo=UTC)))
    await db.commit()

    assert await _flags(db, account_id, patient) == []


async def test_spellings_of_one_marker_are_never_screened_as_two_markers(db):
    """The grouping is one key, so three spellings of potassium yield at most one flag —
    not three copies of the same finding for a clinician to reconcile."""
    account_id, patient = await _account_and_patient(db)
    for i, name in enumerate(("Potassium", "POTASSIUM", " potassium ")):
        db.add(_potassium(patient.id, name, PANIC_K, datetime(2026, 1, i + 1, tzinfo=UTC)))
    await db.commit()

    assert len(await _flags(db, account_id, patient)) == 1


async def test_two_results_from_one_draw_resolve_the_same_way_every_time(db):
    """Determinism on the tie, which is the property a coin flip lacks.

    The rule is most-recently-*recorded* wins: with the sample date equal, the later row to
    reach the record is the one the clinician most recently confirmed. What this pins is that
    there is a rule at all — the same chart must not screen clean on one request and panic on
    the next.
    """
    account_id, patient = await _account_and_patient(db)
    drawn = datetime(2026, 6, 1, tzinfo=UTC)
    first = _potassium(patient.id, "Potassium", PANIC_K, drawn)
    first.created_at = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    second = _potassium(patient.id, "Potassium", NORMAL_K, drawn)
    second.created_at = datetime(2026, 6, 1, 17, 0, tzinfo=UTC)
    db.add_all([first, second])
    await db.commit()

    # The corrected 4.0 was recorded later, so it is the one screened — repeatedly.
    for _ in range(3):
        assert await _flags(db, account_id, patient) == []


async def test_an_undated_result_never_outranks_a_dated_one(db):
    """`sample_date` is nullable — a report with no legible date still gets ingested. NULLS
    LAST means it is the fallback for a marker, not the answer for one that has a real date."""
    account_id, patient = await _account_and_patient(db)
    db.add(_potassium(patient.id, "Potassium", PANIC_K, None))
    db.add(_potassium(patient.id, "potassium", NORMAL_K, datetime(2026, 1, 1, tzinfo=UTC)))
    await db.commit()

    assert await _flags(db, account_id, patient) == []


async def test_an_undated_result_is_still_screened_when_it_is_all_there_is(db):
    """The other half of NULLS LAST: last is not excluded."""
    account_id, patient = await _account_and_patient(db)
    db.add(_potassium(patient.id, "Potassium", PANIC_K, None))
    await db.commit()

    flagged = await _flags(db, account_id, patient)

    assert len(flagged) == 1
    assert flagged[0][1].severity == "panic_high"


async def test_a_soft_deleted_row_cannot_be_the_latest(db):
    """A row a clinician removed from the record must not be the one the screen evaluates —
    in either direction, which is why the assertion is on the value that *was* used."""
    account_id, patient = await _account_and_patient(db)
    live = _potassium(patient.id, "Potassium", NORMAL_K, datetime(2026, 1, 1, tzinfo=UTC))
    deleted = _potassium(patient.id, "potassium", PANIC_K, datetime(2026, 6, 1, tzinfo=UTC))
    deleted.is_deleted = True
    db.add_all([live, deleted])
    await db.commit()

    assert await _flags(db, account_id, patient) == []
