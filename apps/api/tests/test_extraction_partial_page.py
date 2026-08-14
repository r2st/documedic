"""A page the parser only partly read must not present as a page it fully read.

The extraction review queue shows what was extracted. It has no way to show what was on the page
and was not — and those are the same screen to the clinician reviewing it.

A prescription lists four drugs. The scanner mangles one line's dose into letters
("Warfarin 5rng OD"), so ``_MED_RE`` matches nothing and the line yields no entity. The other
three parse cleanly, every field bands "high", the document reports ``completed``, and the
clinician confirms three drugs. Warfarin — the most interaction-heavy drug in the corpus — is
simply not in the chart, and nothing anywhere recorded that a fourth line existed. The safety
engine cannot catch this: it evaluates what the chart holds, and the chart holds three consistent
medications.

What is pinned here: the loss is counted, the count reaches the clinician through the extraction
response, and a document that lost a line is held for review rather than reported complete. The
count is deliberately not the line's text — ``extraction_metadata`` is not an encrypted column and
an unreadable line is still the patient's prescribing, the same reason
``_mark_extraction_failed`` records an exception's type and not its message.
"""

from __future__ import annotations

import pytest

from app.services.extraction.pipeline import ExtractionPipeline
from app.services.extraction.text_parser import parse_document, parse_text
from tests.conftest import create_patient

# Four drugs. The warfarin line has "5rng" where the page printed "5mg": the digit run is not
# followed by a word boundary, so the medication grammar fails the line outright.
FOUR_DRUGS_ONE_MANGLED = (
    "PRESCRIPTION\nMEDICATIONS\n"
    "Glycomet 500mg BD\n"
    "Ecosprin 75mg OD\n"
    "Warfarin 5rng OD\n"
    "Telma 40mg OD\n"
)


# The upload route sniffs magic bytes, so a text fixture has to arrive wearing a PDF header; the
# PDF reader then finds no pages and the pipeline falls through to the plain-text path.
def _as_scan(text: str) -> bytes:
    return b"%PDF-1.4\n" + text.encode()


# --- the parser counts what it could not read -------------------------------------------------


def test_a_mangled_prescription_line_is_counted_rather_than_only_dropped():
    parsed = parse_document(FOUR_DRUGS_ONE_MANGLED)

    assert len(parsed.entities) == 3, "the mangled line is still unparseable, as expected"
    assert parsed.unreadable == 1, (
        "a line under MEDICATIONS produced no entity and was not counted; the page lists four "
        "drugs and the extraction shows three, with nothing to say so"
    )


def test_a_cleanly_read_page_counts_no_losses():
    parsed = parse_document("MEDICATIONS\nGlycomet 500mg BD\nEcosprin 75mg OD\n")

    assert len(parsed.entities) == 2
    assert parsed.unreadable == 0


@pytest.mark.parametrize(
    "text",
    [
        # Page furniture outside any section: a letterhead, an address, a footer.
        "Dr. A. Sharma, MBBS MD\n12 Nehru Place, New Delhi\n",
        # And the harder case: ``section`` persists to the next header, so a prescriber's name and
        # signature at the foot of the page are still "under MEDICATIONS". These carry no digit,
        # so they were never candidates for the medication grammar and are not losses.
        "MEDICATIONS\nGlycomet 500mg BD\nDr. A. Sharma, MBBS MD\nSignature\n",
        # Page numbering is consumed by the masthead rules before the grammars ever see it.
        "MEDICATIONS\nGlycomet 500mg BD\nPage 1 of 2\n",
    ],
)
def test_page_furniture_is_not_counted_as_a_loss(text):
    """A review flag that is always on is one clinicians learn to click past."""
    assert parse_document(text).unreadable == 0


def test_a_line_the_masthead_rules_consume_is_not_a_loss():
    """Administrative lines are deliberately dropped; they are not failures to read."""
    parsed = parse_document(
        "LAB REPORT\nSample Collected on: 12/03/2026\nLab No: 4471\n"
        "Potassium: 5.4 mmol/L (3.5-5.1)\n"
    )

    assert len(parsed.entities) == 1
    assert parsed.unreadable == 0


def test_parse_text_still_returns_the_entities_alone():
    """The narrow entry point is unchanged, so its 30-odd existing call sites are too."""
    assert parse_text(FOUR_DRUGS_ONE_MANGLED) == parse_document(FOUR_DRUGS_ONE_MANGLED).entities


# --- the count survives the pipeline ----------------------------------------------------------


def test_the_pipeline_carries_the_count_out_of_the_parser():
    result = ExtractionPipeline().run(b"", "text/plain", raw_text=FOUR_DRUGS_ONE_MANGLED)

    assert len(result.entities) == 3
    assert result.unreadable_lines == 1


def test_the_vision_path_reports_no_count_rather_than_a_wrong_one():
    """Only the deterministic parser can know, and defaulting to zero must not read as certainty.

    The vision path is handed a page and returns entities; it has no line-by-line account of the
    page to compare them against. Zero here means "not measured", which is why the field is a
    count on the extraction result and not a "page fully read" flag — a flag would be a claim.
    """
    from app.services.extraction.pipeline import ExtractionResultInternal

    assert ExtractionResultInternal([], None, False, "some-vision-model").unreadable_lines == 0


# --- and reaches the clinician ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_document_that_lost_a_line_is_held_for_review_not_reported_complete(auth_client):
    """End to end through the upload route, which is what the clinician actually drives.

    ``completed`` on this document reads as "this page has been fully understood". It had not
    been, and the only thing standing between that and a chart missing a drug was whether the
    clinician thought to compare the review queue against the original scan.
    """
    patient = await create_patient(auth_client)

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("rx.pdf", _as_scan(FOUR_DRUGS_ONE_MANGLED), "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    document = resp.json()
    assert document["extraction_status"] == "needs_confirmation"

    extraction = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{document['id']}/extraction"
    )
    assert extraction.status_code == 200, extraction.text
    body = extraction.json()

    assert len(body["entities"]) == 3
    assert body["unreadable_line_count"] == 1, (
        "the extraction response gave the clinician no way to know a line was lost"
    )


@pytest.mark.asyncio
async def test_a_fully_read_page_still_reports_its_own_confirmation_state(auth_client):
    """The guard must not hold every document: a clean page reports zero losses."""
    patient = await create_patient(auth_client)

    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={
            "file": (
                "rx.pdf",
                _as_scan("PRESCRIPTION\nMEDICATIONS\nGlycomet 500mg BD\nTelma 40mg OD\n"),
                "application/pdf",
            )
        },
    )
    assert resp.status_code == 201, resp.text

    extraction = await auth_client.get(
        f"/api/v1/patients/{patient['id']}/documents/{resp.json()['id']}/extraction"
    )
    assert extraction.json()["unreadable_line_count"] == 0


@pytest.mark.asyncio
async def test_an_extraction_recorded_before_the_count_existed_reads_as_zero(auth_client, db):
    """Documents already in the database have no ``unreadable_line_count`` in their metadata.

    Defaulting to zero understates rather than reassures — those extractions were not known to be
    complete before this change either, and the alternative (a missing key raising, or the whole
    response 500ing) would make old documents unreadable.
    """
    import uuid

    from sqlalchemy import select

    from app.models.document import Document
    from app.services.document_service import DocumentService

    patient = await create_patient(auth_client)
    resp = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/documents",
        files={"file": ("rx.pdf", _as_scan("MEDICATIONS\nGlycomet 500mg BD\n"), "application/pdf")},
    )
    document_id = uuid.UUID(resp.json()["id"])

    stored = (await db.execute(select(Document).where(Document.id == document_id))).scalar_one()
    meta = dict(stored.extraction_metadata or {})
    meta.pop("unreadable_line_count", None)
    stored.extraction_metadata = meta
    await db.flush()

    assert DocumentService(db).build_extraction_result(stored).unreadable_line_count == 0
