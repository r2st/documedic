"""The panic-value screen across a chart that grew one lab report at a time.

Every existing test of this path gives a patient a single document. The screen's whole job,
though, is to answer "what is this patient's *latest* potassium" — a question that only has a
wrong answer once a second report exists. This module walks the real clinician journey for
that: upload a report, approve the extraction, read ``/labs/critical-flags``, then file the
follow-up report and read it again. Everything goes through HTTP and the deterministic text
parser (no LLM), so the labs are created exactly as production creates them: parse → clinician
approval → graph merge → screen.

**What this file deliberately does not assert, and why.** Nothing in the extraction layer emits
a ``sample_date`` — neither the text parser nor the vision prompt — so every lab ingested
through a document lands with ``sample_date = NULL``. "Most recent per marker" therefore falls
back to the ``created_at`` tiebreaker, and on SQLite ``created_at`` is ``CURRENT_TIMESTAMP``,
which has one-second granularity: two reports filed in the same test resolve by the final
tiebreaker, a random primary key. So a test asserting "the follow-up report's value is the one
screened" is a coin flip here, and would be committed flaky.

That gap is real beyond the test environment. Ingestion order is not draw order, so a
clinician who files an old report after a new one has the *old* value screened as current —
and a printed report's own "Sample Date:" line is currently parsed as a lab result named
"Date" with a value of 12. Both are worth fixing in the extraction pipeline; until they are,
the tests below are restricted to the properties that hold whichever of two same-second rows
wins. The dated orderings themselves are pinned directly, against rows with real sample dates,
in ``test_lab_safety_latest_per_marker`` and ``test_lab_safety_bounded_read``.
"""

from __future__ import annotations

import pytest

from tests.conftest import create_patient

pytestmark = pytest.mark.asyncio

PANIC_K = "7.4"
NORMAL_K = "4.0"


def _lab_report(*lines: str) -> bytes:
    """A synthetic printed lab report. The %PDF- header satisfies magic-byte sniffing; the
    body is read by the deterministic text parser."""
    body = "".join(f"{line}\n" for line in lines)
    return b"%PDF-1.4\nLABS:\n" + body.encode()


def _potassium(value: str, *, spelled: str = "Potassium") -> bytes:
    return _lab_report(f"{spelled}: {value} mmol/L (3.5-5.1)")


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


async def test_the_same_marker_under_three_spellings_never_raises_three_flags(auth_client):
    """Successive reports rarely come from the same laboratory.

    Each lab prints its own layout and the parser records the marker as printed, so one
    patient's chart accumulates "Potassium", "POTASSIUM" and "potassium" for the same test the
    moment they change laboratory. Screened as three markers, one panic potassium becomes three
    findings for the clinician to reconcile against each other — and over-flagging is how a
    screen teaches clinicians to dismiss it.

    All three values are critical, so this holds whichever row the tiebreaker picks.
    """
    patient = await create_patient(auth_client)
    pid = patient["id"]

    for spelled in ("Potassium", "POTASSIUM", "potassium"):
        await _file_report(auth_client, pid, _potassium(PANIC_K, spelled=spelled))

    assert len(await _flags(auth_client, pid)) == 1


async def test_a_marker_seen_only_once_survives_every_later_report(auth_client):
    """A follow-up covering a different panel must not retire a finding it never re-tested.

    "No newer result" is not "resolved" — the last known value stands. Only one potassium row
    exists here, so there is no tie to break: if the sodium report retired the potassium flag,
    it did so by screening the wrong marker.
    """
    patient = await create_patient(auth_client)
    pid = patient["id"]

    await _file_report(auth_client, pid, _potassium(PANIC_K), name="visit-1.pdf")
    await _file_report(
        auth_client, pid, _lab_report("Sodium: 138 mmol/L (135-145)"), name="visit-2.pdf"
    )

    assert [f["marker_name"] for f in await _flags(auth_client, pid)] == ["Potassium"]


async def test_the_screen_gives_the_same_answer_on_every_read_of_an_unchanged_chart(auth_client):
    """No caching, and no dependence on what the planner happened to return first.

    The endpoint re-runs the screen on every call. With two results for one marker in the
    chart, "which one is current" must resolve the same way every time — a chart that screens
    clean on one request and panics on the next is worse than either answer alone. The
    assertion is deliberately on the *agreement* between reads rather than on which value won:
    with both rows undated and recorded in the same second, which one wins is not a property
    this test can pin, but that it wins consistently is.
    """
    patient = await create_patient(auth_client)
    pid = patient["id"]

    await _file_report(auth_client, pid, _potassium(PANIC_K), name="visit-1.pdf")
    await _file_report(auth_client, pid, _potassium(NORMAL_K), name="visit-2.pdf")

    answers = [await _flags(auth_client, pid) for _ in range(4)]

    assert answers[0] == answers[1] == answers[2] == answers[3]


async def test_one_patients_reports_never_resolve_anothers_flag(auth_client):
    """Two charts under one clinician, screened independently.

    Both patients have a panic potassium; only one has follow-up reports filed. The tenancy
    predicate lives inside the ranked read, so a bug there would surface as the untouched
    patient's flag quietly disappearing — a *missing* safety flag rather than leaked data,
    which is the harder failure to notice. The assertion is on the untouched chart, whose
    single potassium row leaves nothing to tie-break.
    """
    treated = (await create_patient(auth_client, full_name="Treated Patient"))["id"]
    untouched = (await create_patient(auth_client, full_name="Untouched Patient"))["id"]

    await _file_report(auth_client, untouched, _potassium(PANIC_K), name="b-1.pdf")
    await _file_report(auth_client, treated, _potassium(PANIC_K), name="a-1.pdf")
    await _file_report(auth_client, treated, _potassium(NORMAL_K), name="a-2.pdf")
    await _file_report(auth_client, treated, _potassium(NORMAL_K), name="a-3.pdf")

    assert [f["severity"] for f in await _flags(auth_client, untouched)] == ["panic_high"]
