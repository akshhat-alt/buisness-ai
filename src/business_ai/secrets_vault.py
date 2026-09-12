"""Field-level encryption for secrets stored at rest (Phase 9) — today,
a tenant's WhatsApp access token and Razorpay key secret in `tenants.db`.
Both were plaintext since V1 (a known, explicitly-documented limitation);
this closes it without a big-bang migration.

Design:
  - Encryption is keyed by SECRET_ENCRYPTION_KEY (any string; hashed down
    to a valid Fernet key so operators don't need to generate a
    special-format value — the same "any string works" ergonomics as
    JWT_SECRET_KEY/ADMIN_SECRET).
  - If the key isn't configured, `encrypt_secret` is a no-op passthrough
    (plaintext) — existing deployments that haven't set the new env var
    keep working exactly as before. `validate_environment` (config.py)
    treats this as a production-fail-closed, dev-warning issue, the same
    tier as the other secrets there.
  - Encrypted values are prefixed `enc:v1:` so `decrypt_secret` can tell
    an already-encrypted value from legacy (or passthrough-mode)
    plaintext apart, and never double-encrypts or mis-decrypts either.
  - Migration is lazy by default: a tenant's secret fields get encrypted
    the next time that tenant's config is written (any update). For an
    immediate, one-time pass over every existing tenant, see
    scripts/rotate_secrets.py — the same tool doubles as key-rotation
    support (decrypt under the old key, re-encrypt under the current
    one).
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_ENC_PREFIX = "enc:v1:"


def _derive_fernet_key(raw_key: str) -> bytes:
    digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet_for(raw_key: str) -> Fernet:
    return Fernet(_derive_fernet_key(raw_key))


def encrypt_secret(plaintext: str | None, *, key: str | None) -> str | None:
    """Returns None/empty unchanged. Returns plaintext unchanged (never
    double-encrypted) if it's already an enc:v1: value or no key is
    configured. Otherwise returns `enc:v1:<fernet token>`."""
    if not plaintext:
        return plaintext
    if plaintext.startswith(_ENC_PREFIX):
        return plaintext
    if not key:
        return plaintext
    token = _fernet_for(key).encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def decrypt_secret(value: str | None, *, key: str | None) -> str | None:
    """Returns non-enc:v1: values unchanged (legacy plaintext, or
    passthrough when no key is configured — both are just "this value
    isn't encrypted"). Never raises on a value it can't decrypt with the
    CURRENT key; instead returns it unchanged and lets the caller keep
    working with what's effectively an opaque string, rather than a hard
    crash on every tenant lookup because one secret was encrypted under a
    since-rotated-away key that was never re-encrypted."""
    if not value or not value.startswith(_ENC_PREFIX):
        return value
    if not key:
        return value
    token = value[len(_ENC_PREFIX):]
    try:
        return _fernet_for(key).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.warning("Could not decrypt a stored secret under the current SECRET_ENCRYPTION_KEY — returning it unchanged (likely encrypted under a key that has since been rotated away without a re-encryption pass; see scripts/rotate_secrets.py).")
        return value
