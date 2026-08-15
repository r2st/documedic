"""The OpenAPI schema is the API's public contract, so these are contract tests.

The schema is what a frontend developer reads, what a generated client is built from, and what
`/docs` renders. It had drifted into being none of those: no route carried a `summary`, no
security scheme was declared (so the spec claimed every patient route was open and `/docs`
offered no Authorize button), and the one documented error shape advertised a field the API
never returns.

These tests keep the parts a client actually depends on from silently regressing — a new route
added without a summary, an authenticated route whose 401 is undocumented, or a leak of
clinician-facing prose into a place a client would be tempted to match on.
"""

from __future__ import annotations

import re

import pytest

from app.main import create_app
from app.openapi import errors

# Documented as open on purpose: probes, and the sign-in endpoints that mint the token.
_PUBLIC_PATHS = {
    "/health",
    "/health/live",
    "/health/ready",
    "/health/dependencies",
    "/api/v1/auth/signup",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    # A clinician who has lost their password cannot hold a bearer token, so both halves of the
    # reset flow are necessarily open. Neither can be used to learn anything about an account:
    # the request endpoint's response is identical for an address with one and without, and the
    # confirm endpoint's failures are identical for all four ways to fail.
    "/api/v1/auth/password-reset/request",
    "/api/v1/auth/password-reset/confirm",
    # SSE: EventSource cannot send headers, so this authenticates from a bearer header *or* a
    # session-bound ?token=. That is hand-rolled inside the handler rather than a dependency,
    # which is why the scheme does not appear on it.
    "/api/v1/reasoning/{session_id}/stream",
}


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


def _operations(schema: dict):
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            yield path, method, operation


def test_the_schema_generates(schema):
    """A response model FastAPI cannot serialise raises here and nowhere else — `/docs` is not
    exercised by any other test, so a broken schema would ship silently."""
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"] == "Aether Clinician API"
    assert list(schema["paths"])


def test_every_route_has_a_summary(schema):
    """`summary` is the line rendered in the endpoint list; without it a reader sees the
    function name."""
    missing = [f"{m.upper()} {p}" for p, m, op in _operations(schema) if not op.get("summary")]
    assert missing == []


def test_every_route_has_a_description(schema):
    """The docstring becomes `description`. Several endpoints carry constraints a client cannot
    infer from types — that a hard block needs a documented override, that intake is iterative,
    that PDFs are never served inline — and this is where they are written down."""
    missing = [f"{m.upper()} {p}" for p, m, op in _operations(schema) if not op.get("description")]
    assert missing == []


def test_the_documentation_uses_no_imperative_clinical_language(schema):
    """CLAUDE.md rule #4 covers clinician-facing text, and the schema renders into `/docs`.

    Two shapes are checked, because they fail differently. A *command* ("Give drug X") is only
    a command in imperative position, so those verbs are matched at the start of a sentence —
    matching them anywhere flags ordinary prose like "both give the same 401". A *certainty
    claim* ("the patient has X") is wrong wherever it sits, so those match anywhere.
    """
    imperatives = ("give", "administer", "prescribe", "diagnose", "start the patient on")
    certainty = ("the patient has ", "the diagnosis is ", "diagnose with ")

    offenders = []
    for path, method, operation in _operations(schema):
        text = f"{operation.get('summary', '')}. {operation.get('description', '')}"
        route = f"{method.upper()} {path}"
        for sentence in re.split(r"(?<=[.!?:])\s+|\n", text):
            first = sentence.strip().lower().split(" ")[0].rstrip(",")
            if first in imperatives:
                offenders.append((route, sentence.strip()[:60]))
        offenders.extend((route, phrase) for phrase in certainty if phrase in text.lower())
    assert offenders == []


def test_authenticated_routes_declare_the_bearer_scheme(schema):
    """Without a security scheme in the dependency tree the spec claims these routes are open.

    That is not cosmetic: a generated client has no idea to send the header, and `/docs` has
    no Authorize button, so every authenticated endpoint appears to 401 for no reason.
    """
    undeclared = [
        f"{m.upper()} {p}"
        for p, m, op in _operations(schema)
        if p not in _PUBLIC_PATHS and not op.get("security")
    ]
    assert undeclared == []
    assert "AccessToken" in schema["components"]["securitySchemes"]
    assert schema["components"]["securitySchemes"]["AccessToken"]["scheme"] == "bearer"


def test_authenticated_routes_document_their_401(schema):
    """A 401 is the one error every client must handle, and it is where the refresh-then-retry
    path hangs off."""
    undocumented = [
        f"{m.upper()} {p}"
        for p, m, op in _operations(schema)
        if p not in _PUBLIC_PATHS and "401" not in op.get("responses", {})
    ]
    assert undocumented == []


def test_patient_scoped_routes_document_their_404(schema):
    """Every `{patient_id}` route 404s for an unknown *or* another account's chart, and a
    client that treats 404 as "bug" rather than "not yours" gets that wrong."""
    undocumented = [
        f"{m.upper()} {p}"
        for p, m, op in _operations(schema)
        if "{patient_id}" in p and "404" not in op.get("responses", {})
    ]
    assert undocumented == []


def test_documented_errors_all_use_the_shared_envelope(schema):
    """Every declared 4xx/5xx points at `ErrorResponse`, except the 422 FastAPI generates."""
    wrong = []
    for path, method, operation in _operations(schema):
        for status, response in operation.get("responses", {}).items():
            if not status.startswith(("4", "5")) or status == "422":
                continue
            ref = response.get("content", {}).get("application/json", {}).get("schema", {})
            if ref.get("$ref") != "#/components/schemas/ErrorResponse":
                wrong.append(f"{method.upper()} {path} -> {status}")
    assert wrong == []


def test_the_error_envelope_does_not_advertise_fields_the_api_withholds(schema):
    """`AetherError.detail` is the internal cause and is logged, never serialised.

    `ErrorResponse` used to declare a `detail` field anyway, pointing clients at something that
    is never populated — and, worse, implying the API hands back the mechanism behind a 401.
    """
    properties = schema["components"]["schemas"]["ErrorResponse"]["properties"]
    assert set(properties) == {"code", "message", "request_id"}


def test_error_helper_rejects_a_status_it_has_no_description_for():
    """Silently emitting an empty description would be worse than refusing: the schema would
    look documented."""
    with pytest.raises(ValueError, match="No error description"):
        errors(418)


def test_error_helper_refuses_to_overwrite_the_generated_422():
    """FastAPI's 422 is the accurate one — `{detail: [...]}`, from the request model. Passing
    422 here would replace it with the `{code, message}` shape, which request validation does
    not return."""
    with pytest.raises(ValueError, match="422"):
        errors(422)


def test_every_query_parameter_is_described(schema):
    """A query parameter's name and type are rarely the part a caller gets wrong.

    `limit` on the longitudinal record is per *section* rather than a total; `offset` on that
    same route is applied to five collections independently; `action` on the audit trail is an
    exact match and not a prefix. None of that is inferable from `limit: integer`, and a
    parameter's `description` is the only place in the schema where it can be said — path and
    body fields at least arrive with the endpoint's own prose around them.

    Path parameters are deliberately excluded: they are positional and named for the resource
    they identify (`patient_id`, `session_id`), so a description on each would be noise.
    """
    undescribed = [
        f"{method.upper()} {path} ?{param['name']}"
        for path, method, operation in _operations(schema)
        for param in operation.get("parameters", [])
        if param.get("in") == "query" and not param.get("description", "").strip()
    ]
    assert undescribed == [], undescribed


def test_every_paginated_route_publishes_its_ceiling(schema):
    """An unbounded `limit` is the unbounded read wearing a query parameter.

    Every paging control on this API exists to stop a response scaling with how much history a
    patient has accumulated, and a `limit` with no `maximum` hands that straight back to the
    caller — including the caller who passes 100000 to avoid writing a paging loop. The schema
    is where a client discovers the ceiling, so the ceiling has to be in the schema and not
    only in the handler's signature.
    """
    unbounded = [
        f"{method.upper()} {path} ?{param['name']}"
        for path, method, operation in _operations(schema)
        for param in operation.get("parameters", [])
        if param.get("in") == "query"
        and param["name"] == "limit"
        and "maximum" not in param.get("schema", {})
    ]
    assert unbounded == [], unbounded


def test_every_tag_in_use_is_described(schema):
    """A tag with no `openapi_tags` entry renders as a bare heading with no explanation of what
    the group is for."""
    described = {tag["name"] for tag in schema["tags"]}
    used = {tag for _, _, op in _operations(schema) for tag in op.get("tags", [])}
    assert used - described == set()


@pytest.mark.asyncio
async def test_docs_and_schema_are_actually_served(client):
    """The schema being generatable in-process is not the same as it being reachable, and
    neither is behind auth — the contract is public even though the data is not."""
    assert (await client.get("/openapi.json")).status_code == 200
    assert (await client.get("/docs")).status_code == 200


@pytest.mark.asyncio
async def test_the_served_schema_matches_the_generated_one(client):
    served = (await client.get("/openapi.json")).json()
    assert served["info"]["title"] == "Aether Clinician API"
    assert served["paths"].keys() == create_app().openapi()["paths"].keys()
