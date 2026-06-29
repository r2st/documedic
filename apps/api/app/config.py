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


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()


settings = get_settings()
