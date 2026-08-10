"""Production configuration guard — the app must refuse to serve with dev-grade secrets."""

from __future__ import annotations

import pytest

from app.config import (
    DEFAULT_SECRET_KEY,
    InsecureProductionConfigError,
    Settings,
    assert_production_config,
    production_config_errors,
)

_SAFE_KEY = "a" * 64


def _prod(**overrides) -> Settings:
    base = {
        "app_env": "production",
        "app_secret_key": _SAFE_KEY,
        "app_debug": False,
        "cors_origins": "https://documedic.aiknol.com",
        "field_encryption_key": "k" * 44,
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
        for p in production_config_errors(_prod(storage_backend="s3", s3_secret_key="minioadmin"))
    )


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
