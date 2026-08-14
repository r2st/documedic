"""A lab value printed without a unit must not be assumed to be in the canonical unit.

The unit lives in a column header or a footnote, and columnar extraction does not always carry
it down to the row. Assuming it produced a confident panic flag on six entirely ordinary Indian
lab results, and — through the creatinine conversion the eGFR derivation shares — a fabricated
eGFR of 0.4 written into the chart with the metformin renal hard block hanging off it.

See ``app.core.lab_safety._UNITLESS_BANDS``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.clinical import serum_creatinine_mg_dl
from app.core.lab_safety import evaluate_critical_value, unreadable_lab
from app.models.derived_marker import DerivedMarker
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.models.user import Account
from app.services.graph_service import GraphService
from app.services.lab_safety_service import LabSafetyService

from .conftest import create_patient

# (marker, value) pairs that are ordinary results in a unit these reports commonly print, and
# which the guard used to read as though they were in its own canonical unit.
NORMAL_BUT_MISREAD = [
    ("Serum Creatinine", 88.0),  # µmol/L — a normal 1.0 mg/dL
    ("Platelet Count", 250000.0),  # per cumm — a normal 250 x10^3/µL
    ("Total Leukocyte Count", 8000.0),  # per cumm — a normal 8 x10^3/µL
    ("Fasting Blood Glucose", 5.5),  # mmol/L — a normal 99 mg/dL
    ("Hemoglobin", 120.0),  # g/L — a normal 12 g/dL
    ("Serum Calcium", 2.4),  # mmol/L — a normal 9.6 mg/dL
]

# Values only readable one way, so a missing unit costs nothing and the guard must still fire.
# These are the flags the fix must not throw away to buy the ones above.
GENUINELY_CRITICAL_UNITLESS = [
    ("Potassium", 6.8, "panic_high"),
    ("Potassium", 2.4, "panic_low"),
    ("Sodium", 118.0, "critical_low"),
    ("Sodium", 112.0, "panic_low"),
    ("Serum Creatinine", 12.0, "panic_high"),
    ("Platelet Count", 8.0, "panic_low"),
    ("WBC", 0.8, "panic_low"),
    ("Hemoglobin", 4.5, "panic_low"),
    ("Serum Calcium", 13.5, "critical_high"),
    ("Glucose", 450.0, "critical_high"),
    ("INR", 7.0, "critical_high"),
]


@pytest.mark.parametrize(("marker", "value"), NORMAL_BUT_MISREAD)
def test_normal_value_without_a_unit_is_not_flagged(marker: str, value: float) -> None:
    assert evaluate_critical_value(marker, value, None) is None


@pytest.mark.parametrize(("marker", "value"), NORMAL_BUT_MISREAD)
def test_ambiguous_value_without_a_unit_is_reported_as_unread(marker: str, value: float) -> None:
    """Not flagged is not the same as fine — the row has to say it was skipped."""
    note = unreadable_lab(marker, value, None)
    assert note is not None
    assert note.reason == "unit_missing"
    assert "not the same as a normal result" in note.summary


@pytest.mark.parametrize(("marker", "value", "severity"), GENUINELY_CRITICAL_UNITLESS)
def test_unambiguous_critical_value_without_a_unit_still_flags(
    marker: str, value: float, severity: str
) -> None:
    flag = evaluate_critical_value(marker, value, None)
    assert flag is not None
    assert flag.severity == severity
    assert unreadable_lab(marker, value, None) is None


def test_supplying_the_unit_restores_the_reading() -> None:
    """The ambiguity is in the missing unit, not in the value: naming the unit resolves it."""
    assert evaluate_critical_value("Serum Creatinine", 88.0, "umol/L") is None
    mg_dl = evaluate_critical_value("Serum Creatinine", 88.0, "mg/dL")
    assert mg_dl is not None and mg_dl.severity == "panic_high"
    assert evaluate_critical_value("Platelet Count", 250000.0, "/cumm") is None
    assert evaluate_critical_value("Total Leukocyte Count", 8000.0, "/cumm") is None
    assert evaluate_critical_value("Fasting Blood Glucose", 5.5, "mmol/L") is None


def test_a_value_ambiguous_in_both_directions_is_reported_rather_than_guessed() -> None:
    """A bare 30 glucose is a panic low in mg/dL and a crisis in mmol/L. Say so."""
    assert evaluate_critical_value("Glucose", 30.0, None) is None
    note = unreadable_lab("Glucose", 30.0, None)
    assert note is not None and note.reason == "unit_missing"


def test_a_bare_number_outside_every_scale_is_not_converted_into_one() -> None:
    """Inside exactly one other unit's span is still not the document saying which."""
    assert evaluate_critical_value("Serum Creatinine", 500.0, None) is None
    note = unreadable_lab("Serum Creatinine", 500.0, None)
    assert note is not None and note.reason == "unit_missing"


def test_an_uninterpretable_unit_is_reported_rather_than_silently_skipped() -> None:
    """The pre-existing skip on an unconvertible unit was silent too."""
    assert evaluate_critical_value("Potassium", 5.0, "furlongs") is None
    note = unreadable_lab("Potassium", 5.0, "furlongs")
    assert note is not None
    assert note.reason == "unit_unrecognised"
    assert "furlongs" in note.summary


def test_an_uncurated_marker_is_not_reported_as_unread() -> None:
    """This module has no threshold for HbA1c, so it was never going to evaluate it."""
    assert unreadable_lab("HbA1c", 9.2, None) is None
    assert unreadable_lab("HbA1c", 9.2, "%") is None


def test_a_row_with_no_value_is_not_reported_as_unread() -> None:
    assert unreadable_lab("Potassium", None, "mmol/L") is None


def test_unitless_creatinine_outside_the_mg_dl_scale_derives_no_egfr() -> None:
    """The eGFR derivation shares this conversion; a guess here is a number in the chart."""
    assert serum_creatinine_mg_dl("Serum Creatinine", Decimal("88"), None) is None
    assert serum_creatinine_mg_dl("Serum Creatinine", Decimal("1.1"), None) == pytest.approx(1.1)


async def _account_and_patient(db) -> tuple[uuid.UUID, Patient]:
    account_id = (await db.execute(select(Account.id))).scalars().first()
    if account_id is None:
        account = Account(email="unitless@example.com", password_hash="x", display_name="Dr Unit")
        db.add(account)
        await db.flush()
        account_id = account.id
    patient = Patient(
        account_id=account_id,
        full_name="Unitless Patient",
        date_of_birth=date(1970, 3, 4),
        sex="male",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return account_id, patient


@pytest.mark.asyncio
async def test_screen_reports_unreadable_rows_and_audits_them(db) -> None:
    account_id, patient = await _account_and_patient(db)
    db.add_all(
        [
            LabResult(
                patient_id=patient.id,
                marker_name="Serum Creatinine",
                value_numeric=Decimal("88"),
                unit=None,
                sample_date=date(2026, 1, 5),
            ),
            LabResult(
                patient_id=patient.id,
                marker_name="Potassium",
                value_numeric=Decimal("6.9"),
                unit="mmol/L",
                sample_date=date(2026, 1, 5),
            ),
        ]
    )
    await db.flush()

    screen = await LabSafetyService(db).screen_labs(account_id=account_id, patient_id=patient.id)
    await db.commit()

    assert [f.canonical_marker for _, f in screen.flags] == ["potassium"]
    assert [n.canonical_marker for _, n in screen.unreadable] == ["creatinine"]

    from app.models.audit_log import AuditLog

    actions = (
        (await db.execute(select(AuditLog.action).where(AuditLog.patient_id == patient.id)))
        .scalars()
        .all()
    )
    assert "critical_lab_value_not_evaluated" in actions


@pytest.mark.asyncio
async def test_a_chart_of_only_unreadable_rows_does_not_look_clean(db) -> None:
    """The chart where the omission is invisible: no flags, and nothing else said."""
    account_id, patient = await _account_and_patient(db)
    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name="Platelet Count",
            value_numeric=Decimal("250000"),
            unit=None,
            sample_date=date(2026, 2, 1),
        )
    )
    await db.flush()

    screen = await LabSafetyService(db).screen_labs(
        account_id=account_id, patient_id=patient.id, audit=False
    )
    assert screen.flags == []
    assert len(screen.unreadable) == 1


@pytest.mark.asyncio
async def test_no_egfr_is_derived_from_a_unitless_micromolar_creatinine(db) -> None:
    """End to end through the derivation: no eGFR at all, rather than a fabricated 0.4."""
    _, patient = await _account_and_patient(db)
    lab = LabResult(
        patient_id=patient.id,
        marker_name="Serum Creatinine",
        value_numeric=Decimal("88"),
        unit=None,
        sample_date=date(2026, 1, 9),
    )
    db.add(lab)
    await db.flush()

    await GraphService(db)._compute_derived_markers(patient, [lab])
    await db.flush()

    markers = (
        (await db.execute(select(DerivedMarker).where(DerivedMarker.patient_id == patient.id)))
        .scalars()
        .all()
    )
    assert markers == []


@pytest.mark.asyncio
async def test_critical_flags_endpoint_returns_the_unreadable_rows(auth_client) -> None:
    patient = await create_patient(auth_client)
    upload = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={
            "file": (
                "labs.pdf",
                b"%PDF-1.4\nLABS:\nPlatelet Count: 250000\nSodium: 140 mmol/L (135-145)\n",
                "application/pdf",
            )
        },
    )
    assert upload.status_code == 201, upload.text
    approve = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents/{upload.json()['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 200, approve.text

    resp = await auth_client.get(f"/api/v1/patients/{patient['id']}/labs/critical-flags")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["flags"] == []
    assert [row["marker_name"] for row in body["unreadable"]] == ["Platelet Count"]
    assert body["unreadable"][0]["reason"] == "unit_missing"
