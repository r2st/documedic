"""How much of a chart the panic-value screen has to read to answer.

``LabSafetyService.check_patient_labs`` evaluates exactly one row per marker — the most recent
one. It used to *find* that row by reading every lab result the patient has ever had into
Python and keeping the first of each run, which made the cost of a safety screen grow with the
length of the record rather than with the number of distinct markers in it. A chart with 3000
results across 40 markers built 3000 ORM instances to use 40 of them, and this screen runs on
every document ingest and every record read.

The selection is now a ``ROW_NUMBER() OVER (PARTITION BY lower(trim(marker_name)))`` filtered
to rank 1, so the database returns the 40 rows and nothing else.

These tests are about that boundary, not about which row wins — the choosing rules are pinned
in ``test_lab_safety_latest_per_marker``. Two testing hazards would let a regression pass here:

* Objects already in the session's identity map cost no SQL (the hazard
  ``test_query_efficiency`` documents), so every measurement calls ``db.expunge_all()`` first —
  which is what a production request, with its fresh session, actually looks like.
* The identity map holds *weak* references, so counting what is in it afterwards undercounts
  every row the caller did not keep a reference to — which on this path is every row that was
  read and found normal, i.e. exactly the rows a regression would add. Rows are therefore
  counted as they are loaded, via the ``loaded_as_persistent`` event.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.sql import ClauseElement

from app.models.audit_log import AuditLog
from app.models.lab_result import LabResult
from app.models.patient import Patient
from app.models.user import Account
from app.services.lab_safety_service import LabSafetyService

pytestmark = pytest.mark.asyncio

# Each marker below is one the curated critical-value table knows, paired with a value that is
# unambiguously inside the panic band and one that is unambiguously normal. Screening a wide
# set of markers at once is the case where the old shape was most wasteful and the case a
# per-marker query loop would be slowest.
PANIC = {
    "Potassium": (7.4, "mmol/L"),
    "Sodium": (112.0, "mmol/L"),
    "Glucose": (520.0, "mg/dL"),
    "Hemoglobin": (4.2, "g/dL"),
    "Platelets": (8.0, "10^3/uL"),
    "Creatinine": (12.0, "mg/dL"),
    "INR": (9.5, "ratio"),
    "WBC": (0.8, "10^3/uL"),
}
NORMAL = {
    "Potassium": 4.0,
    "Sodium": 140.0,
    "Glucose": 95.0,
    "Hemoglobin": 13.5,
    "Platelets": 250.0,
    "Creatinine": 0.9,
    "INR": 1.1,
    "WBC": 7.0,
}


@contextmanager
def counting_queries(engine):
    """Yields the list of SQL statements executed on `engine` while the block runs."""
    statements: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)


@contextmanager
def counting_loaded_labs(db):
    """Yields a list that grows by one entry per ``LabResult`` the DB materialises into `db`.

    Counted at load time rather than by inspecting the identity map afterwards: the identity
    map is weak-referencing, so a row that was read and then dropped (every normal result, on
    this path) would have vanished from it by the time a test looked.
    """
    loaded: list[LabResult] = []

    def _record(session, instance):
        if isinstance(instance, LabResult):
            loaded.append(instance)

    event.listen(db.sync_session, "loaded_as_persistent", _record)
    try:
        yield loaded
    finally:
        event.remove(db.sync_session, "loaded_as_persistent", _record)


@contextmanager
def capturing_orm_statements(db):
    """Yields a list of the pre-compilation ORM statements executed on `db`.

    Capturing the statement *object* rather than the SQL string is what lets a test compile the
    same read under a dialect the test database is not — see the PostgreSQL portability test.
    """
    statements: list[ClauseElement] = []

    def _capture(orm_execute_state):
        statements.append(orm_execute_state.statement)

    event.listen(db.sync_session, "do_orm_execute", _capture)
    try:
        yield statements
    finally:
        event.remove(db.sync_session, "do_orm_execute", _capture)


async def _patient_of(db, account_id: uuid.UUID) -> Patient:
    patient = Patient(
        account_id=account_id,
        full_name="Bounded Read Patient",
        consent_given=True,
        consent_given_at=datetime.now(UTC),
    )
    db.add(patient)
    await db.flush()
    return patient


async def _account_and_patient(db) -> tuple[uuid.UUID, Patient]:
    account = Account(
        email=f"bounded-{uuid.uuid4().hex}@example.com", password_hash="x", display_name="Dr Bound"
    )
    db.add(account)
    await db.flush()
    return account.id, await _patient_of(db, account.id)


async def _seed_history(db, patient: Patient, markers: list[str], *, depth: int) -> None:
    """Give `patient` `depth` results for each of `markers`, newest last and panic-valued.

    The older rows are normal, so a screen that evaluated the wrong row of a marker would
    report *nothing* for it — the failure is visible in the flags, not only in the row count.
    """
    for marker in markers:
        panic_value, unit = PANIC[marker]
        for i in range(depth):
            newest = i == depth - 1
            db.add(
                LabResult(
                    patient_id=patient.id,
                    marker_name=marker,
                    value_numeric=panic_value if newest else NORMAL[marker],
                    unit=unit,
                    sample_date=datetime(2026, 1, 1, tzinfo=UTC).replace(day=1 + i),
                )
            )
    await db.commit()


def _lab_reads(statements: list[str]) -> list[str]:
    return [s for s in statements if "lab_results" in s]


async def test_only_the_screened_row_per_marker_is_ever_materialised(db):
    """The read returns one row per marker, not one row per result.

    Eight markers with twenty results each is 160 rows in the table and 8 rows worth screening.
    Counting what the DB materialises measures the exact quantity the old whole-history read
    let grow without bound.
    """
    account_id, patient = await _account_and_patient(db)
    markers = list(PANIC)
    await _seed_history(db, patient, markers, depth=20)

    db.expunge_all()
    with counting_loaded_labs(db) as loaded:
        flagged = await LabSafetyService(db).check_patient_labs(
            account_id=account_id, patient_id=patient.id, audit=False
        )

    assert len(loaded) == len(markers)
    # ...and the rows it did read are the right ones: every marker's newest value is the panic
    # one, so a screen reading a different row per marker would come back short.
    assert sorted(lab.marker_name for lab, _ in flagged) == sorted(markers)


async def test_the_cost_of_the_screen_does_not_grow_with_the_length_of_the_record(db):
    """Same markers, twenty times the history — same number of rows read.

    This is the property the row count alone cannot show: 8 rows read from a 160-row history
    could equally be 8 rows read from an 8-row one. Holding the marker set fixed and varying
    only the depth isolates the axis that used to be unbounded.
    """
    account_id, shallow = await _account_and_patient(db)
    deep = await _patient_of(db, account_id)
    markers = list(PANIC)
    await _seed_history(db, shallow, markers, depth=1)
    await _seed_history(db, deep, markers, depth=20)

    service = LabSafetyService(db)
    db.expunge_all()
    with counting_loaded_labs(db) as shallow_rows:
        await service.check_patient_labs(account_id=account_id, patient_id=shallow.id, audit=False)

    db.expunge_all()
    with counting_loaded_labs(db) as deep_rows:
        await service.check_patient_labs(account_id=account_id, patient_id=deep.id, audit=False)

    assert len(shallow_rows) == len(deep_rows) == len(markers)


async def test_the_screen_is_one_statement_however_many_markers_it_covers(db, engine):
    """Rank-and-filter in SQL, not a query per marker.

    "One row per marker" is also satisfiable by looping markers and querying each — which
    trades an unbounded transfer for an unbounded round-trip count, the worse of the two over a
    network. The statement count must not move when the marker set grows.
    """
    account_id, few = await _account_and_patient(db)
    many = await _patient_of(db, account_id)
    await _seed_history(db, few, list(PANIC)[:2], depth=3)
    await _seed_history(db, many, list(PANIC), depth=3)

    service = LabSafetyService(db)

    db.expunge_all()
    with counting_queries(engine) as few_statements:
        await service.check_patient_labs(account_id=account_id, patient_id=few.id, audit=False)

    db.expunge_all()
    with counting_queries(engine) as many_statements:
        await service.check_patient_labs(account_id=account_id, patient_id=many.id, audit=False)

    assert len(_lab_reads(few_statements)) == 1
    assert len(_lab_reads(many_statements)) == 1


async def test_the_row_per_marker_is_chosen_by_a_window_function(db, engine):
    """Pins the mechanism, because the observable behaviour is identical either way.

    Every other test in this file passes for a Python-side scan that happens to be given a
    small table. This one fails for it.
    """
    account_id, patient = await _account_and_patient(db)
    await _seed_history(db, patient, ["Potassium", "Sodium"], depth=3)

    db.expunge_all()
    with counting_queries(engine) as statements:
        await LabSafetyService(db).check_patient_labs(
            account_id=account_id, patient_id=patient.id, audit=False
        )

    lab_sql = " ".join(_lab_reads(statements)).lower()
    assert "row_number() over (partition by lower(trim(lab_results.marker_name))" in lab_sql
    # The rank filter is what makes it a *bounded* read rather than a decorated whole-set read.
    assert "marker_rank" in lab_sql


async def test_the_same_read_compiles_on_postgresql(db):
    """Portability, asserted against the dialect the tests do not run on.

    The suite runs on SQLite and production runs on PostgreSQL 16, so a read that quietly
    depended on one dialect's grammar would ship green. Window functions with a NULLS LAST
    ordering are plain SQL:2003 that both parse natively — this checks that the statement this
    service builds really is that, and not a construct only one side accepts (PostgreSQL's
    ``DISTINCT ON`` being the obvious tempting non-portable alternative).
    """
    account_id, patient = await _account_and_patient(db)
    await _seed_history(db, patient, ["Potassium"], depth=2)

    db.expunge_all()
    with capturing_orm_statements(db) as statements:
        await LabSafetyService(db).check_patient_labs(
            account_id=account_id, patient_id=patient.id, audit=False
        )

    compiled = {
        name: [str(s.compile(dialect=dialect)).lower() for s in statements]
        for name, dialect in (("pg", postgresql.dialect()), ("sqlite", sqlite.dialect()))
    }
    for dialect_name, rendered in compiled.items():
        lab_reads = [sql for sql in rendered if "from lab_results" in sql]
        assert len(lab_reads) == 1, f"{dialect_name}: {rendered}"
        sql = lab_reads[0]
        assert "row_number() over (partition by lower(trim(lab_results.marker_name))" in sql
        # sample_date is nullable, so an undated result must sort last rather than first —
        # which is the default a PostgreSQL DESC ordering would otherwise give it.
        assert "order by lab_results.sample_date desc nulls last" in sql
        assert "distinct on" not in sql


async def test_a_deleted_row_is_excluded_before_it_can_win_the_rank(db):
    """The soft-delete predicate lives inside the ranked subquery, not outside it.

    Filtering after the ranking would let a deleted row take rank 1 and then be discarded,
    leaving the marker unscreened entirely — a strictly worse failure than the pre-window code
    had, and one no behavioural test of a single-row marker would notice.
    """
    account_id, patient = await _account_and_patient(db)
    live = LabResult(
        patient_id=patient.id,
        marker_name="Potassium",
        value_numeric=7.4,
        unit="mmol/L",
        sample_date=datetime(2026, 1, 1, tzinfo=UTC),
    )
    deleted = LabResult(
        patient_id=patient.id,
        marker_name="potassium",
        value_numeric=4.0,
        unit="mmol/L",
        sample_date=datetime(2026, 6, 1, tzinfo=UTC),
    )
    deleted.is_deleted = True
    db.add_all([live, deleted])
    await db.commit()

    db.expunge_all()
    flagged = await LabSafetyService(db).check_patient_labs(
        account_id=account_id, patient_id=patient.id, audit=False
    )

    assert [float(lab.value_numeric) for lab, _ in flagged] == [7.4]


async def test_flags_come_back_in_a_stable_order_across_repeated_screens(db):
    """A window function makes no promise about the order rows leave the outer query in.

    The old read got its ordering incidentally from the ORDER BY it used to do the dedup. The
    flag list is copied into an audit-log payload clinicians compare across requests, so the
    order is part of the record: the same chart must serialise the same way every time.
    """
    account_id, patient = await _account_and_patient(db)
    await _seed_history(db, patient, list(PANIC), depth=2)

    service = LabSafetyService(db)
    orders = []
    for _ in range(3):
        db.expunge_all()
        flagged = await service.check_patient_labs(
            account_id=account_id, patient_id=patient.id, audit=False
        )
        orders.append([lab.marker_name for lab, _ in flagged])

    assert orders[0] == orders[1] == orders[2]
    # Marker-alphabetical on the normalised key, which is the partition key itself.
    assert orders[0] == sorted(orders[0], key=lambda name: name.strip().lower())


async def test_an_alias_spelling_is_screened_in_its_own_right(db):
    """A marker's alias spelling is its own partition, and both spellings get screened.

    The partition key is the marker name as recorded, normalised for case and whitespace — not
    the canonical marker the alias table maps it to. So a chart carrying both spellings can
    raise a flag for each. That direction is the safe one: every distinct spelling's newest
    value is evaluated, and nothing a lab reported can hide behind a synonym of itself.
    """
    account_id, patient = await _account_and_patient(db)
    for name in ("Potassium", "Serum Potassium"):
        db.add(
            LabResult(
                patient_id=patient.id,
                marker_name=name,
                value_numeric=7.4,
                unit="mmol/L",
                sample_date=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
    await db.commit()

    db.expunge_all()
    flagged = await LabSafetyService(db).check_patient_labs(
        account_id=account_id, patient_id=patient.id, audit=False
    )

    assert sorted(lab.marker_name for lab, _ in flagged) == ["Potassium", "Serum Potassium"]
    assert {flag.canonical_marker for _, flag in flagged} == {"potassium"}


async def test_one_patients_markers_do_not_partition_with_anothers(db):
    """The patient predicate is inside the ranked subquery too.

    Ranking across the whole table and filtering by patient afterwards would let another
    patient's newer potassium take rank 1 and suppress this patient's panic value — a
    cross-tenant read manifesting as a *missing* safety flag rather than as leaked data.
    """
    account_id, mine = await _account_and_patient(db)
    _, theirs = await _account_and_patient(db)
    db.add(
        LabResult(
            patient_id=mine.id,
            marker_name="Potassium",
            value_numeric=7.4,
            unit="mmol/L",
            sample_date=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    db.add(
        LabResult(
            patient_id=theirs.id,
            marker_name="Potassium",
            value_numeric=4.0,
            unit="mmol/L",
            sample_date=datetime(2026, 12, 1, tzinfo=UTC),
        )
    )
    await db.commit()

    db.expunge_all()
    flagged = await LabSafetyService(db).check_patient_labs(
        account_id=account_id, patient_id=mine.id, audit=False
    )

    assert [float(lab.value_numeric) for lab, _ in flagged] == [7.4]


async def test_a_marker_whose_newest_row_is_qualitative_is_not_screened_from_an_older_one(db):
    """Unchanged semantics at the boundary, stated explicitly because the rewrite touches it.

    "Most recent per marker" picks the row *before* the numeric check, so a marker whose newest
    result is qualitative ("haemolysed sample") is skipped rather than falling back to the last
    numeric value. Falling back would report a superseded number as current, which on a panic
    screen is a flag the clinician has already acted on.
    """
    account_id, patient = await _account_and_patient(db)
    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name="Potassium",
            value_numeric=7.4,
            unit="mmol/L",
            sample_date=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    db.add(
        LabResult(
            patient_id=patient.id,
            marker_name="POTASSIUM",
            value_numeric=None,
            value_text="haemolysed sample",
            unit="mmol/L",
            sample_date=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    await db.commit()

    db.expunge_all()
    with counting_loaded_labs(db) as loaded:
        flagged = await LabSafetyService(db).check_patient_labs(
            account_id=account_id, patient_id=patient.id, audit=False
        )

    assert flagged == []
    # The read still stopped at one row for the marker — it did not go looking for a fallback.
    assert len(loaded) == 1


async def test_a_patient_with_no_labs_reads_nothing_and_flags_nothing(db):
    """The empty case, which a subquery-plus-filter can get wrong in a way a flat select cannot."""
    account_id, patient = await _account_and_patient(db)
    await db.commit()

    db.expunge_all()
    with counting_loaded_labs(db) as loaded:
        result = await LabSafetyService(db).check_patient_labs(
            account_id=account_id, patient_id=patient.id, audit=False
        )

    assert result == []
    assert loaded == []


async def test_the_audit_entry_still_names_every_flagged_marker(db):
    """The audit write is fed from the bounded read, so it must not have been narrowed with it.

    ``audit=True`` is the production path (document ingest calls it that way); a screen that
    flags correctly but records a subset of what it flagged is a silent gap in the trail.
    """
    account_id, patient = await _account_and_patient(db)
    await _seed_history(db, patient, list(PANIC), depth=3)

    db.expunge_all()
    flagged = await LabSafetyService(db).check_patient_labs(
        account_id=account_id, patient_id=patient.id, audit=True
    )
    await db.commit()

    entry = (
        (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.patient_id == patient.id,
                    AuditLog.action == "critical_lab_value_detected",
                )
            )
        )
        .scalars()
        .one()
    )
    recorded = [f["marker_name"] for f in entry.payload["flags"]]
    assert recorded == [lab.marker_name for lab, _ in flagged]
    assert sorted(recorded) == sorted(PANIC)
