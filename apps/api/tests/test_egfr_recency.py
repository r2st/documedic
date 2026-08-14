"""Which eGFR the renal hard-block is evaluated against, when the chart holds more than one.

``SafetyService._latest_egfr`` feeds ``SafetyContext.egfr``, which is the *only* input to the
renal band in ``core.safety._evaluate_renal`` — the check that hard-blocks metformin below an
eGFR of 30. Every other test of that path gives the patient a single derived marker, so "latest"
is never exercised as a decision. It is a decision with a clinical consequence: the marker that
loses is the number the block is computed from.

This product's premise is that histories arrive fragmented and out of order — a patient's
plastic bag of lab reports gets uploaded in whatever sequence it is handed over. So the case
below is the ordinary one, not a contrived one: today's upload is an *old* report.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.derived_marker import DerivedMarker
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.models.user import Account
from app.services.safety_service import SafetyService

pytestmark = pytest.mark.asyncio

STALE_EGFR = 95.0  # healthy: metformin is unremarkable
CURRENT_EGFR = 22.0  # below the 30 threshold: metformin is a hard block


async def _patient(db) -> tuple[uuid.UUID, Patient]:
    account_id = (await db.execute(select(Account.id))).scalars().first()
    if account_id is None:
        account = Account(email="egfr@example.com", password_hash="x", display_name="Dr Renal")
        db.add(account)
        await db.flush()
        account_id = account.id
    patient = Patient(
        account_id=account_id,
        full_name="Renal Patient",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account_id, patient


async def _egfr(
    db,
    patient: Patient,
    *,
    value: float,
    sample_date: datetime | None,
    computed_at: datetime,
) -> None:
    """One eGFR marker, with the creatinine it was derived from.

    The creatinine value is derived from ``value`` only so that two undated draws are distinct
    observations — ``lab_observation_key`` folds the value and the sample date together, and two
    identical undated rows are the same observation by definition.
    """
    lab = LabResult(
        patient_id=patient.id,
        marker_name="Creatinine",
        value_numeric=Decimal(str(round(100.0 / value, 3))),
        unit="mg/dL",
        sample_date=sample_date,
    )
    db.add(lab)
    await db.flush()
    db.add(
        DerivedMarker(
            patient_id=patient.id,
            source_lab_result_id=lab.id,
            marker_name="eGFR",
            value_numeric=Decimal(str(value)),
            unit="mL/min/1.73m2",
            formula_name="CKD-EPI 2021",
            formula_version="2021",
            input_values={},
            computed_at=computed_at,
        )
    )
    await db.flush()


async def test_an_old_report_uploaded_today_does_not_become_the_current_egfr(db):
    """The renal block must read the most recent *sample*, not the most recent upload.

    Both markers below are computed today, because both documents were ingested today. The one
    with the later ``computed_at`` is derived from a creatinine drawn seven years ago. Ordering
    by ``computed_at`` — the timestamp of the arithmetic, not of the blood draw — hands the
    engine an eGFR of 95 for a patient whose current eGFR is 22, and metformin comes back clear.
    """
    _, patient = await _patient(db)
    now = datetime.now(UTC)

    # Current renal function: drawn last month, uploaded yesterday.
    await _egfr(
        db,
        patient,
        value=CURRENT_EGFR,
        sample_date=now - timedelta(days=30),
        computed_at=now - timedelta(days=1),
    )
    # An old report from the same plastic bag, uploaded today.
    await _egfr(
        db,
        patient,
        value=STALE_EGFR,
        sample_date=now - timedelta(days=365 * 7),
        computed_at=now,
    )

    egfr = await SafetyService(db)._latest_egfr(patient.id)
    assert egfr == pytest.approx(CURRENT_EGFR), (
        f"expected the marker from the most recent blood draw ({CURRENT_EGFR}); "
        f"got {egfr} — the ordering is following upload time, not sample time"
    )


async def test_a_marker_with_no_sample_date_falls_back_to_when_it_was_computed(db):
    """An undated source lab is not a reason to drop the marker out of the ordering.

    Sample dates come out of OCR and are routinely missing. With nothing better to sort on,
    when the value was computed is the best available proxy — which is the behaviour this
    module has always had, kept here for the rows that have nothing else.
    """
    _, patient = await _patient(db)
    now = datetime.now(UTC)

    await _egfr(
        db, patient, value=STALE_EGFR, sample_date=None, computed_at=now - timedelta(days=2)
    )
    await _egfr(db, patient, value=CURRENT_EGFR, sample_date=None, computed_at=now)

    assert await SafetyService(db)._latest_egfr(patient.id) == pytest.approx(CURRENT_EGFR)


async def test_a_dated_sample_outranks_an_undated_one_computed_later(db):
    """A marker whose draw date is known and recent beats one whose date was never read.

    The undated row could be from any time; the dated row is evidence. Falling back to
    ``computed_at`` for the undated row keeps both comparable on one axis rather than
    excluding either.
    """
    _, patient = await _patient(db)
    now = datetime.now(UTC)

    await _egfr(
        db,
        patient,
        value=CURRENT_EGFR,
        sample_date=now - timedelta(days=1),
        computed_at=now - timedelta(days=1),
    )
    # No sample date, and computed *before* the dated row above, so it must not win.
    await _egfr(
        db, patient, value=STALE_EGFR, sample_date=None, computed_at=now - timedelta(days=3)
    )

    assert await SafetyService(db)._latest_egfr(patient.id) == pytest.approx(CURRENT_EGFR)


async def test_a_marker_with_no_source_lab_still_participates(db):
    """Markers are not always traceable to a single lab row; they must still be orderable."""
    _, patient = await _patient(db)
    now = datetime.now(UTC)

    db.add(
        DerivedMarker(
            patient_id=patient.id,
            source_lab_result_id=None,
            marker_name="eGFR",
            value_numeric=Decimal(str(CURRENT_EGFR)),
            unit="mL/min/1.73m2",
            formula_name="CKD-EPI 2021",
            formula_version="2021",
            input_values={},
            computed_at=now,
        )
    )
    await db.flush()

    assert await SafetyService(db)._latest_egfr(patient.id) == pytest.approx(CURRENT_EGFR)
