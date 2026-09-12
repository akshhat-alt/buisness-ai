"""Tests for Phase 9's at-rest secret encryption: the vault's own
encrypt/decrypt contract, and TenantRegistry's integration of it —
including the backward-compatible read of a legacy plaintext row and the
"no key configured" passthrough that keeps every pre-Phase-9 deployment
working unchanged.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from business_ai.secrets_vault import decrypt_secret, encrypt_secret
from business_ai.tenant import TenantConfig, TenantRegistry

KEY = "a-real-32-byte-class-secret-key-value"
OTHER_KEY = "a-completely-different-rotated-key!!"


# ------------------------------------------------------------------ vault unit tests


def test_encrypt_then_decrypt_round_trips():
    token = encrypt_secret("super-secret-token", key=KEY)
    assert token != "super-secret-token"
    assert token.startswith("enc:v1:")
    assert decrypt_secret(token, key=KEY) == "super-secret-token"


def test_encrypt_is_a_noop_without_a_key():
    assert encrypt_secret("plain-value", key=None) == "plain-value"


def test_decrypt_returns_legacy_plaintext_unchanged():
    """A value with no enc:v1: prefix is legacy plaintext (or a value
    written before a key was ever configured) — never treated as
    ciphertext, never raises."""
    assert decrypt_secret("legacy-plaintext-token", key=KEY) == "legacy-plaintext-token"


def test_decrypt_without_a_key_returns_stored_value_unchanged():
    token = encrypt_secret("secret", key=KEY)
    assert decrypt_secret(token, key=None) == token


def test_encrypt_none_and_empty_are_unchanged():
    assert encrypt_secret(None, key=KEY) is None
    assert encrypt_secret("", key=KEY) == ""
    assert decrypt_secret(None, key=KEY) is None


def test_decrypt_under_wrong_key_fails_soft_not_raises():
    token = encrypt_secret("secret", key=KEY)
    # Wrong key can't recover the plaintext — must return SOMETHING
    # rather than raise and take down the caller (a tenant lookup).
    result = decrypt_secret(token, key=OTHER_KEY)
    assert result == token  # unrecoverable -> returned unchanged, not silently corrupted


def test_double_encryption_is_a_noop():
    token = encrypt_secret("secret", key=KEY)
    assert encrypt_secret(token, key=KEY) == token


# ------------------------------------------------------------------ TenantRegistry integration


def _raw_config_json(db_path: Path, tenant_id: str) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT config_json FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
        return row[0]
    finally:
        conn.close()


def test_secrets_are_encrypted_on_disk_when_key_configured(tmp_path):
    registry = TenantRegistry(tmp_path / "tenants.db", secret_encryption_key=KEY)
    registry.register(
        TenantConfig(
            tenant_id="salon-a", business_name="Salon A", owner_email="a@x.com",
            whatsapp_access_token="wa-real-token-123", razorpay_key_secret="rzp-real-secret-456",
        )
    )
    raw = _raw_config_json(tmp_path / "tenants.db", "salon-a")
    assert "wa-real-token-123" not in raw
    assert "rzp-real-secret-456" not in raw
    assert "enc:v1:" in raw

    # But the API contract is unchanged: callers always see plaintext.
    config = registry.get_config("salon-a")
    assert config.whatsapp_access_token == "wa-real-token-123"
    assert config.razorpay_key_secret == "rzp-real-secret-456"


def test_secrets_stay_plaintext_on_disk_without_a_key(tmp_path):
    """Zero behavior change for a deployment that hasn't set
    SECRET_ENCRYPTION_KEY — the exact backward-compatibility bar."""
    registry = TenantRegistry(tmp_path / "tenants.db")
    registry.register(
        TenantConfig(
            tenant_id="salon-a", business_name="Salon A", owner_email="a@x.com",
            whatsapp_access_token="wa-real-token-123",
        )
    )
    raw = _raw_config_json(tmp_path / "tenants.db", "salon-a")
    assert "wa-real-token-123" in raw

    config = registry.get_config("salon-a")
    assert config.whatsapp_access_token == "wa-real-token-123"


def test_reading_a_legacy_plaintext_row_still_works_after_key_is_configured(tmp_path):
    """The actual migration story: a tenant registered BEFORE
    SECRET_ENCRYPTION_KEY was set must keep working the moment the
    operator turns encryption on, with no manual data migration step."""
    db_path = tmp_path / "tenants.db"
    registry_before = TenantRegistry(db_path)  # no key yet
    registry_before.register(
        TenantConfig(
            tenant_id="salon-a", business_name="Salon A", owner_email="a@x.com",
            whatsapp_access_token="wa-legacy-plaintext-token",
        )
    )

    registry_after = TenantRegistry(db_path, secret_encryption_key=KEY)  # operator turns on encryption
    config = registry_after.get_config("salon-a")
    assert config.whatsapp_access_token == "wa-legacy-plaintext-token"

    # And the NEXT write re-saves it encrypted (lazy migration) —
    # without anyone having to run a one-off script for this tenant.
    registry_after.update_config("salon-a", assistant_name="Updated Name")
    raw = _raw_config_json(db_path, "salon-a")
    assert "wa-legacy-plaintext-token" not in raw
    assert registry_after.get_config("salon-a").whatsapp_access_token == "wa-legacy-plaintext-token"


def test_update_config_does_not_mutate_caller_held_plaintext_object(tmp_path):
    """register() must encrypt onto a COPY for storage, never mutate the
    TenantConfig instance the caller still holds and may keep using."""
    registry = TenantRegistry(tmp_path / "tenants.db", secret_encryption_key=KEY)
    config = TenantConfig(
        tenant_id="salon-a", business_name="Salon A", owner_email="a@x.com",
        whatsapp_access_token="wa-real-token-123",
    )
    returned = registry.register(config)
    assert returned.whatsapp_access_token == "wa-real-token-123"
    assert config.whatsapp_access_token == "wa-real-token-123"
