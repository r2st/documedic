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
    # instead of raising, so the product is always demonstrable. Default on.
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
    if not cfg.field_encryption_key:
        problems.append(
            "FIELD_ENCRYPTION_KEY must be set explicitly in production so rotating "
            "APP_SECRET_KEY does not make stored patient PII undecryptable."
        )
    if cfg.storage_backend == "s3" and cfg.s3_secret_key == "minioadmin":
        problems.append("S3_SECRET_KEY is still the MinIO development default.")
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
