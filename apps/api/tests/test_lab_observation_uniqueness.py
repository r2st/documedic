"""The database-level half of "one blood draw is not two": ``uq_lab_results_observation``.

``GraphService`` already skips a lab observation it finds on the chart, but that check is a
read-then-insert and two overlapping approvals of one document can both pass it. The row lock in
``DocumentService.get`` closes the window on PostgreSQL and is silently dropped by SQLAlchemy's
SQLite dialect, so up to now the guarantee did not exist on every backend the product runs on.

A partial unique index on ``(patient_id, dedup_key) WHERE is_deleted = false`` closes it
everywhere: the second insert of one observation fails whatever the application layer believed.
These tests are therefore split in two. The first half pins the key function — what counts as
"the same observation" — because a key that is too coarse loses real clinical data (a
post-dialysis creatinine drawn the same day as the pre-dialysis one) and a key that is too fine
silently stops constraining anything. The second half proves the constraint actually fires, and
that the approval path turns it into an answer a client can act on rather than a 500.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.exceptions import ConcurrentApprovalError
from app.models.lab_result import LabResult, lab_observation_key
from app.models.patient import Patient
from app.models.user import Account
from app.services.document_service import _is_observation_conflict
from app.services.graph_service import GraphService
from tests.conftest import create_patient
from tests.test_graph_service_merge_dedup import _document, _entity

DOC = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_DOC = uuid.UUID("22222222-2222-2222-2222-222222222222")
DRAWN = datetime(2026, 8, 1, 9, 30, tzinfo=UTC)

# Parsed by the deterministic text parser, so the extraction under test carries exactly one lab
# observation and no LLM is involved.
RENAL_PANEL = b"%PDF-1.4\nLABS:\nCreatinine: 1.4 mg/dL (0.6-1.2)\n"


# --------------------------------------------------------------------------- the key itself


def test_the_same_observation_hashes_the_same_way_twice():
    """The floor: without this nothing below means anything."""
    assert lab_observation_key(DOC, "Creatinine", Decimal("1.4"), DRAWN) == lab_observation_key(
        DOC, "Creatinine", Decimal("1.4"), DRAWN
    )


@pytest.mark.parametrize(
    ("value", "equivalent"),
    [
        (Decimal("3.0"), Decimal("3.000000")),
        (Decimal("3"), Decimal("3.00")),
        (Decimal("100"), Decimal("1E+2")),
        (Decimal("0.50"), Decimal("0.5")),
    ],
)
def test_numerically_equal_values_are_one_observation(value: Decimal, equivalent: Decimal):
    """``Numeric(18, 6)`` re-spells what it is given, and the key must not care.

    A value parsed out of an extraction is ``Decimal("3.0")``; the same value read back off the
    column is ``Decimal("3.000000")``. If those hashed differently the constraint would let the
    duplicate straight through — and the in-memory dedup, which compares against keys read back
    from the database, would stop matching too.
    """
    assert lab_observation_key(DOC, "Creatinine", value, DRAWN) == lab_observation_key(
        DOC, "Creatinine", equivalent, DRAWN
    )


def test_the_same_instant_in_two_timezones_is_one_observation():
    """SQLite drops tzinfo on the round trip, so both spellings of one instant reach this."""
    ist = timezone(timedelta(hours=5, minutes=30))
    assert lab_observation_key(DOC, "Creatinine", Decimal("1.4"), DRAWN) == lab_observation_key(
        DOC, "Creatinine", Decimal("1.4"), DRAWN.astimezone(ist)
    )


def test_a_naive_timestamp_is_read_as_utc():
    """What comes back off SQLite is naive. Treating it as anything but UTC would split the key."""
    assert lab_observation_key(
        DOC, "Creatinine", Decimal("1.4"), DRAWN.replace(tzinfo=None)
    ) == lab_observation_key(DOC, "Creatinine", Decimal("1.4"), DRAWN)


@pytest.mark.parametrize("spelling", ["creatinine", "  Creatinine  ", "CREATININE"])
def test_marker_names_are_compared_case_and_whitespace_insensitively(spelling: str):
    """Extractors are not consistent about either, and neither is a lab's letterhead."""
    assert lab_observation_key(DOC, spelling, Decimal("1.4"), DRAWN) == lab_observation_key(
        DOC, "Creatinine", Decimal("1.4"), DRAWN
    )


@pytest.mark.parametrize(
    ("label", "args"),
    [
        ("a different document", (OTHER_DOC, "Creatinine", Decimal("1.4"), DRAWN)),
        ("a different marker", (DOC, "Sodium", Decimal("1.4"), DRAWN)),
        ("a different value", (DOC, "Creatinine", Decimal("2.9"), DRAWN)),
        ("a different draw time", (DOC, "Creatinine", Decimal("1.4"), DRAWN + timedelta(hours=6))),
        ("no value at all", (DOC, "Creatinine", None, DRAWN)),
        ("no draw time at all", (DOC, "Creatinine", Decimal("1.4"), None)),
    ],
)
def test_a_genuinely_different_observation_gets_a_different_key(label: str, args: tuple):
    """Each of these is a distinct clinical fact, and collapsing any of them loses a reading.

    The last two matter most for the constraint: a nullable column is where a composite unique
    index would have stopped constraining anything, since NULLs are distinct in a unique index on
    both dialects. Folding them into the digest as "" keeps those rows constrained — while
    keeping them distinct from a row that has a value.
    """
    assert lab_observation_key(*args) != lab_observation_key(
        DOC, "Creatinine", Decimal("1.4"), DRAWN
    ), f"{label} collided with the reference observation"


def test_two_undated_observations_of_one_marker_still_collide():
    """The other side of the above: absent is a value, not a wildcard."""
    assert lab_observation_key(DOC, "Creatinine", None, None) == lab_observation_key(
        DOC, "Creatinine", None, None
    )


def test_the_field_separator_cannot_be_forged_from_a_marker_name():
    """Concatenating components invites a marker name that reproduces a neighbouring field.

    The separator is a unit separator (0x1f), which no lab report contains, so this stays
    theoretical — but "impossible in practice" is what the assertion is for.
    """
    assert lab_observation_key(DOC, "Creatinine\x1f1.4", None, DRAWN) != lab_observation_key(
        DOC, "Creatinine", Decimal("1.4"), DRAWN
    )


# ------------------------------------------------------- the key as a derived column default


async def _patient(db) -> Patient:
    account = Account(email=f"uniq-{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(account)
    await db.flush()
    patient = Patient(
        account_id=account.id,
        full_name="Uniqueness Patient",
        sex="male",
        date_of_birth=datetime(1970, 1, 1).date(),
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


@pytest.mark.asyncio
async def test_a_row_inserted_without_a_key_still_gets_the_right_one(db):
    """The key is derived on insert, so no writer can omit it.

    Made a column default rather than something each caller passes precisely because a caller who
    forgets is the failure the constraint exists to prevent: the row would land unconstrained.
    """
    patient = await _patient(db)
    db.add(
        LabResult(
            patient_id=patient.id,
            source_document_id=None,
            marker_name="Creatinine",
            value_numeric=Decimal("1.4"),
            sample_date=DRAWN,
        )
    )
    await db.flush()

    stored = (await db.execute(select(LabResult.dedup_key))).scalar_one()
    assert stored == lab_observation_key(None, "Creatinine", Decimal("1.4"), DRAWN)


@pytest.mark.asyncio
async def test_deriving_a_key_survives_a_date_where_a_datetime_was_expected(db):
    """A column default must not be able to fail an insert it has no business failing.

    ``sample_date`` is a ``DateTime`` column, but a bare ``date`` reaches it intact — SQLAlchemy
    passes it through and the driver widens it. Reading ``.tzinfo`` off one would raise inside the
    default and take down an insert for a reason unrelated to deduplication.
    """
    patient = await _patient(db)
    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name="Potassium",
            value_numeric=110.0,  # a float, the other shape that reaches a Numeric column
            sample_date=datetime(2026, 8, 1).date(),
        )
    )
    await db.flush()

    assert (await db.execute(select(LabResult.dedup_key))).scalar_one()


# ----------------------------------------------------------------- the constraint firing


@pytest.mark.asyncio
async def test_the_database_refuses_a_second_copy_of_one_observation(db):
    """The whole point: bypass the application's dedup entirely and the row is still refused.

    Two identical inserts in one flush is what two overlapping approvals produce between them —
    neither saw the other's row when it decided to insert.
    """
    patient = await _patient(db)
    for _ in range(2):
        db.add(
            LabResult(
                patient_id=patient.id,
                source_document_id=DOC,
                marker_name="Creatinine",
                value_numeric=Decimal("1.4"),
                sample_date=DRAWN,
            )
        )

    with pytest.raises(IntegrityError) as caught:
        await db.flush()

    assert _is_observation_conflict(caught.value), (
        "the duplicate-observation constraint fired but DocumentService no longer recognises the "
        f"driver's wording, so an approval racing another would 500: {caught.value.orig}"
    )


@pytest.mark.asyncio
async def test_the_constraint_does_not_reach_across_patients(db):
    """One document belongs to one patient, but the index leads with patient_id regardless.

    Without it the digest alone would be unique table-wide, and two patients whose charts happen
    to carry the same undated marker with no numeric value would collide.
    """
    first, second = await _patient(db), await _patient(db)
    for patient in (first, second):
        db.add(
            LabResult(
                patient_id=patient.id,
                source_document_id=DOC,
                marker_name="Creatinine",
                value_numeric=Decimal("1.4"),
                sample_date=DRAWN,
            )
        )
    await db.flush()  # must not raise

    assert len((await db.execute(select(LabResult))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_a_soft_deleted_observation_does_not_block_re_merging_its_document(db):
    """Why the index is partial on ``is_deleted``.

    A full unique index would make a removed observation permanently unrepeatable, so a document
    whose merge was withdrawn could never be approved again — the chart would be stuck without a
    reading anybody could see, and the only evidence why would be a hidden row.
    """
    patient = await _patient(db)
    retired = LabResult(
        patient_id=patient.id,
        source_document_id=DOC,
        marker_name="Creatinine",
        value_numeric=Decimal("1.4"),
        sample_date=DRAWN,
    )
    db.add(retired)
    await db.flush()
    retired.is_deleted = True
    await db.flush()

    db.add(
        LabResult(
            patient_id=patient.id,
            source_document_id=DOC,
            marker_name="Creatinine",
            value_numeric=Decimal("1.4"),
            sample_date=DRAWN,
        )
    )
    await db.flush()  # must not raise

    live = (
        (await db.execute(select(LabResult).where(LabResult.is_deleted.is_(False)))).scalars().all()
    )
    assert len(live) == 1


@pytest.mark.asyncio
async def test_two_draws_of_one_marker_in_one_report_both_survive(db):
    """The constraint must not be coarser than the clinical fact.

    A pre- and post-dialysis creatinine on one report are two observations, and losing the second
    would misrepresent the treatment as having done nothing.
    """
    patient = await _patient(db)
    for value in (Decimal("8.1"), Decimal("2.4")):
        db.add(
            LabResult(
                patient_id=patient.id,
                source_document_id=DOC,
                marker_name="Creatinine",
                value_numeric=value,
                sample_date=DRAWN,
            )
        )
    await db.flush()  # must not raise

    values = (await db.execute(select(LabResult.value_numeric))).scalars().all()
    assert sorted(values) == [Decimal("2.4"), Decimal("8.1")]


@pytest.mark.asyncio
async def test_the_merge_still_deduplicates_before_the_constraint_has_to(db):
    """The constraint is the backstop, not the mechanism.

    A sequential re-approval — the case that actually happens — must still be answered by the
    in-memory dedup, quietly merging nothing. If it started relying on the constraint instead,
    every double-clicked Approve button would surface as a 409.
    """
    patient = await _patient(db)
    document = await _document(db, patient)
    entities = [_entity("lab_result", marker_name="Creatinine", value_numeric="1.4")]
    service = GraphService(db)

    assert (await service.merge_entities(patient=patient, document=document, entities=entities))[
        "lab_results"
    ] == 1
    assert (await service.merge_entities(patient=patient, document=document, entities=entities))[
        "lab_results"
    ] == 0


# ------------------------------------------------------------ what the clinician is told


def test_only_the_observation_constraint_is_treated_as_a_lost_race():
    """Anything else that violates a constraint during a merge is a bug, and must stay a 500.

    Reporting an unrelated integrity failure as "someone else approved first" would tell the
    clinician the chart is fine when it is not, and would bury the traceback that says otherwise.
    """

    class _Fake:
        def __init__(self, message: str) -> None:
            self.orig = message

    assert not _is_observation_conflict(
        _Fake("FOREIGN KEY constraint failed")  # type: ignore[arg-type]
    )
    assert not _is_observation_conflict(
        _Fake("UNIQUE constraint failed: audit_logs.sequence")  # type: ignore[arg-type]
    )
    assert _is_observation_conflict(
        _Fake('duplicate key value violates unique constraint "uq_lab_results_observation"')  # type: ignore[arg-type]
    ), "the PostgreSQL wording — the one that matters in production — is not matched"


def test_the_conflict_is_a_409_that_says_a_retry_is_safe():
    """A clinician reading this mid-consultation needs to know whether to approve again.

    The message is the whole answer to that: nothing merged twice, and re-approving is how you
    confirm a correction this attempt was carrying actually landed.
    """
    error = ConcurrentApprovalError()

    assert error.status_code == 409
    assert error.code == "concurrent_approval"
    assert "approve again" in error.message.lower()


@pytest.mark.asyncio
async def test_an_approval_that_loses_the_race_answers_409_rather_than_500(
    auth_client, monkeypatch
):
    """End to end over HTTP, with the race staged so the outcome is deterministic.

    The window cannot be opened for real on the shared in-memory test bind — one connection, so
    two sessions cannot genuinely overlap — and on a file-backed SQLite the two writers serialise
    on the database-wide write lock rather than reliably in the dedup window. So the losing side
    is reproduced directly: ``_existing_keys`` is made to report a chart with no lab observations
    on it, which is exactly what the loser of a race reads, and the insert then meets a row that
    is already committed.

    What this pins is the part that is not backend-specific and is the whole reason the constraint
    is worth having: the clinician gets a 409 saying the merge did not happen, not a 500 saying
    the system broke.
    """
    patient = await create_patient(auth_client)
    pid = patient["id"]
    upload = await auth_client.post(
        f"/api/v1/patients/{pid}/documents",
        files={"file": ("renal.pdf", RENAL_PANEL, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()["id"]
    body = {"corrections": [], "rejected_entity_indexes": []}

    winner = await auth_client.post(f"/api/v1/patients/{pid}/documents/{doc_id}/approve", json=body)
    assert winner.status_code == 200, winner.text
    assert winner.json()["merged"]["lab_results"] == 1

    original = GraphService._existing_keys

    async def _blind_to_labs(self, patient, source_doc_id):
        keys = await original(self, patient, source_doc_id)
        return {**keys, "lab_results": set()}

    monkeypatch.setattr(GraphService, "_existing_keys", _blind_to_labs)

    loser = await auth_client.post(f"/api/v1/patients/{pid}/documents/{doc_id}/approve", json=body)

    assert loser.status_code == 409, loser.text
    assert loser.json()["code"] == "concurrent_approval"

    monkeypatch.undo()
    record = await auth_client.get(f"/api/v1/patients/{pid}/record")
    assert [lab["marker_name"] for lab in record.json()["lab_results"]] == ["Creatinine"], (
        "the losing approval was rolled back, so the chart must read exactly as the winner left it"
    )
