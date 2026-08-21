"""Clinical-notes search: the ranker, the scoping, and the amended-note trap.

The one that matters clinically is ``test_an_amended_note_is_returned_marked_as_superseded``.
A search over a medical record turns up the best textual match, and the best textual match is
quite often a sentence somebody has since corrected.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.note_search import (
    SearchDocument,
    parse_query,
    rank,
    tokenize,
)
from app.models.audit_log import AuditLog
from tests.conftest import create_patient


def _doc(ident: str, complaint: str | None, notes: str | None) -> SearchDocument:
    return SearchDocument(document_id=ident, presenting_complaint=complaint, clinician_notes=notes)


def _order(query: str, documents: list[SearchDocument]) -> list[str]:
    return [hit.document_id for hit in rank(parse_query(query), documents)]


# --- Parsing ------------------------------------------------------------------------------


async def test_case_and_punctuation_are_not_part_of_a_term():
    assert tokenize("Chest-pain, WORSE on exertion.") == [
        "chest",
        "pain",
        "worse",
        "on",
        "exertion",
    ]


async def test_the_plural_fold_is_applied_to_both_sides_so_it_can_only_over_match():
    """Symmetry is the whole correctness argument. "Headaches" must find "headache"."""
    assert tokenize("headaches")[0] == tokenize("headache")[0]
    assert _order("headaches", [_doc("a", "Recurrent headache", None)]) == ["a"]


@pytest.mark.parametrize("word", ["stress", "sinus", "analysis"])
async def test_words_ending_in_ss_us_and_is_keep_their_ending(word):
    """Stripping there produces a different word rather than a stem."""
    assert tokenize(word) == [word]


async def test_stop_words_are_dropped_but_negations_are_not():
    """ "No chest pain" and "chest pain" are opposite findings; a stop list that eats the
    negation makes the search answer the wrong question."""
    parsed = parse_query("the patient has no chest pain and denies nausea")
    assert "the" not in parsed.terms and "has" not in parsed.terms
    assert "no" in parsed.terms
    # The fold is applied to both sides, so the *folded* form is what a term is. Asserting the
    # spelling here would be asserting against a stem this module deliberately does not promise.
    assert tokenize("denies")[0] in parsed.terms


async def test_a_short_token_is_dropped_unless_it_carries_a_digit():
    """ "bd", "mg" and "od" match every note. "b12" and the "2" in "type 2" do real work."""
    parsed = parse_query("mg b12 type 2 diabetes")
    assert "mg" not in parsed.terms
    assert "b12" in parsed.terms
    assert "2" in parsed.terms


async def test_a_query_that_reduces_to_nothing_is_empty_not_a_match_for_everything():
    assert parse_query("the and of it").is_empty
    assert rank(parse_query("the and of"), [_doc("a", "anything", "at all")]) == []


async def test_an_unclosed_quote_is_treated_as_text_not_as_a_syntax_error():
    """A clinician typing a query is not writing an expression."""
    parsed = parse_query('chest "pain')
    assert "chest" in parsed.terms and "pain" in parsed.terms
    assert parsed.phrases == ()


async def test_a_quoted_phrase_becomes_a_phrase_and_leaves_the_loose_terms_alone():
    parsed = parse_query('"chest pain" exertion')
    assert parsed.phrases == (("chest", "pain"),)
    assert parsed.terms == ("exertion",)
    assert set(parsed.lookup_terms) == {"exertion", "chest", "pain"}


# --- Ranking ------------------------------------------------------------------------------


async def test_a_match_in_the_presenting_complaint_outranks_one_in_the_body():
    """The visit that was *about* chest pain, ahead of the review that mentions it."""
    assert _order(
        "chest pain",
        [
            _doc("body", "Diabetes review", "Stable. Denies chest pain."),
            _doc("complaint", "Chest pain on exertion", "Reviewed."),
        ],
    ) == ["complaint", "body"]


async def test_term_frequency_saturates_so_a_long_note_does_not_win_by_being_long():
    """A discharge summary saying "diabetes" eleven times is not eleven times more about
    diabetes than a clinic note saying it once. A linear score ranks by verbosity, which in a
    medical record means it ranks by how sick the patient was."""
    padding = " ".join(f"unrelated{i}" for i in range(400))
    order = _order(
        "diabetes",
        [
            _doc("long", "Admission", "diabetes " * 11 + padding),
            _doc("short", "Diabetes review", "Stable."),
        ],
    )
    assert order == ["short", "long"]


async def test_a_single_term_search_over_a_prefiltered_set_still_ranks_the_right_way_up():
    """The textbook Okapi IDF goes negative when every candidate contains the term, which is the
    normal case for a *re-ranker* over a prefiltered set — and a negative IDF puts the least
    relevant note first. Lucene's always-positive form is what stops that."""
    order = _order(
        "diabetes",
        [
            _doc("passing", "Ankle sprain", "Background: diabetes. " + "filler " * 200),
            _doc("about", "Diabetes review", "Diabetes control discussed."),
        ],
    )
    assert order[0] == "about"


async def test_a_contiguous_phrase_outranks_the_same_words_scattered_across_a_page():
    order = _order(
        '"chest pain"',
        [
            _doc("scattered", "Review", "Chest examined. " + "filler " * 40 + " Reports pain."),
            _doc("contiguous", "Review", "Reports chest pain."),
        ],
    )
    assert order[0] == "contiguous"


async def test_a_phrase_near_miss_is_still_returned_and_flagged():
    """A clinician who quoted a phrase usually still wants the near misses."""
    hits = rank(parse_query('"chest pain"'), [_doc("a", "Review", "Chest clear. Leg pain.")])
    assert [h.document_id for h in hits] == ["a"]
    assert hits[0].phrases_matched is False


async def test_a_document_matching_nothing_is_dropped_rather_than_scored_zero():
    assert rank(parse_query("diabetes"), [_doc("a", "Ankle sprain", "Ice and rest.")]) == []


async def test_ties_keep_the_callers_order_so_the_recent_visit_wins():
    """The service hands documents newest-first and Python's sort is stable."""
    identical = "Diabetes review"
    assert _order("diabetes", [_doc("new", identical, None), _doc("old", identical, None)]) == [
        "new",
        "old",
    ]


# --- Snippets -----------------------------------------------------------------------------


async def test_a_snippet_names_its_field_and_carries_offsets_not_markup():
    """This text is what a clinician typed; building markup around it server-side would put an
    injection surface in the one response whose job is to hand back exactly that."""
    hits = rank(parse_query("warfarin"), [_doc("a", "Review", "Continues warfarin 3mg.")])
    snippet = hits[0].snippets[0]
    assert snippet.field == "clinician_notes"
    assert "<" not in snippet.text and "mark" not in snippet.text
    start, end = snippet.matches[0]
    assert snippet.text[start:end].lower() == "warfarin"


async def test_the_snippet_offsets_point_at_the_original_text_not_the_folded_form():
    """Folding changes token lengths; offsets taken from folded text drift along the line."""
    notes = "Mr. Kumar's REPEATED headaches, worse in the mornings."
    hits = rank(parse_query("headaches"), [_doc("a", None, notes)])
    snippet = hits[0].snippets[0]
    start, end = snippet.matches[0]
    assert snippet.text[start:end] == "headaches"


async def test_the_snippet_window_is_chosen_where_the_matches_are_densest():
    """The first mention of a word in a long note is often the problem list at the top; the
    sentence that discusses it is three paragraphs down."""
    notes = "Problem list: asthma. " + ("filler " * 60) + "Asthma poorly controlled; asthma plan."
    hits = rank(parse_query("asthma"), [_doc("a", None, notes)])
    snippet = hits[0].snippets[0]
    assert len(snippet.matches) >= 2
    assert "controlled" in snippet.text


async def test_a_truncated_window_says_which_ends_it_cut():
    notes = ("filler " * 60) + "warfarin" + (" filler" * 60)
    hits = rank(parse_query("warfarin"), [_doc("a", None, notes)])
    snippet = hits[0].snippets[0]
    assert snippet.truncated_start is True and snippet.truncated_end is True


async def test_a_field_that_is_absent_produces_no_snippet_rather_than_an_empty_one():
    hits = rank(parse_query("warfarin"), [_doc("a", None, "On warfarin.")])
    assert [s.field for s in hits[0].snippets] == ["clinician_notes"]


# --- The route ----------------------------------------------------------------------------


async def _visit(client, patient_id: str, complaint: str, notes: str, day: str = "2026-01-05"):
    resp = await client.post(
        f"/api/v1/patients/{patient_id}/encounters",
        json={
            "encounter_date": day,
            "encounter_type": "outpatient",
            "presenting_complaint": complaint,
            "clinician_notes": notes,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _search(client, query: str, **extra):
    resp = await client.post("/api/v1/notes/search", json={"query": query, **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_the_route_finds_and_ranks_notes(auth_client):
    patient = await create_patient(auth_client)
    await _visit(auth_client, patient["id"], "Chest pain on exertion", "ECG normal.")
    await _visit(auth_client, patient["id"], "Diabetes review", "Denies chest pain.")
    body = await _search(auth_client, "chest pain")
    assert len(body["results"]) == 2
    assert body["results"][0]["snippets"][0]["field"] == "presenting_complaint"
    assert body["truncated"] is False


async def test_the_search_never_crosses_an_account(auth_client, second_auth_client):
    """The worst single failure this endpoint could have."""
    patient = await create_patient(auth_client)
    await _visit(auth_client, patient["id"], "Chest pain on exertion", "ECG normal.")
    body = await _search(second_auth_client, "chest pain")
    assert body["results"] == []


async def test_a_patient_id_from_another_account_matches_nothing_rather_than_that_chart(
    auth_client, second_auth_client
):
    """The account join is applied *as well as* the patient filter, never instead of it."""
    patient = await create_patient(auth_client)
    await _visit(auth_client, patient["id"], "Chest pain on exertion", "ECG normal.")
    body = await _search(second_auth_client, "chest pain", patient_id=patient["id"])
    assert body["results"] == []


async def test_scoping_to_a_chart_excludes_the_rest_of_the_panel(auth_client):
    first = await create_patient(auth_client)
    second = await create_patient(auth_client, full_name="Asha Devi")
    await _visit(auth_client, first["id"], "Chest pain on exertion", "ECG normal.")
    await _visit(auth_client, second["id"], "Chest pain at rest", "Referred.")
    body = await _search(auth_client, "chest pain", patient_id=first["id"])
    assert [r["patient_id"] for r in body["results"]] == [first["id"]]


async def test_an_amended_note_is_returned_marked_as_superseded(auth_client):
    """The clinical trap. "Penicillin — tolerated" is a perfect match for a search about
    penicillin, and if it was corrected afterwards, handing it back unmarked is this system
    answering a safety question with a retracted statement.
    """
    patient = await create_patient(auth_client)
    original = await _visit(
        auth_client, patient["id"], "Antibiotic review", "Penicillin tolerated previously."
    )
    signed = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/encounters/{original['id']}/sign"
    )
    assert signed.status_code == 200, signed.text
    amendment = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/encounters/{original['id']}/amend",
        json={
            "amendment_reason": "Allergy history corrected after speaking to the patient.",
            "clinician_notes": "Penicillin caused a rash. Recorded as an allergy.",
        },
    )
    assert amendment.status_code == 201, amendment.text
    amendment_id = amendment.json()["id"]
    signed_amendment = await auth_client.post(
        f"/api/v1/patients/{patient['id']}/encounters/{amendment_id}/sign"
    )
    assert signed_amendment.status_code == 200, signed_amendment.text

    body = await _search(auth_client, "penicillin")
    by_id = {r["encounter_id"]: r for r in body["results"]}
    assert by_id[original["id"]]["status"] == "amended"
    assert by_id[original["id"]]["superseded_by"] == amendment_id
    # The current version carries no supersession of its own.
    assert by_id[amendment_id]["superseded_by"] is None


async def test_a_draft_amendment_does_not_yet_supersede_anything(auth_client):
    """Somebody partway through a correction has not corrected it."""
    patient = await create_patient(auth_client)
    original = await _visit(auth_client, patient["id"], "Antibiotic review", "Penicillin fine.")
    await auth_client.post(f"/api/v1/patients/{patient['id']}/encounters/{original['id']}/sign")
    await auth_client.post(
        f"/api/v1/patients/{patient['id']}/encounters/{original['id']}/amend",
        json={"amendment_reason": "Rechecking with the patient.", "clinician_notes": "Pending."},
    )
    body = await _search(auth_client, "penicillin")
    hit = next(r for r in body["results"] if r["encounter_id"] == original["id"])
    assert hit["status"] == "signed"
    assert hit["superseded_by"] is None


async def test_a_wildcard_in_the_query_is_a_literal_not_a_match_for_everything(auth_client):
    """ "50%" is a thing clinicians write, and an unescaped LIKE wildcard is a full scan that
    matches every note on the panel."""
    patient = await create_patient(auth_client)
    await _visit(auth_client, patient["id"], "Ankle sprain", "Ice and rest.")
    await _visit(auth_client, patient["id"], "Burns review", "Approximately 5% surface area.")
    body = await _search(auth_client, "5% surface")
    assert [r["snippets"][0]["field"] for r in body["results"]] == ["clinician_notes"]
    assert len(body["results"]) == 1


async def test_a_query_of_only_stop_words_returns_nothing_not_the_whole_panel(auth_client):
    patient = await create_patient(auth_client)
    await _visit(auth_client, patient["id"], "Ankle sprain", "Ice and rest.")
    body = await _search(auth_client, "the and of")
    assert body["results"] == []


async def test_a_query_shorter_than_the_floor_is_a_422(auth_client):
    resp = await auth_client.post("/api/v1/notes/search", json={"query": "a"})
    assert resp.status_code == 422


async def test_a_withdrawn_chart_is_not_searchable(auth_client, db):
    """A soft-deleted patient is excluded from every other clinical read; the search must not
    be the way back in."""
    patient = await create_patient(auth_client)
    await _visit(auth_client, patient["id"], "Chest pain on exertion", "ECG normal.")
    resp = await auth_client.delete(f"/api/v1/patients/{patient['id']}")
    assert resp.status_code in (200, 204), resp.text
    body = await _search(auth_client, "chest pain")
    assert body["results"] == []


async def test_the_search_is_audited_without_recording_the_query(auth_client, db):
    """A permanent, unencrypted record of what a clinician was looking for, scoped to one chart,
    names a patient and a suspicion in the same row."""
    patient = await create_patient(auth_client)
    await _visit(auth_client, patient["id"], "Chest pain on exertion", "ECG normal.")
    await _search(auth_client, "chest pain", patient_id=patient["id"])
    row = (
        (await db.execute(select(AuditLog).where(AuditLog.action == "clinical_notes_searched")))
        .scalars()
        .one()
    )
    assert str(row.patient_id) == patient["id"]
    assert row.payload["term_count"] == 2
    assert row.payload["results"] == 1
    serialized = str(row.payload).lower()
    assert "chest" not in serialized and "pain" not in serialized


async def test_the_limit_is_honoured(auth_client):
    patient = await create_patient(auth_client)
    for index in range(5):
        await _visit(auth_client, patient["id"], f"Chest pain episode {index}", "Reviewed.")
    body = await _search(auth_client, "chest pain", limit=2)
    assert len(body["results"]) == 2


async def test_the_patient_id_in_the_body_must_be_a_uuid(auth_client):
    resp = await auth_client.post(
        "/api/v1/notes/search", json={"query": "chest pain", "patient_id": "not-a-uuid"}
    )
    assert resp.status_code == 422


async def test_an_anonymous_caller_is_refused(client):
    resp = await client.post("/api/v1/notes/search", json={"query": "chest pain"})
    assert resp.status_code == 401


async def test_a_soft_deleted_encounter_is_not_searchable(auth_client, db):
    from app.models.encounter import Encounter

    patient = await create_patient(auth_client)
    visit = await _visit(auth_client, patient["id"], "Chest pain on exertion", "ECG normal.")
    row = await db.get(Encounter, uuid.UUID(visit["id"]))
    assert row is not None
    row.is_deleted = True
    await db.commit()
    body = await _search(auth_client, "chest pain")
    assert body["results"] == []
