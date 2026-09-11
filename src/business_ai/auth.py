"""Authentication: JWT principals + a persistent password-based user store.

Adapted from Shri AI's runtime/auth.py + runtime/user_store.py (both proven,
generic, not spiritual-content-specific). Trimmed for Business AI's 3-role
model and dropped the password-reset-token flow — no email delivery exists
yet, so a reset token with nowhere to send it is a dead end (this exact gap
was flagged in Shri AI; not repeating it here until email sending exists).
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

import jwt
from pydantic import BaseModel, Field

from business_ai.config import KNOWN_INSECURE_SECRETS, Settings

ROLES = frozenset({"platform_admin", "owner", "manager", "staff"})


class AuthenticationError(Exception):
    """Base exception for all authentication failures."""


@dataclass(frozen=True)
class Principal:
    principal_id: str
    tenant_id: str | None
    role: str
    is_authenticated: bool = True

    @staticmethod
    def platform_admin(principal_id: str) -> "Principal":
        return Principal(principal_id=principal_id, tenant_id=None, role="platform_admin")

    @staticmethod
    def owner(principal_id: str, tenant_id: str) -> "Principal":
        return Principal(principal_id=principal_id, tenant_id=tenant_id, role="owner")

    @staticmethod
    def manager(principal_id: str, tenant_id: str) -> "Principal":
        return Principal(principal_id=principal_id, tenant_id=tenant_id, role="manager")

    @staticmethod
    def staff(principal_id: str, tenant_id: str) -> "Principal":
        return Principal(principal_id=principal_id, tenant_id=tenant_id, role="staff")


# ==============================================================================
# JWT issuance & verification
# ==============================================================================

_DEV_FALLBACK_SECRET = "business_ai_dev_only_secret_never_use_in_production_32b"


def _resolve_jwt_secret(settings: Settings) -> str:
    enforce = settings.auth_required or settings.app_env == "production"
    secret = settings.jwt_secret_key

    if secret:
        if enforce and (secret.lower() in KNOWN_INSECURE_SECRETS or len(secret) < 16):
            raise AuthenticationError("JWT_SECRET_KEY is insecure or too short under enforced auth.")
        return secret

    if enforce:
        raise AuthenticationError("JWT_SECRET_KEY is missing. Required when AUTH_REQUIRED/production mode is set.")

    return _DEV_FALLBACK_SECRET


def create_access_token(principal: Principal, settings: Settings, *, expires_delta_seconds: int = 86400) -> str:
    now = int(time.time())
    secret = _resolve_jwt_secret(settings)
    payload = {
        "sub": principal.principal_id,
        "tenant_id": principal.tenant_id,
        "role": principal.role,
        "iat": now,
        "exp": now + expires_delta_seconds,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_and_verify_token(token: str, settings: Settings) -> Principal:
    if not token or not isinstance(token, str):
        raise AuthenticationError("Bearer token is empty or missing.")

    secret = _resolve_jwt_secret(settings)
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"verify_exp": True, "verify_iss": True, "verify_aud": True},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError(f"Invalid or expired token: {exc}") from exc

    sub = payload.get("sub")
    role = payload.get("role")
    tenant_id = payload.get("tenant_id")

    if not sub or not role:
        raise AuthenticationError("Token missing required 'sub' or 'role' claim.")
    clean_role = str(role).lower().strip()
    if clean_role not in ROLES:
        raise AuthenticationError(f"Invalid role claim '{role}' in token.")
    if clean_role != "platform_admin" and not tenant_id:
        raise AuthenticationError(f"Role '{clean_role}' requires a tenant_id claim.")

    return Principal(principal_id=str(sub), tenant_id=(str(tenant_id) if tenant_id else None), role=clean_role)


def resolve_principal(authorization_header: str | None, settings: Settings) -> Principal | None:
    """Resolve the caller's Principal from a Bearer token. Returns None if
    no token was supplied (an anonymous/unauthenticated caller) — callers
    must decide whether that's acceptable for the specific route."""
    if not authorization_header or not authorization_header.strip():
        return None
    parts = authorization_header.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthenticationError("Authorization header must use format 'Bearer <token>'.")
    return decode_and_verify_token(parts[1], settings)


# ==============================================================================
# Persistent user store (PBKDF2-hashed passwords)
# ==============================================================================


class UserRecord(BaseModel):
    user_id: str
    email: str
    name: str
    business_name: str
    password_hash: str
    salt: str
    role: str = "owner"
    tenant_id: str
    created_at: str


class UserStore:
    """Thread-safe SQLite store for business owner/staff accounts."""

    def __init__(self, db_path: Path | str = "data/users.db") -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    business_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    role TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id)")
            conn.commit()

    @staticmethod
    def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
        if not salt:
            salt = secrets.token_hex(16)
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
        return key.hex(), salt

    @staticmethod
    def verify_password(password: str, password_hash: str, salt: str) -> bool:
        computed_hash, _ = UserStore.hash_password(password, salt)
        return secrets.compare_digest(computed_hash, password_hash)

    def create_user(
        self,
        *,
        email: str,
        password: str,
        name: str,
        business_name: str,
        tenant_id: str,
        role: str = "owner",
    ) -> UserRecord:
        clean_email = email.strip().lower()
        if not clean_email or "@" not in clean_email:
            raise ValueError("Invalid email address.")
        if len(password) < 6:
            raise ValueError("Password must be at least 6 characters.")
        if role not in ROLES:
            raise ValueError(f"Invalid role '{role}'.")

        p_hash, salt = self.hash_password(password)
        user_id = f"user_{secrets.token_hex(8)}"
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        user = UserRecord(
            user_id=user_id,
            email=clean_email,
            name=name.strip(),
            business_name=business_name.strip(),
            password_hash=p_hash,
            salt=salt,
            role=role,
            tenant_id=tenant_id,
            created_at=now_iso,
        )

        with self._lock, self._db() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, email, name, business_name, password_hash,
                        salt, role, tenant_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user.user_id, user.email, user.name, user.business_name,
                        user.password_hash, user.salt, user.role, user.tenant_id, user.created_at,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError(f"Account with email '{clean_email}' already exists.") from None

        return user

    def get_user_by_email(self, email: str) -> UserRecord | None:
        clean_email = email.strip().lower()
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (clean_email,)).fetchone()
            if not row:
                return None
            return UserRecord(**dict(row))


_USER_STORES: dict[Path, UserStore] = {}


def get_global_user_store(data_root: Path | str = "data") -> UserStore:
    resolved_path = Path(data_root).resolve() / "users.db"
    if resolved_path not in _USER_STORES:
        _USER_STORES[resolved_path] = UserStore(resolved_path)
    return _USER_STORES[resolved_path]
