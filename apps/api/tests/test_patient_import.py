"""Bulk patient import: the reader, the duplicate rule, and the two things it refuses to guess.

Most of what is asserted here is refusal. An importer is a route that turns a file nobody wrote
for this system into clinical records, and the failure modes that matter are not crashes — they
are the rows it accepts *wrongly*: a chart created without consent, a second chart for a patient
who already has one, or a date of birth read in the wrong order. Each of those looks like a
successful import and is discovered later, by a clinician, from a chart that is subtly not the
patient's.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.patient import Patient

pytestmark = pytest.mark.asyncio

_HEADER = "full_name,date_of_birth,sex,consent_given\n"


def _by_name(result, name: str) -> Patient:
    """The one chart with this name. ``full_name`` is encrypted with no blind index, so it can
    never be a SQL predicate — every lookup by name in this module happens after decryption."""
    matches = [p for p in result.scalars().all() if p.full_name == name]
    assert len(matches) == 1, f"expected one {name!r}, found {len(matches)}"
    return matches[0]


def _file(body: str, *, header: str = _HEADER, name: str = "patients.csv") -> dict:
    return {"file": (name, (header + body).encode(), "text/csv")}


async def _post(client, body: str, **kwargs):
    header = kwargs.pop("header", _HEADER)
    params = kwargs.pop("params", None)
    return await client.post(
        "/api/v1/patients/import", files=_file(body, header=header), params=params
    )


# --- The ordinary case ----------------------------------------------------------------------


async def test_a_plain_file_creates_a_chart_per_row(auth_client, db):
    resp = await _post(
        auth_client,
        "Ramesh Kumar,1968-05-10,male,yes\nPriya Nair,1981-11-02,female,yes\n",
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["total_rows"], body["created"], body["skipped"], body["invalid"]) == (2, 2, 0, 0)
    assert [row["status"] for row in body["rows"]] == ["created", "created"]
    names = (await db.execute(select(Patient.full_name))).scalars().all()
    assert sorted(names) == ["Priya Nair", "Ramesh Kumar"]


async def test_the_row_number_is_the_one_the_spreadsheet_shows(auth_client):
    """The header is row 1, so a report an operator can navigate from starts at 2. A zero-based
    index into the body would be correct and useless."""
    resp = await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")

    assert resp.json()["rows"][0]["row_number"] == 2


async def test_a_created_chart_is_indistinguishable_from_one_typed_in_by_hand(auth_client, db):
    """The import has no private path into the table: consent is stamped, the name is encrypted
    by the same column type, and the chart is readable through the ordinary route."""
    resp = await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")
    patient_id = resp.json()["rows"][0]["patient_id"]

    fetched = await auth_client.get(f"/api/v1/patients/{patient_id}")
    assert fetched.status_code == 200
    assert fetched.json()["consent_given"] is True
    assert fetched.json()["consent_given_at"] is not None


async def test_each_chart_gets_its_own_creation_entry_and_the_upload_gets_one(auth_client, db):
    """Both, and neither substitutes for the other: the per-chart rows are what a patient's own
    trail needs, and the import row is the only thing that says those two creations were one act
    by one clinician from one file."""
    await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\nPriya Nair,,female,yes\n")

    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert actions.count("patient_created") == 2
    assert actions.count("patients_imported") == 1


async def test_the_import_audit_entry_carries_counts_and_never_a_name(auth_client, db):
    """``audit_logs`` is unencrypted, immutable and never pruned. Every name in an imported file
    is a direct identifier, so none of them may land there."""
    await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")

    row = (
        await db.execute(select(AuditLog).where(AuditLog.action == "patients_imported"))
    ).scalar_one()
    assert row.payload["created"] == 1
    assert "Ramesh" not in str(row.payload)


# --- Consent --------------------------------------------------------------------------------


async def test_a_row_without_consent_is_not_created(auth_client, db):
    """The bulk path refuses exactly what the create form refuses. A file is the obvious way to
    load a thousand charts around a consent gate that only guards one form."""
    resp = await _post(auth_client, "Ramesh Kumar,1968-05-10,male,no\n")

    body = resp.json()
    assert body["created"] == 0 and body["invalid"] == 1
    assert body["rows"][0]["status"] == "invalid"
    assert "consent" in body["rows"][0]["message"]
    assert (await db.execute(select(Patient))).scalars().all() == []


@pytest.mark.parametrize("token", ["yes", "Yes", "TRUE", "1", "y", "granted"])
async def test_consent_is_read_in_the_spellings_a_person_types(auth_client, token):
    resp = await _post(auth_client, f"Ramesh Kumar,1968-05-10,male,{token}\n")
    assert resp.json()["created"] == 1, resp.text


async def test_an_unreadable_consent_value_is_refused_rather_than_assumed(auth_client):
    """Blank, or a word this reader does not know, is not "no" and is certainly not "yes" — it
    is a file the operator has to look at. The message says which cell."""
    resp = await _post(auth_client, "Ramesh Kumar,1968-05-10,male,maybe\n")

    row = resp.json()["rows"][0]
    assert row["status"] == "invalid"
    assert "consent_given" in row["message"] and "maybe" in row["message"]


async def test_a_file_with_no_consent_column_at_all_is_refused_whole(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients/import",
        files=_file("Ramesh Kumar,1968-05-10\n", header="full_name,date_of_birth\n"),
    )

    assert resp.status_code == 422
    assert "consent_given" in resp.json()["message"]


# --- Duplicates -----------------------------------------------------------------------------


async def test_a_name_and_date_already_on_the_account_is_never_created_again(auth_client, db):
    """The failure this exists for is clinical, not tidiness: two charts for one patient split
    their allergies across both, and every deterministic safety check then runs against half a
    record."""
    await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")

    resp = await _post(auth_client, "ramesh   KUMAR,1968-05-10,male,yes\n")

    body = resp.json()
    assert body["created"] == 0 and body["skipped"] == 1
    assert body["rows"][0]["status"] == "duplicate"
    assert body["rows"][0]["existing_patient_id"] is not None
    assert len((await db.execute(select(Patient))).scalars().all()) == 1


async def test_the_same_row_twice_in_one_file_is_caught_too(auth_client, db):
    """A list uploaded twice within one file is the same failure as one uploaded twice across
    two, and only the in-file check catches it."""
    resp = await _post(
        auth_client,
        "Ramesh Kumar,1968-05-10,male,yes\nRamesh Kumar,1968-05-10,male,yes\n",
    )

    body = resp.json()
    assert body["created"] == 1 and body["skipped"] == 1
    assert [row["status"] for row in body["rows"]] == ["created", "duplicate"]


async def test_two_people_sharing_a_name_are_two_charts(auth_client, db):
    """Ordinary, and the reason the rule is name *and* date of birth rather than name alone."""
    resp = await _post(
        auth_client,
        "Ramesh Kumar,1968-05-10,male,yes\nRamesh Kumar,1991-02-20,male,yes\n",
    )

    assert resp.json()["created"] == 2
    assert len((await db.execute(select(Patient))).scalars().all()) == 2


async def test_a_name_match_with_no_date_to_compare_is_reported_apart(auth_client, db):
    """Not the same fact as a confirmed duplicate, and the operator's next action differs — so
    it gets its own status and its own message. Still not created: two charts for one patient is
    worse than one chart an operator has to add by hand."""
    await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")

    resp = await _post(auth_client, "Ramesh Kumar,,male,yes\n")

    row = resp.json()["rows"][0]
    assert row["status"] == "possible_duplicate"
    assert "date of birth" in row["message"]
    assert len((await db.execute(select(Patient))).scalars().all()) == 1


async def test_a_withdrawn_chart_does_not_block_a_re_import(auth_client, db):
    """A soft-deleted chart is out of clinical use, so the name is free again. Matching against
    it would leave a practice unable to re-create a chart it had deliberately withdrawn."""
    first = await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")
    await auth_client.delete(f"/api/v1/patients/{first.json()['rows'][0]['patient_id']}")

    resp = await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")

    assert resp.json()["created"] == 1, resp.text


async def test_another_account_s_charts_are_not_consulted(auth_client, second_auth_client, db):
    """The duplicate check is account-scoped like every other read here. A name being taken in
    another practice must neither block this import nor be observable from it."""
    await _post(second_auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")

    resp = await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")

    assert resp.json()["created"] == 1, resp.text


# --- Dates ----------------------------------------------------------------------------------


async def test_iso_dates_are_read_without_needing_any_inference(auth_client):
    resp = await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")

    assert resp.json()["date_convention"] is None
    assert resp.json()["created"] == 1


async def test_one_unambiguous_row_settles_the_order_for_the_whole_file(auth_client, db):
    """``25/12/1970`` can only be day-first, and one file is one export from one locale — so the
    ambiguous rows beside it can be read with confidence rather than guessed at."""
    resp = await _post(
        auth_client,
        "Anita Rao,25/12/1970,female,yes\nRamesh Kumar,03/04/1990,male,yes\n",
    )

    assert resp.json()["date_convention"] == "day_first"
    assert resp.json()["created"] == 2
    # `full_name` is encrypted at rest with no blind index, so it cannot be a SQL predicate —
    # the value only exists after SQLAlchemy has decrypted it in Python.
    ramesh = _by_name(await db.execute(select(Patient)), "Ramesh Kumar")
    assert ramesh.date_of_birth.isoformat() == "1990-04-03"


async def test_a_month_first_file_is_read_month_first(auth_client, db):
    resp = await _post(
        auth_client,
        "Anita Rao,12/25/1970,female,yes\nRamesh Kumar,03/04/1990,male,yes\n",
    )

    assert resp.json()["date_convention"] == "month_first"
    # `full_name` is encrypted at rest with no blind index, so it cannot be a SQL predicate —
    # the value only exists after SQLAlchemy has decrypted it in Python.
    ramesh = _by_name(await db.execute(select(Patient)), "Ramesh Kumar")
    assert ramesh.date_of_birth.isoformat() == "1990-03-04"


async def test_an_ambiguous_date_with_nothing_to_settle_it_is_refused(auth_client, db):
    """The whole point. Guessing day-first because this product ships in India would be right
    most of the time and silently wrong the rest — and a wrong date of birth decides whether a
    dose is judged against a paediatric mg/kg ceiling or an adult one."""
    resp = await _post(auth_client, "Ramesh Kumar,03/04/1990,male,yes\n")

    row = resp.json()["rows"][0]
    assert row["status"] == "invalid"
    assert "YYYY-MM-DD" in row["message"]
    assert (await db.execute(select(Patient))).scalars().all() == []


async def test_a_file_proving_both_orders_is_refused_whole(auth_client):
    """Not a locale — a file assembled from two sources, and no single reading of it is right.
    Refused as a file because reporting it per row would produce four hundred identical
    messages instead of one useful one."""
    resp = await _post(
        auth_client,
        "Anita Rao,25/12/1970,female,yes\nRamesh Kumar,12/25/1970,male,yes\n",
    )

    assert resp.status_code == 422
    assert "mixes date orders" in resp.json()["message"]


async def test_a_blank_date_of_birth_is_simply_absent(auth_client, db):
    resp = await _post(auth_client, "Ramesh Kumar,,male,yes\n")

    assert resp.json()["created"] == 1
    assert (await db.execute(select(Patient))).scalar_one().date_of_birth is None


async def test_a_date_of_birth_the_create_form_would_reject_is_rejected_here(auth_client):
    """The same validators, not a second implementation of them: a future date of birth is
    refused on both paths or the bulk path is a way around the rules."""
    resp = await _post(auth_client, "Ramesh Kumar,2999-01-01,male,yes\n")

    row = resp.json()["rows"][0]
    assert row["status"] == "invalid"
    assert "date_of_birth" in row["message"]


# --- File shapes ----------------------------------------------------------------------------


async def test_a_utf8_bom_and_semicolons_are_read(auth_client):
    """What Excel writes as "CSV UTF-8" in a locale with a comma decimal separator. Refusing it
    would make the importer usable only by files written for it."""
    raw = "﻿full_name;date_of_birth;sex;consent_given\nRamesh Kumar;1968-05-10;male;yes\n"
    resp = await auth_client.post(
        "/api/v1/patients/import",
        files={"file": ("patients.csv", raw.encode("utf-8"), "text/csv")},
    )

    body = resp.json()
    assert body["created"] == 1, resp.text
    assert body["delimiter"] == ";"
    assert body["encoding"] == "utf-8-sig"


async def test_a_utf16_export_is_read_and_names_survive_it(auth_client, db):
    """Excel's "Unicode text" is UTF-16 TSV. Decoding it as CP1252 succeeds and yields NUL
    between every character, so "it decoded" is not evidence the encoding was right."""
    raw = "full_name\tdate_of_birth\tsex\tconsent_given\nRamesh Kumar\t1968-05-10\tmale\tyes\n"
    resp = await auth_client.post(
        "/api/v1/patients/import",
        files={"file": ("patients.csv", raw.encode("utf-16"), "text/csv")},
    )

    assert resp.json()["created"] == 1, resp.text
    assert resp.json()["encoding"] == "utf-16"
    assert (await db.execute(select(Patient.full_name))).scalar_one() == "Ramesh Kumar"


async def test_headers_are_matched_however_they_are_capitalised_or_spaced(auth_client):
    resp = await auth_client.post(
        "/api/v1/patients/import",
        files=_file(
            "Ramesh Kumar,1968-05-10,yes\n", header="Full Name,Date-Of-Birth,CONSENT_GIVEN\n"
        ),
    )

    assert resp.json()["created"] == 1, resp.text


async def test_a_practice_s_own_extra_columns_are_reported_not_refused(auth_client):
    """A misspelt ``date_of_birth`` shows up here as the reason every date came out empty, which
    is the point of reporting them at all."""
    resp = await auth_client.post(
        "/api/v1/patients/import",
        files=_file(
            "Ramesh Kumar,1968-05-10,yes,MRN-4471\n",
            header="full_name,date_of_birth,consent_given,local_mrn\n",
        ),
    )

    assert resp.json()["created"] == 1, resp.text
    assert resp.json()["unknown_columns"] == ["local_mrn"]


async def test_a_column_named_twice_is_refused(auth_client):
    """Two values for one field, and which one the operator meant is not knowable from here —
    reading the last silently would be a coin toss over a date of birth."""
    resp = await auth_client.post(
        "/api/v1/patients/import",
        files=_file(
            "Ramesh Kumar,1968-05-10,1970-01-01,yes\n",
            header="full_name,date_of_birth,date_of_birth,consent_given\n",
        ),
    )

    assert resp.status_code == 422
    assert "more than once" in resp.json()["message"]


async def test_blank_lines_between_blocks_are_not_rows(auth_client):
    resp = await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n\n\n")

    assert resp.json()["total_rows"] == 1


async def test_an_empty_file_and_a_headers_only_file_say_different_things(auth_client):
    empty = await auth_client.post(
        "/api/v1/patients/import", files={"file": ("p.csv", b"", "text/csv")}
    )
    headers_only = await auth_client.post(
        "/api/v1/patients/import", files={"file": ("p.csv", _HEADER.encode(), "text/csv")}
    )

    assert empty.status_code == 422 and "empty" in empty.json()["message"]
    assert headers_only.status_code == 422
    assert "no patients" in headers_only.json()["message"]


async def test_a_file_over_the_row_ceiling_is_refused_before_anything_is_written(
    auth_client, db, monkeypatch
):
    monkeypatch.setattr(settings, "patient_import_max_rows", 3)
    body = "".join(f"Patient {i},1968-05-10,male,yes\n" for i in range(5))

    resp = await _post(auth_client, body)

    assert resp.status_code == 422
    assert "up to 3" in resp.json()["message"]
    assert (await db.execute(select(Patient))).scalars().all() == []


# --- Partial success and dry run --------------------------------------------------------------


async def test_good_rows_land_even_when_others_are_refused(auth_client, db):
    """An operator fixing three bad lines must not have to re-upload the four hundred good ones
    — and re-uploading them would then be four hundred duplicates."""
    resp = await _post(
        auth_client,
        "Ramesh Kumar,1968-05-10,male,yes\n"
        ",1970-01-01,male,yes\n"
        "Priya Nair,1981-11-02,female,no\n"
        "Anita Rao,1975-06-06,female,yes\n",
    )

    body = resp.json()
    assert (body["created"], body["invalid"]) == (2, 2)
    assert [row["status"] for row in body["rows"]] == [
        "created",
        "invalid",
        "invalid",
        "created",
    ]
    assert len((await db.execute(select(Patient))).scalars().all()) == 2


async def test_a_dry_run_reports_the_same_decisions_and_writes_nothing(auth_client, db):
    resp = await _post(
        auth_client,
        "Ramesh Kumar,1968-05-10,male,yes\nPriya Nair,1981-11-02,female,no\n",
        params={"dry_run": "true"},
    )

    body = resp.json()
    assert body["dry_run"] is True
    assert body["created"] == 0
    assert [row["status"] for row in body["rows"]] == ["not_created", "invalid"]
    assert (await db.execute(select(Patient))).scalars().all() == []


async def test_a_dry_run_still_leaves_a_trail(auth_client, db):
    """A disclosure-free event that still answers "who tried to load what into this account, and
    when" — which is the question a DPDP review asks of a file that was never committed."""
    await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n", params={"dry_run": "true"})

    row = (
        await db.execute(select(AuditLog).where(AuditLog.action == "patients_imported"))
    ).scalar_one()
    assert row.payload["dry_run"] is True
    assert row.payload["created"] == 0


async def test_the_counts_always_partition_the_rows(auth_client):
    """``created + skipped + invalid == total_rows``, which is what makes a report readable
    without adding up the row array."""
    await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")
    resp = await _post(
        auth_client,
        "Ramesh Kumar,1968-05-10,male,yes\nPriya Nair,1981-11-02,female,no\n"
        "Anita Rao,1975-06-06,female,yes\n",
    )

    body = resp.json()
    assert body["created"] + body["skipped"] + body["invalid"] == body["total_rows"]
    assert len(body["rows"]) == body["total_rows"]


# --- The gate and the tenancy boundary --------------------------------------------------------


async def test_the_import_is_behind_the_step_up_password_prompt(auth_client, db):
    from datetime import UTC, datetime, timedelta

    from app.models.user import Session as AuthSession

    for row in (await db.execute(select(AuthSession))).scalars().all():
        row.last_authenticated_at = datetime.now(UTC) - timedelta(days=1)
    await db.commit()

    resp = await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")

    assert resp.status_code == 403
    assert resp.json()["code"] == "reauthentication_required"


async def test_charts_land_on_the_importing_account_only(auth_client, second_auth_client, db):
    await _post(auth_client, "Ramesh Kumar,1968-05-10,male,yes\n")

    listed = await second_auth_client.get("/api/v1/patients")
    assert listed.json()["pagination"]["total"] == 0
