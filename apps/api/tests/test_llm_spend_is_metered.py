"""Every route that spends upstream LLM budget carries a ceiling — as an inventory, not a habit.

``test_rate_limit`` asserts, route by route, that the metered routes are metered and that the
deterministic ones are not. Both are worth having and neither can speak for a route that does not
exist yet, which is how ``POST /validation/run`` shipped unmetered: it replays every
gold-standard vignette, so one request costs several full eight-agent panels plus their intake
rounds — more upstream spend than the ``POST ../run`` sitting behind a ten-per-minute ceiling
right next to it. Nothing failed, because nothing was watching the set of routes as a whole.
The cheapest way to drain the provider budget was the one route nobody had thought to cap.

Reachability of ``app.agents.llm`` through the import graph cannot decide this, which is why the
inventory below is written out by hand instead. It is wrong in both directions: ``app.routers.
health`` imports the module to ask ``is_available()`` and spends nothing, while
``app.routers.documents`` reaches the extraction client only through a deferred import and spends
on every upload. What a route costs is a fact about what it *calls*, and the honest way to hold
that is to name it.

So each API route is classified once, here. A new route fails this test until someone classifies
it, and classifying it as ``LLM`` fails again until it is given a ceiling. That is the point: the
decision becomes a line in a diff rather than an omission nobody sees.
"""

from __future__ import annotations

import pytest

from app.dependencies import limit_for
from app.main import create_app

# --- The inventory -----------------------------------------------------------------------------

# Routes that reach an LLM provider, directly or through the engine. Each must carry a ceiling.
LLM = "llm"
# Routes that cost only local work — a query, a deterministic computation, a stored file. These
# must stay unmetered for the reason in ``app/core/rate_limit.py``: a 429 on an allergy
# cross-check reads to a hurried clinician as "no conflict found".
DETERMINISTIC = "deterministic"
# Routes with no upstream spend where a ceiling is nonetheless justified on other grounds —
# account creation walking around the per-account limits, and the vector store's responsiveness.
BOUNDED_FOR_OTHER_REASONS = "bounded"
# Local work whose cost is *not* bounded by the request that asks for it, and which must
# therefore carry a ceiling even though it spends nothing upstream. The distinction
# ``DETERMINISTIC`` was hiding: "costs no LLM call" and "must never be throttled" are not the
# same claim. A paged chart read is bounded by its page size and a drug-safety check by the
# chart; a whole-chart FHIR export is deliberately unpaged, and an audit-chain walk hashes every
# row of a table that is append-only and never pruned. Neither is a safety-critical read a
# clinician meets mid-consultation, so Rule #8's argument does not reach them — and unmetered,
# each is a bulk-disclosure or CPU-amplification surface. Each of these must be metered, which
# is asserted below in both directions.
LOCAL_BUT_UNBOUNDED = "local_unbounded"

ROUTE_COST: dict[tuple[str, str], str] = {
    # --- auth. No clinical work; signup is capped so accounts cannot be minted to bypass the
    # per-account ceilings everywhere else.
    ("POST", "/api/v1/auth/signup"): BOUNDED_FOR_OTHER_REASONS,
    ("POST", "/api/v1/auth/login"): DETERMINISTIC,
    ("POST", "/api/v1/auth/logout"): DETERMINISTIC,
    ("POST", "/api/v1/auth/logout-all"): DETERMINISTIC,
    ("POST", "/api/v1/auth/refresh"): DETERMINISTIC,
    ("GET", "/api/v1/auth/me"): DETERMINISTIC,
    ("GET", "/api/v1/auth/sessions"): DETERMINISTIC,
    ("DELETE", "/api/v1/auth/sessions/{session_id}"): DETERMINISTIC,
    ("POST", "/api/v1/auth/password"): DETERMINISTIC,
    # Unauthenticated, so there is no account to key a ceiling on — capped per address instead,
    # for the same reason signup is: without it an unauthenticated caller can walk an address
    # list. The per-*account* ceiling that stops one clinician's inbox being flooded is
    # password_reset_max_requests_per_hour, enforced in the service.
    ("POST", "/api/v1/auth/password-reset/request"): BOUNDED_FOR_OTHER_REASONS,
    ("POST", "/api/v1/auth/password-reset/confirm"): DETERMINISTIC,
    # --- patients and chart reads. Never metered; a clinician must always be able to read.
    ("POST", "/api/v1/patients"): DETERMINISTIC,
    ("GET", "/api/v1/patients"): DETERMINISTIC,
    ("POST", "/api/v1/patients/search"): DETERMINISTIC,
    ("GET", "/api/v1/patients/{patient_id}"): DETERMINISTIC,
    ("PATCH", "/api/v1/patients/{patient_id}"): DETERMINISTIC,
    ("DELETE", "/api/v1/patients/{patient_id}"): DETERMINISTIC,
    ("GET", "/api/v1/patients/{patient_id}/record"): DETERMINISTIC,
    # --- encounters. Chart writes and paged chart reads: a row read, a row written, no model
    # in the path. Deliberately unmetered like the rest of the chart — a clinician must always
    # be able to open a visit and sign the note they just wrote.
    ("GET", "/api/v1/patients/{patient_id}/encounters"): DETERMINISTIC,
    ("POST", "/api/v1/patients/{patient_id}/encounters"): DETERMINISTIC,
    ("GET", "/api/v1/patients/{patient_id}/encounters/{encounter_id}"): DETERMINISTIC,
    ("PATCH", "/api/v1/patients/{patient_id}/encounters/{encounter_id}"): DETERMINISTIC,
    ("POST", "/api/v1/patients/{patient_id}/encounters/{encounter_id}/sign"): DETERMINISTIC,
    ("POST", "/api/v1/patients/{patient_id}/encounters/{encounter_id}/amend"): DETERMINISTIC,
    # The FHIR export reads the graph and nothing else — no LLM, so it works offline like every
    # other chart read. It is metered all the same: it is the one read that is deliberately
    # unpaged, so it is both the most expensive and the bulk-disclosure surface.
    ("GET", "/api/v1/patients/{patient_id}/export"): LOCAL_BUT_UNBOUNDED,
    # The same chart, typeset instead of serialised, and so the same classification: unpaged,
    # and additionally it lays out and deflates every row it read. It shares the *bucket* with
    # the bundle above rather than carrying one of its own — the ceiling is on how often a whole
    # chart may be pulled out of the system, and that question has the same answer whichever
    # format it leaves in.
    ("GET", "/api/v1/patients/{patient_id}/export/pdf"): LOCAL_BUT_UNBOUNDED,
    # Paging the trail is an indexed read of one page. Verifying it recomputes a SHA-256 per
    # entry over the patient's whole history — different work, different classification.
    ("GET", "/api/v1/patients/{patient_id}/audit"): DETERMINISTIC,
    ("GET", "/api/v1/patients/{patient_id}/audit/verify"): LOCAL_BUT_UNBOUNDED,
    # --- documents. Upload runs multimodal extraction over the scan; everything else is
    # metadata, stored bytes, or a merge of an extraction that already happened.
    ("POST", "/api/v1/patients/{patient_id}/documents"): LLM,
    ("GET", "/api/v1/patients/{patient_id}/documents"): DETERMINISTIC,
    ("GET", "/api/v1/patients/{patient_id}/documents/{doc_id}"): DETERMINISTIC,
    ("POST", "/api/v1/patients/{patient_id}/documents/{doc_id}/approve"): DETERMINISTIC,
    ("GET", "/api/v1/patients/{patient_id}/documents/{doc_id}/extraction"): DETERMINISTIC,
    ("GET", "/api/v1/patients/{patient_id}/documents/{doc_id}/file"): DETERMINISTIC,
    # Retry re-runs the same multimodal extraction over the stored scan, so it costs exactly
    # what the upload costs and shares the upload's ceiling.
    ("POST", "/api/v1/patients/{patient_id}/documents/{doc_id}/extraction/retry"): LLM,
    # --- drug safety and labs. Critical Safety Rule #8 — these answer whenever asked.
    ("POST", "/api/v1/patients/{patient_id}/drug-safety/check"): DETERMINISTIC,
    ("GET", "/api/v1/patients/{patient_id}/drug-safety/flags"): DETERMINISTIC,
    ("POST", "/api/v1/patients/{patient_id}/drug-safety/override"): DETERMINISTIC,
    ("GET", "/api/v1/patients/{patient_id}/drug-safety/overrides"): DETERMINISTIC,
    ("GET", "/api/v1/patients/{patient_id}/labs/critical-flags"): DETERMINISTIC,
    # --- pathways. Rule-based matching against the local corpus.
    ("GET", "/api/v1/pathways"): DETERMINISTIC,
    ("GET", "/api/v1/pathways/{condition_name}"): DETERMINISTIC,
    ("GET", "/api/v1/patients/{patient_id}/pathways"): DETERMINISTIC,
    # --- reasoning. Opening a session and answering intake each cost a Triage agent call; the
    # run and the stream are the same eight-agent panel and share one budget. The rest read
    # what a run already persisted.
    ("POST", "/api/v1/patients/{patient_id}/reasoning"): LLM,
    ("POST", "/api/v1/reasoning/{session_id}/intake/answers"): LLM,
    ("POST", "/api/v1/reasoning/{session_id}/run"): LLM,
    ("GET", "/api/v1/reasoning/{session_id}/stream"): LLM,
    ("GET", "/api/v1/reasoning/{session_id}"): DETERMINISTIC,
    ("GET", "/api/v1/reasoning/{session_id}/intake"): DETERMINISTIC,
    ("POST", "/api/v1/reasoning/{session_id}/stream-token"): DETERMINISTIC,
    ("GET", "/api/v1/reasoning/{session_id}/suggestions"): DETERMINISTIC,
    ("GET", "/api/v1/reasoning/{session_id}/management-options"): DETERMINISTIC,
    ("POST", "/api/v1/reasoning/{session_id}/suggestions/{suggestion_id}/decision"): DETERMINISTIC,
    # --- guidelines. Search embeds the query and hits Qdrant — no generation, but the ceiling
    # keeps the vector store responsive. The corpus listing is a count.
    ("GET", "/api/v1/guidelines/search"): BOUNDED_FOR_OTHER_REASONS,
    ("GET", "/api/v1/guidelines/corpus"): DETERMINISTIC,
    # --- Phase 4. The validation harness replays every vignette through the whole engine, which
    # is what this file exists for. Everything else here reads what it wrote.
    ("POST", "/api/v1/validation/run"): LLM,
    ("GET", "/api/v1/validation/runs"): DETERMINISTIC,
    ("GET", "/api/v1/validation/runs/{run_id}"): DETERMINISTIC,
    ("GET", "/api/v1/metrics/performance"): DETERMINISTIC,
    ("GET", "/api/v1/pilot/status"): DETERMINISTIC,
    # Reports the audit chain's length and validity, which means verify_full_chain — a SHA-256
    # over every row in audit_logs. The same work as the per-patient walk, one scale worse, and
    # it shares that walk's budget.
    ("GET", "/api/v1/regulatory/samd-dossier"): LOCAL_BUT_UNBOUNDED,
    ("POST", "/api/v1/safety-reports"): DETERMINISTIC,
    ("GET", "/api/v1/safety-reports"): DETERMINISTIC,
}

# Metered inside the handler rather than by a route dependency, so the dependency walk below
# cannot see it. ``rate_limit()`` resolves the account from the bearer header, and the stream
# deliberately also accepts the ``?token=`` stream token that ``get_current_account`` rejects —
# so it calls ``enforce_rate_limit`` itself. Named here because "no dependency" and "no ceiling"
# must not look the same to this test.
METERED_IN_HANDLER = {("GET", "/api/v1/reasoning/{session_id}/stream")}


def _api_routes() -> dict[tuple[str, str], object]:
    app = create_app()
    found = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or not hasattr(route, "dependant"):
            continue
        for method in route.methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            found[(method, path)] = route
    return found


def _has_limiter_dependency(route: object) -> bool:
    """Whether a ``rate_limit``/``rate_limit_by_ip`` dependency is attached to the route.

    Both build their enforcer as a closure named ``enforce``, so the closure's qualified name
    carries the factory that produced it. Matching on that rather than on identity keeps this
    working for every bucket without the test having to know which buckets exist.
    """
    return any(
        "rate_limit" in getattr(dep.call, "__qualname__", "")
        for dep in route.dependant.dependencies  # type: ignore[attr-defined]
    )


def _is_metered(key: tuple[str, str], route: object) -> bool:
    return key in METERED_IN_HANDLER or _has_limiter_dependency(route)


# --- The inventory matches reality --------------------------------------------------------------


def test_every_api_route_is_classified():
    """A new route is unclassified until someone says what it costs, and that fails here."""
    missing = sorted(set(_api_routes()) - set(ROUTE_COST))
    assert not missing, (
        "these routes have no entry in ROUTE_COST — classify each as LLM (and give it a "
        f"ceiling) or DETERMINISTIC: {missing}"
    )


def test_the_inventory_names_no_route_that_does_not_exist():
    """A stale entry would silently excuse a route that was renamed rather than removed."""
    stale = sorted(set(ROUTE_COST) - set(_api_routes()))
    assert not stale, f"ROUTE_COST names routes the app does not serve: {stale}"


# --- What the classification requires -----------------------------------------------------------


def test_every_route_that_spends_llm_budget_is_metered():
    """The invariant ``POST /validation/run`` broke."""
    routes = _api_routes()
    unmetered = sorted(
        key for key, cost in ROUTE_COST.items() if cost == LLM and not _is_metered(key, routes[key])
    )
    assert not unmetered, (
        "these routes spend upstream LLM budget with no ceiling, so a loop on any of them "
        f"drains the provider quota for every clinician on the deployment: {unmetered}"
    )


def test_no_deterministic_route_is_metered():
    """Critical Safety Rule #8, from the other side.

    A 429 on an allergy cross-check is not a delay, it is a hurried clinician reading an error
    where a hard block should have been. Metering one of these would be a safety regression that
    looks like tidiness in review.
    """
    routes = _api_routes()
    metered = sorted(
        key
        for key, cost in ROUTE_COST.items()
        if cost == DETERMINISTIC and _is_metered(key, routes[key])
    )
    assert not metered, (
        f"these routes are local and must answer whenever they are asked, but are metered: "
        f"{metered}"
    )


def test_every_locally_unbounded_route_is_metered():
    """The other half of the LLM invariant, for work that costs CPU and disclosure instead.

    Classifying a route ``LOCAL_BUT_UNBOUNDED`` is a statement that its cost does not follow from
    the request, so leaving it uncapped is the same omission ``POST /validation/run`` was.
    """
    routes = _api_routes()
    unmetered = sorted(
        key
        for key, cost in ROUTE_COST.items()
        if cost == LOCAL_BUT_UNBOUNDED and not _is_metered(key, routes[key])
    )
    assert not unmetered, (
        "these routes do unbounded local work with no ceiling, so a loop on any of them buys "
        f"unbounded CPU or walks every chart in the account at line rate: {unmetered}"
    )


def test_the_metered_in_handler_exemption_covers_only_routes_without_a_dependency():
    """If the stream ever gains a real dependency, the exemption should go rather than linger."""
    routes = _api_routes()
    redundant = sorted(k for k in METERED_IN_HANDLER if _has_limiter_dependency(routes[k]))
    assert not redundant, f"these no longer need the in-handler exemption: {redundant}"


# --- The ceiling the harness got ------------------------------------------------------------------


def test_the_validation_harness_ceiling_is_tighter_than_a_single_panels():
    """One validation request costs several panels, so its ceiling must not be the looser one.

    Sized per hour against ``reasoning_run``'s per minute. Comparing them as requests would be
    meaningless — what matters is that the route costing the most cannot be called the most.
    """
    harness = limit_for("validation_run")
    panel = limit_for("reasoning_run")
    assert harness is not None and panel is not None
    per_second = harness.max_requests / harness.window_seconds
    assert per_second < panel.max_requests / panel.window_seconds


@pytest.mark.asyncio
async def test_the_harness_refuses_once_its_hourly_ceiling_is_spent(auth_client, monkeypatch):
    """End to end, because a bucket that is declared but never consulted still limits nothing."""
    monkeypatch.setattr("app.config.settings.rate_limit_validation_runs_per_hour", 1)

    first = await auth_client.post("/api/v1/validation/run")
    assert first.status_code == 201, first.text

    second = await auth_client.post("/api/v1/validation/run")
    assert second.status_code == 429, second.text
    assert second.json()["code"] == "rate_limited"
    assert int(second.headers["Retry-After"]) > 0
