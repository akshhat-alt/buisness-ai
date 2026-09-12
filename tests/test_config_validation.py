"""Tests for config.validate_environment's Phase 9 addition:
SECRET_ENCRYPTION_KEY is flagged when missing/weak, but ALWAYS as a
warning — never an error that blocks startup, even in production —
since a missing key just means secrets stay plaintext (backward
compatible), unlike JWT_SECRET_KEY/ADMIN_SECRET which break the app
outright if missing.
"""

from __future__ import annotations

import dataclasses

from business_ai.config import load_settings, validate_environment


def _settings(**overrides):
    base = load_settings()
    return dataclasses.replace(base, jwt_secret_key="a-perfectly-fine-32-byte-secret", admin_secret="a-perfectly-fine-admin-secret", **overrides)


def test_missing_secret_encryption_key_is_flagged():
    settings = _settings(secret_encryption_key=None)
    issues = validate_environment(settings)
    codes = [i.code for i in issues]
    assert "WEAK_SECRET_ENCRYPTION_KEY" in codes


def test_missing_secret_encryption_key_is_never_an_error_even_in_production():
    settings = _settings(secret_encryption_key=None, app_env="production")
    issues = validate_environment(settings)
    issue = next(i for i in issues if i.code == "WEAK_SECRET_ENCRYPTION_KEY")
    assert issue.level == "warning"


def test_configured_secret_encryption_key_is_not_flagged():
    settings = _settings(secret_encryption_key="a-real-32-byte-class-secret-key-value")
    issues = validate_environment(settings)
    codes = [i.code for i in issues]
    assert "WEAK_SECRET_ENCRYPTION_KEY" not in codes


def test_short_secret_encryption_key_is_flagged():
    settings = _settings(secret_encryption_key="short")
    issues = validate_environment(settings)
    codes = [i.code for i in issues]
    assert "WEAK_SECRET_ENCRYPTION_KEY" in codes
