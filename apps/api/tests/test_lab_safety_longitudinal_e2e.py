"""The panic-value screen across a chart that grew one lab report at a time.

Every other test of this path gives a patient a single document. The screen's whole job,
though, is to answer "what is this patient's *latest* potassium" — a question that only has a
wrong answer once a second report exists. This module walks the real clinician journey for
that: upload a report, approve the extraction, read ``/labs/critical-flags``, then file the
follow-up report and read it again. Everything goes through HTTP and the deterministic text
parser (no LLM), so the labs are created exactly as production creates them: parse → clinician
approval → graph merge → screen.

**These reports are dated, and that is the point.** This file used to open with a note
explaining what it could not assert: nothing in the extraction layer emitted a ``sample_date``,
so every lab ingested through a document landed with ``sample_date = NULL``, "most recent per
marker" fell back to the ``created_at`` tiebreaker, and on SQLite ``created_at`` has one-second
granularity — two reports filed in the same test resolved by a random primary key. Asserting
which value was screened would have been a coin flip, so the tests were restricted to the
properties that held either way.

The gap was never only a test-environment one. Ingestion order is not draw order: a clinician
filing a report from a previous hospital after this week's bloods had the *old* value screened
as current, with no signal that anything was stale. The extraction layer now reads the report's
own collection date (see ``test_extraction_sample_date``), so the ordering these tests exercise
is the ordering production uses, and the draw-order assertions below are the ones that were
missing.
"""

from __future__ import annotations

import pytest

from tests.conftest import create_patient

pytestmark = pytest.mark.asyncio

PANIC_K = "7.4"
NORMAL_K = "4.0"


def _lab_report(*lines: str, collected: str | None = None) -> bytes:
    """A synthetic printed lab report. The %PDF- header satisfies magic-byte sniffing; the
    body is read by the deterministic text parser. ``collected`` is printed in the masthead
    the way a pathology lab prints it, DD/MM/YYYY."""
    masthead = f"Sample Collected on: {collected}\n" if collected else ""
    body = "".join(f"{line}\n" for line in lines)
    return b"%PDF-1.4\n" + masthead.encode() + b"LABS:\n" + body.encode()


def _potassium(value: str, *, spelled: str = "Potassium", collected: str | None = None) -> bytes:
    return _lab_report(f"{spelled}: {value} mmol/L (3.5-5.1)", collected=collected)


async def _file_report(client, patient_id: str, content: bytes, *, name: str = "labs.pdf") -> dict:
    """Upload a report and approve its extraction — one visit's worth of ingestion."""
    upload = await client.post(
        f"/api/v1/patients/{patient_id}/documents",
        files={"file": (name, content, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    doc = upload.json()
    approve = await client.post(
        f"/api/v1/patients/{patient_id}/documents/{doc['id']}/approve",
        json={"corrections": [], "rejected_entity_indexes": []},
    )
    assert approve.status_code == 200, approve.text
    return doc


async def _flags(client, patient_id: str) -> list[dict]:
    resp = await client.get(f"/api/v1/patients/{patient_id}/labs/critical-flags")
    assert resp.status_code == 200, resp.text
    return resp.json()["flags"]


async def _labs(client, patient_id: str) -> list[dict]:
    resp = await client.get(f"/api/v1/patients/{patient_id}/record")
    assert resp.status_code == 200, resp.text
    return resp.json()["lab_results"]


# --- Ordering by when the blood was drawn -------------------------------------------------


async def test_a_later_report_retires_the_flag_an_earlier_one_raised(auth_client):
    """The screen answers with the patient's current potassium, not every potassium ever."""
    pid = (await create_patient(auth_client))["id"]

    await _file_report(auth_client, pid, _potassium(PANIC_K, collected="01/03/2026"), name="1.pdf")
    assert [f["severity"] for f in await _flags(auth_client, pid)] == ["panic_high"]

    await _file_report(auth_client, pid, _potassium(NORMAL_K, collected="08/03/2026"), name="2.pdf")
    assert await _flags(auth_client, pid) == []


async def test_a_report_filed_late_does_not_screen_as_the_current_value(auth_client):
    """The failure this whole change closes, and the one an undated record could not avoid.

    Ingestion order is not draw order. A patient arrives with a folder from another hospital
    and the clinician files this week's bloods first, last week's second — or a courier report
    lands a day after the one that supersedes it. Ordering by when the row was *recorded* then
    screens the older, normal potassium as current and the panic value silently stops being
    flagged. Nothing in the UI would say so: the flag simply is not there.
    """
    pid = (await create_patient(auth_client))["id"]

    # Drawn most recently, filed first.
    await _file_report(auth_client, pid, _potassium(PANIC_K, collected="08/03/2026"), name="1.pdf")
    # Drawn a week earlier, filed second.
    await _file_report(auth_client, pid, _potassium(NORMAL_K, collected="01/03/2026"), name="2.pdf")

    assert [f["severity"] for f in await _flags(auth_client, pid)] == ["panic_high"]


async def test_the_draw_date_beats_ingestion_order_for_the_same_marker_spelled_differently(
    auth_client,
):
    """The two corrections have to hold at once.

    Successive reports rarely come from the same laboratory, so one chart accumulates
    "Potassium" and "POTASSIUM" for the same test — they are one marker, and the ranked read
    normalises them into one partition. Within that partition the newest *draw* must win. Here
    the panic value is the newest draw but was filed first and is spelled differently, so
    getting either half wrong loses the flag.
    """
    pid = (await create_patient(auth_client))["id"]

    await _file_report(
        auth_client,
        pid,
        _potassium(PANIC_K, spelled="POTASSIUM", collected="08/03/2026"),
        name="1.pdf",
    )
    await _file_report(
        auth_client,
        pid,
        _potassium(NORMAL_K, spelled="Potassium", collected="01/03/2026"),
        name="2.pdf",
    )

    assert [(f["marker_name"], f["severity"]) for f in await _flags(auth_client, pid)] == [
        ("POTASSIUM", "panic_high")
    ]


async def test_an_undated_report_does_not_outrank_a_dated_one(auth_client):
    """Not every scan is legible. A report whose date could not be read is ingested with
    ``sample_date = NULL`` and must sort *behind* every dated result — NULLS LAST — because the
    alternative is a report of unknown vintage silently claiming to be the current one."""
    pid = (await create_patient(auth_client))["id"]

    await _file_report(auth_client, pid, _potassium(PANIC_K, collected="08/03/2026"), name="1.pdf")
    await _file_report(auth_client, pid, _potassium(NORMAL_K), name="undated.pdf")

    assert [f["severity"] for f in await _flags(auth_client, pid)] == ["panic_high"]


# --- What reaches the record --------------------------------------------------------------


async def test_the_reports_collection_date_reaches_the_record(auth_client):
    """End to end: a date printed on a scan, through parse → approval → merge, into the column
    every longitudinal read sorts by."""
    pid = (await create_patient(auth_client))["id"]

    await _file_report(auth_client, pid, _potassium(PANIC_K, collected="08/03/2026"))

    (lab,) = await _labs(auth_client, pid)
    assert lab["sample_date"].startswith("2026-03-08")


async def test_the_masthead_does_not_become_a_lab_result(auth_client):
    """The date line has the shape of a lab line. Ingested as one it is a fabricated marker in
    a clinical record — and it was, until the parser learned to read it as a date."""
    pid = (await create_patient(auth_client))["id"]

    await _file_report(auth_client, pid, _potassium(PANIC_K, collected="08/03/2026"))

    assert [lab["marker_name"] for lab in await _labs(auth_client, pid)] == ["Potassium"]


# --- Properties that hold regardless of ordering ------------------------------------------


async def test_the_same_marker_under_three_spellings_never_raises_three_flags(auth_client):
    """Screened as three markers, one panic potassium becomes three findings for the clinician
    to reconcile against each other — and over-flagging is how a screen teaches clinicians to
    dismiss it."""
    pid = (await create_patient(auth_client))["id"]

    for i, spelled in enumerate(("Potassium", "POTASSIUM", "potassium")):
        await _file_report(auth_client, pid, _potassium(PANIC_K, spelled=spelled), name=f"{i}.pdf")

    assert len(await _flags(auth_client, pid)) == 1


async def test_a_marker_seen_only_once_survives_every_later_report(auth_client):
    """A follow-up covering a different panel must not retire a finding it never re-tested.
    "No newer result" is not "resolved" — the last known value stands."""
    pid = (await create_patient(auth_client))["id"]

    await _file_report(auth_client, pid, _potassium(PANIC_K, collected="01/03/2026"), name="1.pdf")
    await _file_report(
        auth_client,
        pid,
        _lab_report("Sodium: 138 mmol/L (135-145)", collected="08/03/2026"),
        name="2.pdf",
    )

    assert [f["marker_name"] for f in await _flags(auth_client, pid)] == ["Potassium"]


async def test_the_screen_gives_the_same_answer_on_every_read_of_an_unchanged_chart(auth_client):
    """The endpoint re-runs the screen on every call. A chart that screens clean on one request
    and panics on the next is worse than either answer alone."""
    pid = (await create_patient(auth_client))["id"]

    await _file_report(auth_client, pid, _potassium(PANIC_K, collected="01/03/2026"), name="1.pdf")
    await _file_report(auth_client, pid, _potassium(NORMAL_K, collected="08/03/2026"), name="2.pdf")

    answers = [await _flags(auth_client, pid) for _ in range(4)]

    assert answers[0] == answers[1] == answers[2] == answers[3] == []


async def test_one_patients_reports_never_resolve_anothers_flag(auth_client):
    """Two charts under one clinician, screened independently.

    The tenancy predicate lives inside the ranked read, so a bug there would surface as the
    untouched patient's flag quietly disappearing — a *missing* safety flag rather than leaked
    data, which is the harder failure to notice.
    """
    treated = (await create_patient(auth_client, full_name="Treated Patient"))["id"]
    untouched = (await create_patient(auth_client, full_name="Untouched Patient"))["id"]

    await _file_report(
        auth_client, untouched, _potassium(PANIC_K, collected="01/03/2026"), name="b-1.pdf"
    )
    await _file_report(
        auth_client, treated, _potassium(PANIC_K, collected="01/03/2026"), name="a-1.pdf"
    )
    await _file_report(
        auth_client, treated, _potassium(NORMAL_K, collected="08/03/2026"), name="a-2.pdf"
    )

    assert [f["severity"] for f in await _flags(auth_client, untouched)] == ["panic_high"]
    assert await _flags(auth_client, treated) == []
