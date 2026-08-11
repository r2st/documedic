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

    # --- Brute-force protection (login) ---
    # Counted from the append-only audit log (auth_login_failed), so the control survives a
    # process restart and needs no Redis — it is deterministic and offline-capable.
    # Set login_max_failed_attempts to 0 to disable the lockout entirely.
    login_max_failed_attempts: int = 8
    login_attempt_window_minutes: int = 15
    login_lockout_minutes: int = 15

    # --- Rate limiting (expensive clinical endpoints) ---
    # Per-account ceilings on the routes that cost an LLM call, enforced in-process by
    # app.core.rate_limit. Set any of these to 0 to disable that bucket alone, or
    # rate_limit_enabled=false to disable all of them.
    #
    # The deterministic drug-safety and chart-read routes are deliberately absent: they are
    # local and cheap, and a 429 on an allergy cross-check reads to a hurried clinician as "no
    # conflict found". See app/core/rate_limit.py.
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
    # Account creation, keyed by client IP over an hour. Without this the per-account ceilings
    # above are bypassable by signing up repeatedly; DEMO_MODE removes the credential gate but
    # not this one.
    rate_limit_signups_per_hour: int = 10

    # --- Field-level encryption (patient PII at rest) ---
    # When unset, a key is derived from app_secret_key (dev convenience). Set explicitly in
    # production so rotating app_secret_key doesn't also break decryption of stored PII.
    field_encryption_key: str = ""

    # --- Uploads ---
    max_upload_bytes: int = 20 * 1024 * 1024  # 20 MB
    confirmation_confidence_threshold: float = 0.85
    ocr_fallback_threshold: float = 0.50

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
    problems.extend(_llm_demo_fallback_errors(cfg))
    problems.extend(_database_url_errors(cfg))
    return problems


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
