"""Production configuration guard — the app must refuse to serve with dev-grade secrets."""

from __future__ import annotations

import pytest

from app.config import (
    DEFAULT_DATABASE_URL,
    DEFAULT_SECRET_KEY,
    InsecureProductionConfigError,
    Settings,
    assert_production_config,
    production_config_errors,
)

_SAFE_KEY = "a" * 64
_SAFE_DATABASE_URL = "postgresql+asyncpg://aether_prod:s3cret@db.internal:5432/aether"


def _prod(**overrides) -> Settings:
    base = {
        "app_env": "production",
        "app_secret_key": _SAFE_KEY,
        "app_debug": False,
        "cors_origins": "https://documedic.doaide.com",
        "field_encryption_key": "k" * 44,
        "llm_demo_fallback": False,
        "database_url": _SAFE_DATABASE_URL,
    }
    base.update(overrides)
    return Settings(**base)


def test_development_config_is_never_flagged():
    dev = Settings(app_env="development", app_secret_key=DEFAULT_SECRET_KEY, app_debug=True)
    assert production_config_errors(dev) == []


def test_fully_configured_production_passes():
    assert production_config_errors(_prod()) == []
    assert_production_config(_prod())  # does not raise


def test_default_secret_key_is_rejected():
    problems = production_config_errors(_prod(app_secret_key=DEFAULT_SECRET_KEY))
    assert any("APP_SECRET_KEY" in p for p in problems)


def test_short_secret_key_is_rejected():
    problems = production_config_errors(_prod(app_secret_key="tooshort"))
    assert any("APP_SECRET_KEY" in p for p in problems)


def test_debug_mode_is_rejected():
    problems = production_config_errors(_prod(app_debug=True))
    assert any("APP_DEBUG" in p for p in problems)


def test_wildcard_cors_is_rejected():
    problems = production_config_errors(_prod(cors_origins="*"))
    assert any("CORS_ORIGINS" in p and "wildcard" in p for p in problems)


def test_plaintext_http_origin_is_rejected():
    problems = production_config_errors(_prod(cors_origins="http://clinic.example.com"))
    assert any("http://" in p for p in problems)


def test_localhost_http_origin_is_allowed():
    """A localhost origin is a tunnel/dev-proxy case, not an exposure."""
    problems = production_config_errors(_prod(cors_origins="http://localhost:3000"))
    assert not any("http://" in p for p in problems)


def test_null_cors_origin_is_rejected():
    """`Origin: null` is what sandboxed iframes and file:// pages send.

    Allowing it is close to a wildcard for the attacker who can get the clinician to open
    one local HTML file, and it reads as an innocuous-looking entry in an env var.
    """
    problems = production_config_errors(_prod(cors_origins="https://documedic.doaide.com,null"))
    assert any("`null` origin" in p for p in problems)


@pytest.mark.parametrize(
    "origin",
    [
        "https://documedic.doaide.com/",  # trailing slash
        "https://documedic.doaide.com/app",  # path
        "https://documedic.doaide.com?x=1",  # query
        "documedic.doaide.com",  # no scheme
        "HTTPS://documedic.doaide.com",  # uppercase scheme
        "ftp://documedic.doaide.com",  # not an http(s) origin
        "https://",  # no host
    ],
)
def test_cors_origins_that_can_never_match_a_browser_origin_are_rejected(origin):
    """Starlette compares the Origin header to these strings literally.

    Anything that is not exactly ``scheme://host[:port]`` matches nothing, so the frontend
    just breaks — and the usual field fix for "CORS is broken" is a wildcard, which is the
    outcome this guard exists to prevent.
    """
    problems = production_config_errors(_prod(cors_origins=origin))
    assert any("no path or trailing slash" in p for p in problems), problems
    assert any(origin in p for p in problems), problems


@pytest.mark.parametrize(
    "origin",
    [
        "https://documedic.doaide.com",
        "https://documedic.doaide.com:8443",
        "http://localhost:3000",
        "https://a.example.com,https://b.example.com",
    ],
)
def test_well_formed_origins_are_not_flagged_as_malformed(origin):
    problems = production_config_errors(_prod(cors_origins=origin))
    assert not any("no path or trailing slash" in p for p in problems), problems


def test_wildcard_is_reported_as_a_wildcard_not_as_malformed():
    """`*` and `null` have their own, more useful messages; don't double-report them."""
    for value in ("*", "null"):
        problems = production_config_errors(_prod(cors_origins=value))
        assert not any("no path or trailing slash" in p for p in problems), problems


def test_missing_field_encryption_key_is_rejected():
    problems = production_config_errors(_prod(field_encryption_key=""))
    assert any("FIELD_ENCRYPTION_KEY" in p for p in problems)


def test_default_minio_secret_rejected_only_for_s3_backend():
    assert not any(
        "S3_SECRET_KEY" in p
        for p in production_config_errors(
            _prod(storage_backend="local", s3_secret_key="minioadmin")
        )
    )
    assert any(
        "S3_SECRET_KEY" in p
        for p in production_config_errors(
            _prod(storage_backend="s3", s3_access_key="prod-key", s3_secret_key="minioadmin")
        )
    )


def test_default_minio_access_key_is_rejected():
    """The access key is half the credential; a default one is as reachable as a default secret."""
    problems = production_config_errors(
        _prod(storage_backend="s3", s3_access_key="minioadmin", s3_secret_key="prod-secret")
    )
    assert any("S3_ACCESS_KEY" in p for p in problems), problems


def test_both_default_minio_credentials_reported_in_one_problem():
    problems = production_config_errors(
        _prod(storage_backend="s3", s3_access_key="minioadmin", s3_secret_key="minioadmin")
    )
    named = [p for p in problems if "S3_ACCESS_KEY" in p and "S3_SECRET_KEY" in p]
    assert len(named) == 1, problems


def test_custom_s3_credentials_are_not_flagged():
    problems = production_config_errors(
        _prod(storage_backend="s3", s3_access_key="prod-key", s3_secret_key="prod-secret")
    )
    assert problems == []


# ------------------------------------------------------------- simulated clinical reasoning


def test_llm_demo_fallback_is_rejected_in_production():
    """Serving fabricated '[DEMO MODE]' reasoning about a real patient is worse than an outage."""
    problems = production_config_errors(_prod(llm_demo_fallback=True))
    assert any("LLM_DEMO_FALLBACK" in p for p in problems), problems
    assert any("SIMULATED" in p for p in problems), problems


def test_llm_demo_fallback_is_allowed_outside_production():
    for env in ("development", "staging", "test"):
        cfg = Settings(app_env=env, llm_demo_fallback=True)
        assert production_config_errors(cfg) == []


def test_llm_demo_fallback_is_flagged_even_with_a_provider_key():
    """A key today does not stop the net firing the moment that provider is unreachable."""
    problems = production_config_errors(_prod(llm_demo_fallback=True, openai_api_key="sk-live"))
    assert any("LLM_DEMO_FALLBACK" in p for p in problems), problems


# ------------------------------------------------------------------------------- database


def test_default_database_url_is_rejected():
    problems = production_config_errors(_prod(database_url=DEFAULT_DATABASE_URL))
    assert any("DATABASE_URL" in p and "development default" in p for p in problems), problems


def test_sqlite_database_url_is_rejected_in_production():
    problems = production_config_errors(_prod(database_url="sqlite+aiosqlite:///./aether.db"))
    assert any("DATABASE_URL" in p and "PostgreSQL" in p for p in problems), problems
    assert any("sqlite+aiosqlite" in p for p in problems), problems


def test_non_default_postgres_url_is_accepted():
    problems = production_config_errors(_prod(database_url=_SAFE_DATABASE_URL))
    assert problems == []


def test_default_database_url_reported_once_not_also_as_wrong_scheme():
    """The default URL *is* PostgreSQL — report the leaked credential, not a bogus scheme error."""
    problems = production_config_errors(_prod(database_url=DEFAULT_DATABASE_URL))
    assert len([p for p in problems if "DATABASE_URL" in p]) == 1, problems


def test_demo_mode_is_allowed_in_production():
    """Demo mode drops the credential gate but keeps auth, audit, and safety checks —
    it is a product mode, not a security downgrade (CLAUDE.md common pitfall #10)."""
    assert production_config_errors(_prod(demo_mode=True)) == []


def test_assert_raises_and_names_every_problem():
    bad = _prod(app_secret_key=DEFAULT_SECRET_KEY, app_debug=True, cors_origins="*")
    with pytest.raises(InsecureProductionConfigError) as exc:
        assert_production_config(bad)
    message = str(exc.value)
    assert "APP_SECRET_KEY" in message
    assert "APP_DEBUG" in message
    assert "CORS_ORIGINS" in message
