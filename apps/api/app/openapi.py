"""OpenAPI documentation helpers: the shared error envelope and the tag descriptions.

Two things this module exists to keep honest.

**The error envelope is uniform, and the schema should say so.** Every domain failure leaves
``main.aether_error_handler`` as ``{"code", "message"}`` -- see :class:`app.exceptions.AetherError`
-- but FastAPI only documents a response it is told about, so an endpoint that could 404 or
409 advertised nothing but its happy path. A generated client therefore had no type for the
one branch it must handle, and the frontend's error rendering was written from reading the
Python rather than from the contract. :func:`errors` attaches the real shape.

**Request-validation 422s are a different shape and are left alone.** FastAPI adds its own 422
(``{"detail": [...]}``) to any route with a body or query model, and that is accurate --
``main.validation_error_handler`` returns exactly that, minus the rejected input. Passing 422
to :func:`errors` would overwrite an accurate entry with a wrong one, so it is rejected
outright. Domain errors that happen to use 422 (consent, unsupported file type) are described
in the endpoint's own text instead.
"""

from __future__ import annotations

from typing import Any

from app.schemas.common import ErrorResponse

# Phrased for whoever is integrating against the API, and describing what the *code* field will
# say -- that is the stable part of the envelope. ``message`` is clinician-facing prose and is
# rewritten whenever it reads badly mid-consultation, so no client should match on it.
_ERROR_DESCRIPTIONS: dict[int, str] = {
    400: "Malformed request — `code` names the specific problem.",
    401: (
        "No valid access token. `code` is `invalid_token` for a missing, expired, malformed "
        "or wrong-type token, and `invalid_credentials` when sign-in details are rejected."
    ),
    403: (
        "Authenticated, but not permitted. `code` is `forbidden` when this account does not "
        "have access to the record, and `consent_withdrawn` when the patient has withdrawn "
        "consent — the chart stays readable, but nothing new may be added to it and the "
        "reasoning engine may not be run over it until consent is re-recorded (DPDP Act)."
    ),
    404: (
        "No such record, or it belongs to another account — the two are deliberately "
        "indistinguishable so the API does not confirm that a chart exists."
    ),
    409: (
        "The request conflicts with current state. `code` is `hard_block` when a deterministic "
        "drug-safety rule blocks the action (overridable only with documented reasoning), "
        "`email_exists` on duplicate signup, `concurrent_approval` when another approval of the "
        "same document merged first and this one was rolled back untouched (safe to retry), "
        "`reasoning_in_progress` when a pipeline run already holds that session (wait for it "
        "rather than starting a second — two runs write two sets of immutable suggestions "
        "against one session), `concurrent_answer` when another submission recorded an answer "
        "to one of the same intake questions first (reload the intake and re-enter anything "
        "missing — a clarifying question has exactly one answer of record), "
        "`extraction_in_progress` when a document is still being read and a second extraction "
        "of it was asked for, `extraction_already_approved` when re-reading a document whose "
        "extracted details are already merged into the record was asked for, `conflict` "
        "otherwise."
    ),
    429: (
        "Rate limited. `code` is `rate_limited` when a per-account ceiling on work that costs "
        "an LLM call was exceeded — back off for the seconds named in the `Retry-After` header "
        "and retry, nothing was written. It is `too_many_attempts` for the sign-in lockout, "
        "which is a security control and clears only after a cooldown of minutes; retrying is "
        "not the fix there."
    ),
    500: (
        "Unhandled server error (`code: internal_error`). The request's transaction is rolled "
        "back before this is returned, so nothing was half-written and a retry is safe. Carries "
        "`request_id` for correlation with the server log."
    ),
    503: (
        "This instance is not currently able to serve requests (`code: not_ready`) — a backing "
        "service it cannot work without did not answer in time. Distinct from 500: nothing is "
        "broken about the request, so retrying it against a healthy instance succeeds. The "
        "cause is logged against the request id and deliberately kept out of the body, which "
        "is answered before any authentication."
    ),
}

# 422 has two shapes on this API and only one of them is ours -- see the module docstring.
_RESERVED_STATUS = 422


def errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """Document the ``{code, message}`` error envelope for the given status codes.

    Pass the statuses an endpoint can actually return. Documenting one it cannot is worse than
    documenting none: it sends the client's error handling after a branch that never happens.
    """
    if _RESERVED_STATUS in statuses:
        raise ValueError(
            "422 is generated by FastAPI from the request model and documented accurately "
            "there; describe domain 422s (consent, file type) in the endpoint description."
        )
    unknown = sorted(set(statuses) - _ERROR_DESCRIPTIONS.keys())
    if unknown:
        raise ValueError(f"No error description for status {unknown}; add one to app/openapi.py")
    return {
        status: {"model": ErrorResponse, "description": _ERROR_DESCRIPTIONS[status]}
        for status in statuses
    }


# Every route that takes a bearer token can 401; spelling it out at each call site is noise.
AUTH_ERRORS = errors(401)
# The common shape for a route addressing one patient's chart.
PATIENT_ERRORS = errors(401, 404)


TAGS_METADATA: list[dict[str, Any]] = [
    {
        "name": "health",
        "description": (
            "Liveness, readiness, and dependency probes. Unauthenticated. `llm_mode` reports "
            "whether clinical reasoning is `live`, `demo` (simulated sample output) or "
            "`offline` — deterministic safety checks keep working in all three."
        ),
    },
    {
        "name": "auth",
        "description": (
            "Email/password sign-in issuing a short-lived access token and a rotating refresh "
            "token. Sessions are listed and revocable; reuse of a rotated refresh token "
            "revokes the whole family and is audited."
        ),
    },
    {
        "name": "patients",
        "description": (
            "Patient chart CRUD. Name, phone, address and notes are encrypted at rest, which "
            "is why search is a POST with the term in the body rather than in a logged URL."
        ),
    },
    {
        "name": "documents",
        "description": (
            "Prescription and report ingestion: upload, extraction review, and clinician "
            "approval into the longitudinal record. Nothing extracted reaches the patient "
            "graph until it is approved."
        ),
    },
    {
        "name": "records",
        "description": (
            "The assembled longitudinal record and the deterministic critical-lab check. Both "
            "read the patient graph directly and need no LLM."
        ),
    },
    {
        "name": "drug-safety",
        "description": (
            "Deterministic allergy, interaction and contraindication checking against the "
            "patient's current medications. Rule-based and offline-capable by design "
            "(no LLM in this path). Hard blocks are never bypassed silently — they require an "
            "explicit override carrying documented clinical reasoning."
        ),
    },
    {
        "name": "reasoning",
        "description": (
            "The eight-agent diagnostic reasoning engine: adaptive intake, the specialist "
            "panel, the can't-miss sentinel, the devil's advocate, and the Verifier that every "
            "clinical output passes through. Outputs are suggestions carrying an autonomy tier "
            "— the clinician decides."
        ),
    },
    {
        "name": "guidelines",
        "description": (
            "Retrieval over the curated ICMR/WHO/NICE corpus. Every result carries its source "
            "citation; nothing here is generated."
        ),
    },
    {
        "name": "pathways",
        "description": (
            "Curated staged care pathways per condition. Reference lookups, not engine output."
        ),
    },
    {
        "name": "audit",
        "description": (
            "The append-only, hash-chained audit trail for a patient, and its verification "
            "endpoint. Entries are never updated or deleted."
        ),
    },
    {
        "name": "validation",
        "description": (
            "Validation harness, performance metrics, the CDSCO SaMD dossier, and clinician "
            "safety reports for the monitored pilot."
        ),
    },
]
