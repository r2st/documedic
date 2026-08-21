"""Application settings, loaded from environment via pydantic-settings."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration. Values come from environment / .env files."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---
    app_env: Literal["development", "staging", "production", "test"] = "development"
    app_secret_key: str = "dev-insecure-secret-change-me"
    app_debug: bool = True
    demo_mode: bool = True

    # --- Logging ---
    # Applied to the root logger by app.core.logging_config, which is the only thing in this
    # process that configures logging at all. Left at WARNING — the Python default this
    # replaces — the whole INFO diagnostic channel is discarded, including the one place
    # AetherError.detail is ever written. INFO is the floor for a system that has to be able to
    # explain a refusal after the fact.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # `text` is a human reading journalctl; `json` is one object per line for an aggregator that
    # needs to index request_id. Both carry the same fields.
    log_format: Literal["text", "json"] = "text"

    # How long GET /health/ready waits for the database before answering "not ready". A probe
    # is a question about the present, so it has to answer in bounded time: an unbounded await
    # on a database that accepted the connection and then stopped responding leaves the probe
    # hanging on a pooled connection for as long as the orchestrator will wait, which is the
    # one state a readiness probe exists to make visible.
    readiness_timeout_seconds: float = 5.0

    # --- Database ---
    database_url: str = "postgresql+asyncpg://aether:aether@localhost:5432/aether_clinician"
    database_pool_size: int = 20
    database_max_overflow: int = 10

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- LLM provider selection ---
    # The configured primary provider is tried first; the remaining providers act as
    # fallbacks (deduplicated) — see app.agents.llm._provider_order. Any of openai,
    # anthropic, or openrouter may be the primary. With llm_provider=openrouter the
    # chain is OpenRouter -> OpenAI -> Anthropic -> simulated demo, which avoids the
    # latency of failed OpenAI/Anthropic retries when OpenRouter is the intended path.
    llm_provider: Literal["openai", "anthropic", "openrouter"] = "openai"
    llm_fallback_enabled: bool = True
    # When true (and an OpenRouter key is present), OpenRouter is included in the chain —
    # as the primary when llm_provider=openrouter, otherwise as a trailing fallback tier.
    llm_openrouter_fallback: bool = True
    # Final safety net: when NO LLM provider can be reached (no key / offline / every
    # provider call failed), serve realistic simulated "[DEMO MODE]" clinical responses
    # instead of raising, so the product is always demonstrable. Default on — but REFUSED in
    # production (see production_config_errors and app.agents.llm.demo_fallback_enabled):
    # showing a clinician fabricated reasoning about a real patient is worse than an outage.
    llm_demo_fallback: bool = True
    # Per-request timeout (seconds) for each provider SDK call. Without this a hung upstream
    # connection blocks the worker thread indefinitely instead of failing over.
    llm_request_timeout_seconds: float = 30.0
    # Base delay (seconds) for exponential backoff between retries of the SAME provider.
    # Actual sleep = llm_retry_backoff_base_seconds * attempt_number, capped at 2s.
    llm_retry_backoff_base_seconds: float = 0.25
    # Timeout for the reachability probe behind GET /health/dependencies. Much shorter than
    # llm_request_timeout_seconds on purpose: a health endpoint that waits as long as a real
    # completion tells an operator nothing they could not have learned by waiting themselves.
    llm_health_probe_timeout_seconds: float = 5.0
    # How long a probe result (success OR failure) is reused. An unreachable provider is the
    # one whose probe is slowest, so re-running it per poll would make the status endpoint
    # hang for the length of the outage — and turn a monitoring poller into provider traffic.
    llm_health_probe_ttl_seconds: float = 60.0

    # --- Provider circuit breaker ---
    # Timeouts bound one socket; retries and the fallback chain then multiply that bound. A
    # provider that accepts connections and stops answering costs
    # llm_request_timeout_seconds x (retries + 1) per provider — 90s each, 270s across the chain
    # — and it costs it AGAIN on the next agent call, and the one after that, because nothing
    # remembered that the provider had just failed nine times in a row. That is the arithmetic
    # the reasoning_run_lease_minutes comment describes, and it is why an outage at a third
    # party turned into half an hour of a clinician watching an empty Reasoning Theatre.
    #
    # The breaker makes that cost be paid once. After llm_circuit_failure_threshold consecutive
    # *availability* failures (timeouts, connection errors, 429/5xx, and auth rejections — not
    # a malformed model response, which says nothing about whether the provider is up), the
    # provider is skipped without a call until the cooldown elapses, then one trial call decides
    # whether it is back. A run against a dead chain therefore degrades to the deterministic
    # path in milliseconds instead of minutes, which is what Critical Safety Rule #8 wants: the
    # offline safety checks were always going to answer, and now the clinician reaches them
    # while the question is still live.
    llm_circuit_breaker_enabled: bool = True
    # Consecutive availability failures that open a provider's breaker. Three, which is exactly
    # one exhausted complete_json attempt sequence — so a provider proves itself down over one
    # agent call and is skipped from the next one onwards, rather than over several.
    llm_circuit_failure_threshold: int = 3
    # How long an open breaker stays open before admitting one trial call. Short enough that a
    # provider blipping for a few seconds costs at most one run's worth of degradation, long
    # enough that a real outage is not re-probed by every agent of every run.
    llm_circuit_reset_seconds: float = 60.0

    # Size of the thread pool the provider SDKs are called on, and the reason that pool exists
    # separately at all. The SDKs are synchronous, so every call runs on a worker thread, and
    # `asyncio.to_thread` puts it on the event loop's *default* executor — which is shared with
    # everything else in the process that has to leave the loop: bcrypt in `verify_password`,
    # document blob reads and writes, upload hashing, the guideline embedder.
    #
    # That pool holds `min(32, cpu_count + 4)` threads, which is 6 on the 2-vCPU box this runs
    # on. One reasoning run's hypothesis panel occupies 4 of them at once (four specialists under
    # `asyncio.gather`), and a provider that accepts a connection and then stops answering holds
    # each for `llm_request_timeout_seconds` x (retries + 1) x every provider in the chain — 4.5
    # minutes on these defaults. So two clinicians running the panel against a hanging upstream
    # filled the default executor, and the next request needing a thread waited behind them:
    # logging in stopped working, uploads stopped working, and the cause was a third party's
    # socket. An LLM outage should degrade reasoning — the deterministic path is built for
    # exactly that — not take authentication down with it.
    #
    # Sized for two concurrent panels. Beyond that, runs queue for a slot, which is the correct
    # degradation: reasoning gets slower under load while nothing outside it is affected.
    llm_max_concurrent_calls: int = 8
    # Wall-clock budget for the LLM calls of one reasoning run, after which the run stops issuing
    # them and finishes on the deterministic path (marked degraded, escalated to flag-for-review).
    #
    # Nothing bounded a run before this. `llm_request_timeout_seconds` bounds one socket, and
    # `reasoning_run_lease_minutes` bounds how long a claim is honoured — neither stops a run
    # continuing to make calls. A hanging provider therefore cost 4.5 minutes per agent call
    # across seven sequential nodes plus the panel: past half an hour, of which the clinician
    # spends every minute watching a Reasoning Theatre that has stopped producing events, and at
    # the end of which the run has outlived its lease and may have to discard its own output.
    # Ten minutes of nothing, then a degraded answer, is strictly better than thirty minutes of
    # nothing and possibly no answer.
    #
    # Deliberately below the lease with room to spare. The budget is checked before each call
    # rather than interrupting one in flight — a synchronous SDK call on a worker thread cannot be
    # cancelled — so the real ceiling is this plus one worst-case call: 600 + 270 seconds, still
    # inside the 15-minute lease. Keep that inequality if either value moves.
    reasoning_llm_budget_seconds: float = 600.0

    # --- OpenAI (primary LLM) ---
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_max_tokens: int = 4096

    # --- Anthropic (fallback LLM + vision extraction) ---
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    anthropic_max_tokens: int = 4096
    extraction_prompt_version: str = "v1.0"

    # --- OpenRouter (fallback LLM, OpenAI-compatible API) ---
    # Tried after OpenAI/Anthropic. Uses the openai SDK with a custom base_url.
    # Default model is a capable free model suitable for clinical reasoning; override
    # via OPENROUTER_MODEL (e.g. "openrouter/auto" or any ":free" model).
    openrouter_api_key: str = ""
    openrouter_model: str = "nvidia/llama-3.3-nemotron-super-49b-v1:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_max_tokens: int = 4096

    @property
    def llm_configured(self) -> bool:
        """True when at least one usable LLM provider key is configured."""
        if self.openai_api_key:
            return True
        if self.llm_fallback_enabled and self.anthropic_api_key:
            return True
        return bool(self.llm_openrouter_fallback and self.openrouter_api_key)

    # --- Reasoning engine (Phase 2) ---
    reasoning_info_gain_threshold: float = 0.35
    reasoning_question_cap: int = 6
    reasoning_max_intake_rounds: int = 2

    # --- Guideline RAG (Phase 3) ---
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "guidelines"
    guideline_corpus_version: str = "icmr-2024.1"
    guideline_retrieval_threshold: float = 0.75
    guideline_retrieval_k: int = 6

    # --- Validation / pilot (Phase 4) ---
    pilot_mode: bool = False
    citation_faithfulness_target: float = 0.95

    # --- Storage ---
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_dir: str = "./storage"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket_name: str = "aether-documents"

    # --- OCR ---
    tesseract_cmd: str = "/usr/bin/tesseract"

    # --- Auth ---
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 7
    bcrypt_rounds: int = 12
    jwt_algorithm: str = "HS256"
    # A refresh token not used to mint a new access token within this window is treated as an
    # abandoned/idle session and rejected on its next use, even though its absolute
    # jwt_refresh_ttl_days expiry hasn't passed yet.
    session_idle_timeout_minutes: int = 30
    # EventSource cannot set an Authorization header, so the SSE stream authenticates from a
    # query parameter. That parameter lands in proxy access logs and browser history, so it
    # carries a narrowly scoped, very short-lived token instead of the real access token.
    stream_token_ttl_seconds: int = 60

    # --- Clinical summary ---
    # Token ceiling for the handover-summary completion. Smaller than the agent default: this is
    # one paragraph and five short lists over a chart that is already bounded by the per-section
    # slices in app.services.summary_service, not an eight-agent deliberation, and an unbounded
    # ceiling here would let a chatty provider turn a per-patient read into the most expensive
    # LLM route in the API.
    summary_max_tokens: int = 1500
    # How long a summary written from an unchanged chart may be re-served, and how many are
    # held. The cache key is a hash of the exact prompt the model is asked (see
    # ``app.services.summary_service``), so **any** change to the chart — a drug started, a lab
    # arriving, an allergy recorded — misses it. That is what makes caching clinical prose
    # defensible at all; this TTL is a ceiling on how long a *correct* answer is reused, not the
    # thing that keeps it correct.
    #
    # Five minutes because the value is in the burst: a ward-round tool opening the same chart on
    # two screens, a clinician navigating back, a page that re-mounts. Beyond that the saving is
    # small and the reason to hold patient-derived prose in memory gets weaker. Either at 0
    # switches the cache off entirely, and the route simply pays for every call.
    summary_cache_ttl_seconds: int = 300
    summary_cache_max_entries: int = 256

    # --- Clinical measurement staleness ---
    # How old a recorded body weight may be before the deterministic engine says so on a chart
    # carrying weight-dosed medication. Two windows, because a growing child's weight goes out
    # of date at a completely different rate from a stable adult's: the paediatric one applies
    # below app.core.dose_range.PAEDIATRIC_MAX_AGE_YEARS and to a chart with no usable date of
    # birth, since "might be a child" takes the shorter answer.
    #
    # Configuration rather than a constant because the right interval is a clinic's judgement —
    # a paediatric practice weighing at every visit wants a tighter window than a chronic-disease
    # clinic — but they reach app.core.safety as a WeightStalenessPolicy value, never read from
    # here inside that module, which is what keeps the offline engine a pure function of its
    # arguments (Critical Safety Rule #8).
    #
    # Set either to 0 or below to switch that arm off. That is a deliberate widening and it is
    # visible in the config, which is the same shape the rate-limit buckets take.
    weight_stale_adult_days: int = 183
    weight_stale_paediatric_days: int = 92

    # --- Record export ---
    # Per-section ceiling on a FHIR export. Unlike a chart read this is meant to be the whole
    # record, so the ceiling is far above any real patient and exists only to bound the worst
    # case — and when it is hit the bundle says so in an OperationOutcome rather than coming
    # back quietly short. See app.services.export_service.
    export_max_rows_per_section: int = 10_000

    # --- Password reset ---
    # A reset token is a bearer capability over one clinician's account, so its life is
    # measured in minutes rather than hours: long enough to walk from the request to the inbox
    # or the phone call that relays it, short enough that a token left in a log or a chat
    # window is dead before anyone finds it. Single-use as well as time-limited, and requesting
    # a new one invalidates the outstanding one.
    password_reset_token_ttl_minutes: int = 30
    # Per-account ceiling, counted from the audit trail like the login lockout, so it survives a
    # restart and cannot be walked around by moving to another address. Exceeding it is silent:
    # the endpoint's response never varies, or the ceiling itself would answer "does this email
    # have an account?".
    password_reset_max_requests_per_hour: int = 5
    # How the raw token reaches the person who asked for it. This deployment has no mail
    # provider, and inventing one is not this module's job — so the channel is an explicit
    # choice rather than a silent default:
    #
    #   "log"      — the default. Written to the application log for an operator to relay out
    #                of band. The token is a short-lived single-use secret sitting in a log an
    #                operator can read; that is a real cost, taken deliberately as the interim
    #                posture until a delivery integration exists, and it is why the TTL above
    #                is short.
    #   "response" — returned in the request's own response body, which makes the whole flow
    #                usable and testable with nothing but the API. It also means anyone who can
    #                POST an email address can take that account over, so it is opt-in rather
    #                than the default and is REFUSED in production (see
    #                production_config_errors).
    password_reset_delivery: Literal["response", "log"] = "log"
    # Floor on how long both password-reset routes take to answer, whichever branch they took.
    #
    # Everything above is written so the two routes cannot be used to ask "does this address
    # have an account?" — always 202, always the same body, one 401 message for all four ways a
    # token can be bad. The clock said otherwise. Issuing a token costs a count, an invalidation
    # sweep, an insert, an audit row and a commit; an address with no account returns after one
    # indexed SELECT. Measured against SQLite in-process that is 2.1x — and every one of those
    # extra steps is a network round-trip against a real PostgreSQL, so the gap is wider in the
    # deployment than on the bench, not narrower. `confirm` splits 1.9x the same way.
    #
    # 250ms is roughly fifty times the slower branch's own cost here, which is the headroom the
    # floor needs: it only equalises the branches while it exceeds both of them (see
    # `app.core.timing`, which logs when it stops doing so). It is charged only to these two
    # routes — a clinician meets them when they have forgotten their password, where a quarter
    # of a second is not a cost anyone can perceive, and never on the sign-in path.
    #
    # Zero disables it, for a test measuring something else.
    password_reset_min_response_seconds: float = 0.25

    # --- Session retention ---
    # How long a dead session row (revoked, or past its absolute expiry) is kept before the
    # retention sweep deletes it. These rows are an access record, not a clinical record — the
    # clinical trail is the append-only audit log, which is never pruned and carries every
    # sign-in, refresh, logout and revocation independently. Keeping the token hashes
    # themselves forever serves nothing and is data minimisation the DPDP Act asks for.
    #
    # The window exists so "which devices was this account signed in on last week?" is still
    # answerable from the session table during an incident, rather than only from the audit log.
    session_retention_days: int = 30
    # Rows removed per sweep. The sweep runs opportunistically on the auth path, so it must
    # cost a bounded amount of work rather than however much has accumulated: a deployment that
    # has never swept simply takes several passes to catch up.
    session_sweep_batch_size: int = 500
    # Minimum interval between sweeps, per process. Without it every refresh would issue a
    # delete.
    session_sweep_interval_minutes: int = 60
    # Ceiling on how many sign-ins one account may hold at once. 0 disables the cap.
    #
    # Enforced by evicting the least recently used sessions on a *new* sign-in, never by
    # refusing one: refusing would hand anyone who learns a password a way to lock the
    # clinician out of their own account by filling the cap, and a clinician refused sign-in
    # mid-shift is a worse outcome than the extra session. Rotation is not a new sign-in and
    # does not evict — `refresh` revokes the row it rotates, so the count is unchanged.
    #
    # Ten rather than two or three: a clinician legitimately holds several at once (ward
    # workstation, consulting room, phone, a tablet on rounds), and the control is aimed at
    # the account quietly accumulating dozens over months of shared terminals — each one a
    # live refresh token on a machine nobody remembers signing out of.
    session_max_concurrent: int = 10

    # --- Step-up re-authentication ---
    # How recently the person at the keyboard must have proved they hold the account password
    # before the API will perform a *sensitive* operation — deleting a chart, importing charts
    # in bulk, or exporting a whole record out of the system. See
    # ``app.dependencies.require_recent_authentication`` for which routes, and why those.
    #
    # This is not a second factor and does not pretend to be one. It closes a narrower gap that
    # this product's own deployment model opens: one practice login, used from a consulting room
    # and a ward workstation, held signed in all day (``session_max_concurrent`` is 10 for
    # exactly that reason). An access token on an unattended machine is therefore the normal
    # state of the system rather than an incident, and the controls around it — idle timeout,
    # absolute expiry, revocation — all measure *token* age, which is not the question. The
    # question a wide disclosure has to answer is whether the person performing it is the person
    # who signed in, and only a password re-prompt asks that.
    #
    # Fifteen minutes matches ``jwt_access_ttl_minutes``: long enough that a clinician who signs
    # in and immediately exports a record is not asked twice in a row, short enough that a
    # workstation left open over a coffee break has gone cold. Raising it weakens the control
    # smoothly rather than suddenly, which is the right shape for a knob an operator will tune.
    #
    # 0 disables the prompt entirely. Permitted for development and refused in production by
    # ``production_config_errors`` — a deployment holding real patient records must not be able
    # to switch off the one check that distinguishes the clinician from whoever walked past.
    reauthentication_max_age_minutes: int = 15

    # --- Bulk patient import ---
    # Most rows one CSV upload may carry. The import is synchronous — it validates every row,
    # checks each against the charts already on the account, and writes in one transaction — so
    # this is the bound that keeps it a request rather than a job. A practice migrating a larger
    # list splits the file; the response names the ceiling when a file exceeds it.
    patient_import_max_rows: int = 500

    # --- Reverse proxy ---
    # How many reverse proxies this deployment operates in front of the API. 0 means uvicorn
    # is exposed directly and ``X-Forwarded-For`` is ignored entirely (the safe default: the
    # header is client-settable). The reference deployment in nginx/nginx.conf is one hop, so
    # it must run with TRUSTED_PROXY_HOPS=1 — otherwise every clinician reaches the
    # application under nginx's address and every per-address control collapses onto one key.
    # See app.core.client_address.
    trusted_proxy_hops: int = 0

    # --- Brute-force protection (login) ---
    # Counted from the append-only audit log (auth_login_failed), so the control survives a
    # process restart and needs no Redis — it is deterministic and offline-capable.
    # Set login_max_failed_attempts to 0 to disable the lockout entirely.
    login_max_failed_attempts: int = 8
    login_attempt_window_minutes: int = 15
    login_lockout_minutes: int = 15
    # The address arm of the same control, budgeted separately and far more loosely. It exists
    # to slow guessing spread across many accounts from one source; it is NOT a per-account
    # control and must never be able to lock out an account that has had no failures of its
    # own. Sized for a shared egress address — a clinic, a hospital NAT, or (when
    # trusted_proxy_hops is misconfigured) the reverse proxy itself — where honest typos from
    # dozens of clinicians accumulate on one key. Set to 0 to disable the address arm alone.
    login_max_failed_attempts_per_ip: int = 60

    # --- Rate limiting (expensive clinical endpoints) ---
    # Per-account ceilings on the routes that cost an LLM call, enforced in-process by
    # app.core.rate_limit. Set any of these to 0 to disable that bucket alone, or
    # rate_limit_enabled=false to disable all of them.
    #
    # The deterministic drug-safety and *paged* chart-read routes are deliberately absent: they
    # are local, cheap and bounded by their page size, and a 429 on an allergy cross-check reads
    # to a hurried clinician as "no conflict found". See app/core/rate_limit.py. The two
    # ceilings at the end of this block are the exceptions to "reads are cheap" — one is
    # unpaged and one is unbounded in CPU.
    rate_limit_enabled: bool = True
    # A full eight-agent panel. POST ../run and GET ../stream share this budget — they run the
    # same pipeline, so a client must not be able to double its spend by alternating them.
    rate_limit_reasoning_runs_per_minute: int = 10
    # Session open + intake answers: one Triage agent call each, so cheaper than a full run but
    # not free.
    rate_limit_intake_per_minute: int = 30
    # Upload: multimodal extraction over a scan, plus Tesseract fallback.
    rate_limit_uploads_per_minute: int = 20
    # Guideline search: embeds the query and hits Qdrant. No generation, so the cheapest of
    # these — the ceiling is here to keep the vector store responsive.
    rate_limit_searches_per_minute: int = 60
    # Clinical summary: one provider call over one chart, so far cheaper than a run — but it is
    # the kind of read a ward-round list would fire once per patient per page load, and the
    # aggregate of that is what this bounds.
    rate_limit_summaries_per_minute: int = 20
    # Account creation, keyed by client IP over an hour. Without this the per-account ceilings
    # above are bypassable by signing up repeatedly; DEMO_MODE removes the credential gate but
    # not this one.
    rate_limit_signups_per_hour: int = 10
    # Password-reset requests, keyed by client address. The per-account ceiling
    # (password_reset_max_requests_per_hour) is the one that stops a clinician's inbox being
    # flooded; this one stops an unauthenticated caller walking the account list from a single
    # source. Set generously, because a shared hospital egress address carries everyone's
    # genuine resets — the same reasoning as login_max_failed_attempts_per_ip.
    rate_limit_password_resets_per_hour: int = 60
    # Step-up re-authentication attempts, per account per hour. This is the ceiling that stands
    # in for a lockout: `AuthService.reauthenticate` deliberately does not feed the login
    # lockout, because its only possible callers are people at an already signed-in keyboard and
    # a lockout there would hand a passer-by a way to deny a clinician their own record. Thirty
    # is far above a working clinician's rate (the prompt appears on chart deletion, bulk import
    # and whole-record export) and far below a useful guessing rate against a bcrypt hash.
    rate_limit_reauthentications_per_hour: int = 30
    # Bulk patient import, per account per hour. Small because this is an onboarding action, not
    # a clinical one: a practice loads its list once, and each call may create
    # `patient_import_max_rows` charts. A dry run costs the same ceiling as a real one on
    # purpose — validating a 500-row file is the same work minus the writes, and letting dry
    # runs go unmetered would leave the expensive half of the route uncapped.
    rate_limit_patient_imports_per_hour: int = 10
    # The Phase 4 validation harness, and by far the most expensive route in the system: one
    # request replays every gold-standard vignette, and each vignette costs a full eight-agent
    # panel plus its intake rounds. A single POST therefore spends several times what the
    # metered POST ../run spends, which is why leaving it unmetered made every ceiling above
    # academic — the cheapest way to drain the provider budget was the one route with no
    # ceiling on it.
    #
    # Per hour, not per minute: this is a batch job run after a prompt or corpus change, and one
    # execution takes minutes of wall clock. Twelve an hour is far beyond any human cadence
    # while still capping a runaway loop at a bounded spend.
    rate_limit_validation_runs_per_hour: int = 12
    # Whole-chart export (`GET /patients/{id}/export`), per account per hour. The one read in
    # this API that is deliberately *not* paged: it serialises every medication, lab, condition,
    # allergy, encounter and derived marker a patient has into a single FHIR bundle, and it is
    # audited as `patient_record_exported` — "the widest disclosure this API performs". Nothing
    # bounded how often it could be performed, so a stolen bearer token could pull every chart
    # in an account at the speed of HTTP, and the only trace was one audit row per chart written
    # after each one had already left. That is the DPDP-relevant failure: not that the export
    # exists, but that its rate was unmetered.
    #
    # Per hour and generous, because a clinician exporting a handful of charts for a referral in
    # one sitting is the normal use and must not hit this. Bulk retrieval is what it stops.
    rate_limit_exports_per_hour: int = 60
    # Audit-chain verification, per account per hour. Both routes it covers recompute SHA-256
    # over rows rather than reading them: `GET /patients/{id}/audit/verify` walks one chart's
    # whole trail, and `GET /regulatory/samd-dossier` walks *every row in audit_logs* via
    # verify_full_chain. `audit_logs` is append-only and never pruned, and it records every PHI
    # read as well as every write, so both grow without limit — one authenticated GET buys an
    # unbounded amount of CPU, which is the plainest amplification surface in the API. The
    # batching added earlier keeps the event loop responsive during a walk; it does not stop a
    # caller from starting a hundred of them.
    #
    # Tamper checks are run by a human investigating something, or by a scheduled job, so an
    # hourly ceiling in the tens is far above real use and well below what hurts.
    rate_limit_chain_verifications_per_hour: int = 30

    # --- Single-run claim (reasoning sessions) ---
    # How long a claim on a reasoning session stays valid before another request may take it
    # over. A rate limit counts requests over a minute; it cannot stop two of them being in
    # flight at once, and two concurrent runs write two full sets of *immutable* suggestions
    # against one session. So the claim is what makes a run exclusive, and this is its lease.
    #
    # Generous on purpose. Expiring early is the harmful direction — it permits exactly the
    # double run the claim exists to prevent — while expiring late only delays a retry after a
    # run has already died.
    #
    # This said it was sized "well above a full eight-agent panel, whose own ceiling is
    # llm_request_timeout_seconds per call", and that ceiling is wrong by an order of magnitude.
    # One LLMClient.complete_json is bounded by llm_request_timeout_seconds x (retries + 1) x
    # every provider in the fallback chain, because a provider that stops answering without
    # closing the connection is retried and then failed over rather than skipped: 30s x 3 x 3 =
    # 4.5 minutes for a single agent call, and run_reasoning makes seven in sequence plus a
    # parallel panel. A hanging upstream — not one returning 503s promptly — takes a run past
    # fifteen minutes, which is when a clinician is most likely to press Run again.
    #
    # Raising the lease is not the answer: a longer lease is a longer window in which a case
    # killed by a closing tab cannot be re-run. What makes the overrun safe is that a run
    # re-asserts its claim before publishing — see ReasoningService._finish_claimed_run — so a
    # run that lost the session discards its output instead of writing over the successor's.
    reasoning_run_lease_minutes: int = 15

    # --- Field-level encryption (patient PII at rest) ---
    # When unset, a key is derived from app_secret_key (dev convenience). Set explicitly in
    # production so rotating app_secret_key doesn't also break decryption of stored PII.
    field_encryption_key: str = ""

    # --- Uploads ---
    max_upload_bytes: int = 20 * 1024 * 1024  # 20 MB
    # Hard ceiling on ANY request body, refused with 413 before the application reads it.
    # Sits just above max_upload_bytes because a 20 MB document arrives wrapped in multipart
    # framing and must still fit; every other route is orders of magnitude below it. This is a
    # memory backstop for the unauthenticated routes, not an upload rule — the upload route
    # enforces its own 20 MB. See app.middleware.RequestBodyLimitMiddleware.
    max_request_bytes: int = 24 * 1024 * 1024  # 24 MB
    # ...and the ceiling for every route that is *not* an upload, which is all but one of them.
    #
    # A single ceiling sized for a 20 MB scan is not a ceiling for `POST /auth/login`. Before
    # this, an unauthenticated client could put 24 MB of JSON on the sign-in route and have it
    # buffered whole and parsed before the first Pydantic rule rejected it — under the global
    # limit, so nothing refused it, and repeatable as fast as the socket allows. The largest
    # legitimate non-upload body in this API is an extraction approval carrying 500 field
    # corrections of 500 characters each, which is well under 1 MB.
    max_json_request_bytes: int = 1024 * 1024  # 1 MB
    # How deeply a JSON request body may nest before it is refused unparsed.
    #
    # Nothing this API accepts is deeper than a handful of levels: the deepest is an approval's
    # `corrections[i].value`, at three. The guard exists because JSON nesting is parsed
    # recursively and the only thing that was bounding it was CPython's own recursion limit —
    # an interpreter implementation detail that surfaces as an opaque 400 and that says nothing
    # about what this application is willing to accept. See app.middleware.
    max_json_depth: int = 32
    confirmation_confidence_threshold: float = 0.85
    ocr_fallback_threshold: float = 0.50
    # How many pages of a PDF are read for their text layer, and how much text is kept.
    #
    # `max_upload_bytes` bounds the *file*, and a bounded file does not bound this work: PDF
    # content streams are Flate-compressed, so page count and extracted text length are both
    # unbounded functions of a 20 MB upload. A file that declares tens of thousands of pages —
    # or a handful of pages whose streams decompress to hundreds of megabytes of text — costs
    # minutes of CPU in `pypdf`, and then the deterministic parser walks every character of the
    # result with a set of regexes.
    #
    # That cost lands on the *default* thread pool. `_run_extraction` dispatches the pipeline
    # with `asyncio.to_thread`, which shares the pool with bcrypt behind `verify_password`,
    # document blob I/O, and upload hashing — six threads on the box this runs on. It is the
    # same saturation `agents.util.llm_executor` exists to keep a hung provider out of, arriving
    # by a different door: a few concurrent uploads of one pathological scan hold every thread,
    # and signing in stops working. Requiring a valid session first narrows who can do it; it
    # does not make an accidental one (a 4000-page fax archive) any cheaper.
    #
    # Both ceilings are far above any real clinical document. A prescription is one page, a
    # discharge summary a dozen, a bound lab panel rarely past thirty; 200 pages of extracted
    # text is roughly 600 KB of characters, which is already more than any of them carry. Past
    # the limit the read is *truncated, not refused* — the pages that were read still produce
    # entities, and the document lands in the review queue like any other partial read rather
    # than as a failure the clinician has to work around.
    max_pdf_pages_extracted: int = 200
    max_extracted_text_chars: int = 1024 * 1024  # 1 MB of characters
    # How long a document may sit in `processing` before it is treated as abandoned.
    #
    # Extraction runs inline in the upload request and is bounded well below this: a vision call
    # is `llm_request_timeout_seconds` per provider over at most three providers, Tesseract is a
    # 60s subprocess, and pypdf parsing is bounded by `max_pdf_pages_extracted` and
    # `max_extracted_text_chars` — not, as this comment used to claim, by `max_upload_bytes`,
    # which bounds the file and says nothing about what its compressed streams expand to. So
    # a document still `processing` after fifteen minutes is not slow, it is orphaned — the
    # worker was restarted, the deploy rolled, or the request was cancelled between the commit
    # that records the attempt and the commit that records its result. Nothing will ever finish
    # it, and until it is reclaimed it sits in the chart reading "being read" forever.
    extraction_stall_minutes: int = 15

    # --- Scheduling ---
    # The clinic's offset from UTC, in minutes. 330 = IST, which is the whole of this product's
    # target market; a deployment elsewhere sets it once. Used only to read a provider's
    # recorded working hours, which are stated in local time because that is how a clinic
    # states them — "Tuesdays, nine to one" does not move when the offset does.
    #
    # Everything else about an appointment is stored and compared in UTC. This is deliberately
    # not a per-provider or per-appointment field: a single-site practice has one set of
    # opening hours, and a per-row offset would have to be right on every row for the diary to
    # be readable at all.
    clinic_utc_offset_minutes: int = 330
    # Bounds on one booking. The floor stops a zero-ish slot that overlaps nothing and shows on
    # no list; the ceiling stops a typo in the end time swallowing a whole day of the diary.
    appointment_min_duration_minutes: int = 5
    appointment_max_duration_minutes: int = 480
    # How far ahead a booking may be made. Two years: annual reviews and antenatal series are
    # real, and beyond that a date is a guess that will be re-made before it arrives.
    appointment_max_days_ahead: int = 730
    # Lead times at which a reminder falls due, in minutes before the appointment. A day
    # ahead (travel, taking leave from work) and two hours ahead (setting off).
    #
    # A string rather than a list because environment variables are strings and pydantic's
    # list parsing of a bare comma-separated value is JSON-shaped; ``reminder_lead_minutes``
    # below is the parsed form. Same treatment as ``cors_origins``.
    appointment_reminder_leads: str = "1440,120"

    @property
    def reminder_lead_minutes(self) -> tuple[int, ...]:
        """Parsed, deduplicated, longest lead first; malformed entries dropped.

        Dropped rather than raised on, because this setting is read on a routine diary read and
        a typo in it must not take the scheduling routes down. A lead that cannot be parsed is
        a reminder that does not fire, which is visible in the queue; a startup crash on a
        comma is not recoverable by the clinic.
        """
        leads: set[int] = set()
        for part in self.appointment_reminder_leads.split(","):
            try:
                minutes = int(part.strip())
            except ValueError:
                continue
            if minutes > 0:
                leads.add(minutes)
        return tuple(sorted(leads, reverse=True))

    # --- Protocol templates ---
    # The length of a follow-up appointment booked by applying a template. Fifteen minutes: a
    # review, not a new-patient consultation. A clinician who needs longer moves it.
    protocol_follow_up_duration_minutes: int = 15
    # The clinic-local hour a computed follow-up lands at. The ordinary application supplies no
    # time — "review in twelve weeks" is a date — and a booking made at whatever o'clock the
    # request happened to arrive is a booking somebody has to move.
    protocol_follow_up_local_hour: int = 10

    # --- CORS ---
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


DEFAULT_SECRET_KEY = "dev-insecure-secret-change-me"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://aether:aether@localhost:5432/aether_clinician"
DEFAULT_MINIO_CREDENTIAL = "minioadmin"


def _is_malformed_origin(origin: str) -> bool:
    """True for an entry a browser's ``Origin`` header can never equal.

    An Origin is exactly ``scheme://host[:port]`` — no path, no trailing slash, no query.
    Starlette compares the header to these strings literally, so ``https://app.example.com/``
    silently matches nothing. ``*`` and ``null`` are reported by their own dedicated checks,
    which say something more useful than "malformed".
    """
    if origin.lower() in ("*", "null"):
        return False
    scheme, separator, rest = origin.partition("://")
    if not separator or scheme not in ("http", "https") or not rest:
        return True
    return any(character in rest for character in "/?#")


def production_config_errors(cfg: "Settings") -> list[str]:
    """Configuration that is safe in dev but unacceptable in production.

    Returns a list of human-readable problems (empty when the config is fit to serve real
    patient data). Kept as a pure function so it is unit-testable against a constructed
    Settings without touching the process environment.

    Note what is *not* here: DEMO_MODE. Demo mode removes the credential gate but keeps auth,
    audit logging, and every safety check — it is a deliberate product mode, not a security
    downgrade — so it is allowed in production as long as the banner stays visible.
    """
    problems: list[str] = []
    if not cfg.is_production:
        return problems

    if cfg.app_secret_key == DEFAULT_SECRET_KEY or len(cfg.app_secret_key) < 32:
        problems.append(
            "APP_SECRET_KEY is the built-in default or shorter than 32 characters — "
            "generate one with `openssl rand -hex 32`."
        )
    if cfg.app_debug:
        problems.append("APP_DEBUG must be false in production (it leaks internals).")
    if "*" in cfg.cors_origin_list:
        problems.append(
            "CORS_ORIGINS must name explicit origins in production — a wildcard combined "
            "with credentialed requests exposes the API to any site."
        )
    if any(o.startswith("http://") and "localhost" not in o for o in cfg.cors_origin_list):
        problems.append("CORS_ORIGINS contains a non-local http:// origin; use https://.")
    if any(o.lower() == "null" for o in cfg.cors_origin_list):
        problems.append(
            "CORS_ORIGINS contains the `null` origin — sandboxed iframes and file:// pages "
            "send `Origin: null`, so allowing it grants any local HTML file credentialed "
            "access to patient data."
        )
    malformed = [o for o in cfg.cors_origin_list if _is_malformed_origin(o)]
    if malformed:
        problems.append(
            "CORS_ORIGINS entries must be bare scheme://host[:port] with no path or trailing "
            f"slash (browsers send the Origin header in that form): {', '.join(malformed)}. "
            "As written these match nothing, which looks like a broken frontend and invites "
            "someone to 'fix' it with a wildcard."
        )
    if not cfg.field_encryption_key:
        problems.append(
            "FIELD_ENCRYPTION_KEY must be set explicitly in production so rotating "
            "APP_SECRET_KEY does not make stored patient PII undecryptable."
        )
    if cfg.storage_backend == "s3":
        default_s3_keys = [
            name
            for name, value in (
                ("S3_ACCESS_KEY", cfg.s3_access_key),
                ("S3_SECRET_KEY", cfg.s3_secret_key),
            )
            if value == DEFAULT_MINIO_CREDENTIAL
        ]
        if default_s3_keys:
            problems.append(
                f"{' and '.join(default_s3_keys)} still the MinIO development default "
                f"({DEFAULT_MINIO_CREDENTIAL!r}) — anyone who can reach the object store can "
                "read every uploaded prescription and lab report."
            )
    if cfg.password_reset_delivery == "response":
        problems.append(
            "PASSWORD_RESET_DELIVERY=response hands the reset token straight back to whoever "
            "POSTed the email address, so anyone who knows a clinician's address can take "
            "their account. Set it to `log` (an operator relays the token out of band) until "
            "a delivery integration exists."
        )
    problems.extend(_llm_demo_fallback_errors(cfg))
    problems.extend(_reauthentication_errors(cfg))
    problems.extend(_database_url_errors(cfg))
    return problems


def _reauthentication_errors(cfg: "Settings") -> list[str]:
    """Reject a production deployment with the step-up password prompt switched off.

    ``REAUTHENTICATION_MAX_AGE_MINUTES=0`` removes the prompt from chart deletion, bulk import
    and whole-record export. In development that is a convenience; in production it means an
    access token found on an unlocked workstation can delete a chart or walk out with every
    record on it, and nothing in the system ever asks whether the person holding the token is
    the clinician who signed in.

    Refused rather than clamped, for the reason every gate in this function is refused: an
    operator who set this deliberately should find out at startup, not discover months later
    from an audit trail that the control they believed was on had been off the whole time.
    """
    if not cfg.is_production or cfg.reauthentication_max_age_minutes > 0:
        return []
    return [
        "REAUTHENTICATION_MAX_AGE_MINUTES must be greater than 0 in production. At 0 the "
        "step-up password prompt is off, so deleting a chart, importing charts in bulk and "
        "exporting a whole patient record need nothing but a live access token — which on this "
        "product's shared-workstation deployment is the normal resting state of the system. "
        "Set it to 15 (matching the access-token lifetime) unless you have a reason not to."
    ]


def _llm_demo_fallback_errors(cfg: "Settings") -> list[str]:
    """Reject the simulated-reasoning safety net in production.

    ``LLM_DEMO_FALLBACK`` makes the reasoning engine return realistic ``[DEMO MODE]`` sample
    differentials, dissent, and guideline citations when no provider can be reached, so the
    product is always demonstrable. In production that turns an outage into something far worse
    than an outage: a clinician is shown fabricated clinical reasoning about a real patient,
    and the only signal is a field on ``/health`` that nobody reads mid-consultation.

    Production must degrade to the deterministic offline path (deterministic safety checks,
    "AI reasoning paused" indicator) instead — a visibly absent answer, never an invented one.
    """
    if not cfg.is_production or not cfg.llm_demo_fallback:
        return []
    return [
        "LLM_DEMO_FALLBACK must be false in production. When enabled it serves SIMULATED "
        "'[DEMO MODE]' clinical reasoning — fabricated differentials and citations about a "
        "real patient — whenever no LLM provider can be reached. Set LLM_DEMO_FALLBACK=false "
        "so unreachable providers degrade to the deterministic offline path instead."
    ]


def _database_url_errors(cfg: "Settings") -> list[str]:
    """Reject a DATABASE_URL that is the dev default or cannot keep the audit-log guarantees."""
    problems: list[str] = []
    if cfg.database_url == DEFAULT_DATABASE_URL:
        problems.append(
            "DATABASE_URL is the built-in development default (user/password aether@localhost) "
            "— set it to the production PostgreSQL instance, whose credentials are not in the "
            "source tree."
        )
    elif not cfg.database_url.startswith("postgresql"):
        problems.append(
            "DATABASE_URL must point at PostgreSQL in production "
            f"(got scheme {cfg.database_url.split('://', 1)[0]!r}). The patient graph and the "
            "append-only audit log rely on PostgreSQL semantics; SQLite in particular cannot "
            "serve concurrent clinicians and is not a supported store for patient data."
        )
    return problems


class InsecureProductionConfigError(RuntimeError):
    """Raised at startup when production is configured with development-grade secrets."""


def assert_production_config(cfg: "Settings | None" = None) -> None:
    """Fail fast when running as production with an unsafe configuration.

    Called from the app lifespan so a misconfigured deployment refuses to serve rather than
    silently handling patient data with a known signing key.
    """
    problems = production_config_errors(cfg or settings)
    if problems:
        raise InsecureProductionConfigError(
            "Refusing to start in production with an insecure configuration:\n  - "
            + "\n  - ".join(problems)
        )


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()


settings = get_settings()
