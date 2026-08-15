"""The patient list is a total order, so paging it is a partition of the panel.

``RecordService`` learned this when its five sections were paged: LIMIT/OFFSET over a sort with
ties is not a partition of a set, because two rows that compare equal may come back in either
order between two requests. The patient list was the read that never got the same treatment. It
sorted on ``updated_at DESC`` alone, and ``updated_at`` is full of ties here — a seeded panel, a
bulk import, or simply two charts created inside the same clock tick share it — so walking the
pages could hand a clinician the same patient twice and never show them another one at all.

The realistic version is not exotic. Every patient a clinic onboards in one sitting shares the
value, and the clinician who notices is the one who cannot find a chart they know exists.

Both branches of ``PatientService.list`` are covered because they paginate by different
mechanisms: without a search term the page is a SQL LIMIT/OFFSET, and with one it is a Python
slice over the decrypted-and-filtered list. A tiebreaker in only one of them would leave the
search results — the way a clinician actually finds a patient — unstable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.patient import Patient
from app.models.user import Account
from app.services.patient_service import PatientService
from tests.test_query_efficiency import counting_queries

pytestmark = pytest.mark.asyncio

# Enough charts that paging them takes several requests, and few enough that the test is quick.
_PANEL_SIZE = 12
_PAGE = 5


async def _panel_sharing_one_timestamp(
    db, count: int, *, name: str = "Tied Patient"
) -> tuple[Account, list[Patient]]:
    """``count`` live charts for one account, every one carrying the same ``updated_at``.

    Written directly rather than through the API because the point is the tie: the API stamps
    ``updated_at`` from the database default, and two requests are milliseconds apart, so the
    collision this test is about would only show up under a coarser clock. Setting one value
    across the panel reproduces deterministically what a bulk import produces by accident.
    """
    account = Account(
        email=f"paging-{name.lower().replace(' ', '-')}@example.com",
        password_hash="x",
        display_name="Dr Paging",
    )
    db.add(account)
    await db.flush()

    stamp = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    patients = []
    for index in range(count):
        patient = Patient(
            account_id=account.id,
            full_name=f"{name} {index:02d}",
            sex="female",
            consent_given=True,
            created_at=stamp,
            updated_at=stamp,
        )
        db.add(patient)
        patients.append(patient)
    await db.flush()

    # SQLAlchemy's onupdate/server_default can overwrite what was set on the instance; assert
    # the tie actually landed, or the test would pass by not testing anything.
    stamps = {p.updated_at for p in patients}
    assert len(stamps) == 1, f"expected one shared updated_at, got {len(stamps)}"
    return account, patients


async def test_paging_a_panel_of_tied_timestamps_sees_every_chart_exactly_once(db):
    account, patients = await _panel_sharing_one_timestamp(db, _PANEL_SIZE)
    service = PatientService(db)

    seen: list = []
    for offset in range(0, _PANEL_SIZE, _PAGE):
        page, total = await service.list(account.id, limit=_PAGE, offset=offset)
        assert total == _PANEL_SIZE
        seen += [p.id for p in page]

    assert len(seen) == len(set(seen)), "a chart was served on more than one page"
    assert set(seen) == {p.id for p in patients}, "a chart was never served on any page"


async def test_the_same_request_returns_the_same_page(db):
    """A total order is only useful if it is the *same* total order on the next request."""
    account, _ = await _panel_sharing_one_timestamp(db, _PANEL_SIZE)
    service = PatientService(db)

    first, _ = await service.list(account.id, limit=_PAGE, offset=_PAGE)
    second, _ = await service.list(account.id, limit=_PAGE, offset=_PAGE)

    assert [p.id for p in first] == [p.id for p in second]


async def test_searching_a_panel_of_tied_timestamps_pages_stably(db):
    """The search branch paginates in Python over a decrypted list — same guarantee required.

    ``full_name`` is encrypted with non-deterministic ciphertext, so the filter cannot be a SQL
    predicate and the ordering that reaches the slice is the one this query asked for. Without
    a tiebreaker the slice is taken over a list whose order was never pinned.
    """
    account, patients = await _panel_sharing_one_timestamp(db, _PANEL_SIZE, name="Searchable")
    service = PatientService(db)

    seen: list = []
    for offset in range(0, _PANEL_SIZE, _PAGE):
        page, total = await service.list(
            account.id, search="searchable", limit=_PAGE, offset=offset
        )
        assert total == _PANEL_SIZE
        seen += [p.id for p in page]

    assert len(seen) == len(set(seen)), "a chart was served on more than one page of results"
    assert set(seen) == {p.id for p in patients}


async def test_the_ordering_is_newest_first_before_the_tiebreaker(db):
    """The tiebreaker must not have become the sort. Recency still leads."""
    account, _ = await _panel_sharing_one_timestamp(db, 3, name="Recency")
    newer = Patient(
        account_id=account.id,
        full_name="Recency Newest",
        sex="male",
        consent_given=True,
        created_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
    )
    db.add(newer)
    await db.flush()

    page, _ = await PatientService(db).list(account.id, limit=4, offset=0)
    assert page[0].id == newer.id


async def test_the_list_endpoint_pages_without_repeating_a_chart(auth_client):
    """The same guarantee through the HTTP surface a clinician's chart picker actually calls."""
    created = []
    for index in range(7):
        resp = await auth_client.post(
            "/api/v1/patients",
            json={
                "full_name": f"Paged Patient {index}",
                "sex": "other",
                "consent_given": True,
            },
        )
        assert resp.status_code == 201, resp.text
        created.append(resp.json()["id"])

    seen: list[str] = []
    for offset in (0, 3, 6):
        resp = await auth_client.get("/api/v1/patients", params={"limit": 3, "offset": offset})
        assert resp.status_code == 200, resp.text
        seen += [item["id"] for item in resp.json()["items"]]

    assert len(seen) == len(set(seen))
    assert set(created) <= set(seen)


@pytest.mark.parametrize("search", [None, "pinned"])
async def test_the_ordering_is_pinned_in_sql_not_only_in_python(db, engine, search):
    """The tiebreaker is in the statement the database runs, not applied after the rows return.

    A Python-side sort would pass every assertion above and still be wrong in production: the
    page is cut by ``LIMIT``, so *which* rows come back is decided by the database, and
    re-sorting a page that was already chosen from an unordered set fixes nothing. This reads
    the SQL actually emitted, so the guarantee cannot be quietly moved out of the query.

    The search branch is checked too. It slices in Python, but the list it slices is the one
    this statement returned, so the ordering still has to be in the SQL.

    This is the load-bearing assertion of the file, and deliberately structural rather than
    behavioural: SQLite happens to return tied rows in rowid order, so the tests above pass on
    the test backend either way and only fail on a production planner that is free not to. A
    property that only reproduces on the backend the suite does not run against has to be
    pinned as a property of the query.
    """
    account, _ = await _panel_sharing_one_timestamp(db, 2, name="Pinned")
    with counting_queries(engine) as captured:
        await PatientService(db).list(account.id, search=search, limit=1, offset=0)

    ordered = [
        statement
        for statement in captured["statements"]
        if "from patients" in statement.lower() and "order by" in statement.lower()
    ]
    assert ordered, "the patient list did not emit an ordered read"
    for statement in ordered:
        order_clause = statement.lower().split("order by", 1)[1]
        assert "updated_at desc" in order_clause
        assert "patients.id" in order_clause
