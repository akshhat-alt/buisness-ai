"""Runtime configuration for Business AI, loaded from environment variables.

Deliberately a single flat settings object rather than Shri AI's several
nested per-domain configs — Business AI has one runtime concern (auth
secrets, quotas, model names), not distributed-infra/transcription/embedding
sub-configs, so one dataclass is the smallest correct shape today. If a
genuinely separate concern appears later (e.g. a payments config), add a
new dataclass then rather than pre-splitting now.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Auth
    jwt_secret_key: str | None
    jwt_issuer: str
    jwt_audience: str
    admin_secret: str | None
    auth_required: bool
    app_env: str

    # CORS
    allowed_origins: list[str]

    # AI models
    llm_model: str
    embedding_model: str
    max_output_tokens: int

    # Usage protection (mirrors Shri AI's proven pattern)
    active_tenant_quota: int
    requests_per_minute: int
    max_concurrent_requests: int
    max_query_length: int

    # WhatsApp handoff (wa.me click-to-chat fallback link)
    default_whatsapp_number: str | None

    # WhatsApp Business Cloud API (platform-level: one Meta App shared by
    # every tenant's own WhatsApp Business number). Each tenant brings
    # their own phone_number_id + access token (see TenantConfig) — this
    # app never holds a tenant's WhatsApp credentials as platform secrets.
    whatsapp_app_secret: str | None
    whatsapp_verify_token: str | None
    whatsapp_api_version: str

    # Owner daily digest email (optional — digest send is skipped, not
    # fatal, if these aren't set; this is an add-on, not core auth)
    resend_api_key: str | None
    digest_from_email: str | None
    digest_window_hours: int
    public_base_url: str | None

    # Customer win-back: platform-wide default "lapsed" threshold, used
    # when a tenant hasn't set their own TenantConfig.winback_after_days.
    winback_default_days: int

    # Server
    port: int

    # Kill switch: set BUSINESS_AI_ENABLED=false to pause all AI operations
    # instantly (e.g. a cost spike or incident) without a redeploy.
    enabled: bool


def load_settings() -> Settings:
    raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000")
    allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip() and o.strip() != "*"]
    if not allowed_origins:
        allowed_origins = ["http://localhost:8000"]

    jwt_secret = os.getenv("JWT_SECRET_KEY")
    admin_secret = os.getenv("ADMIN_SECRET")

    return Settings(
        jwt_secret_key=jwt_secret.strip() if jwt_secret and jwt_secret.strip() else None,
        jwt_issuer=os.getenv("JWT_ISSUER", "business-ai-auth").strip() or "business-ai-auth",
        jwt_audience=os.getenv("JWT_AUDIENCE", "business-ai-api").strip() or "business-ai-api",
        admin_secret=admin_secret.strip() if admin_secret and admin_secret.strip() else None,
        auth_required=_bool_env("AUTH_REQUIRED", False),
        app_env=os.getenv("APP_ENV", "development").strip() or "development",
        allowed_origins=allowed_origins,
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip() or "text-embedding-3-small",
        max_output_tokens=_int_env("MAX_OUTPUT_TOKENS", 800),
        active_tenant_quota=_int_env("ACTIVE_TENANT_QUESTION_LIMIT", 500),
        requests_per_minute=_int_env("REQUESTS_PER_MINUTE", 2),
        max_concurrent_requests=_int_env("MAX_CONCURRENT_REQUESTS_PER_SESSION", 1),
        max_query_length=_int_env("MAX_QUERY_LENGTH", 1000),
        default_whatsapp_number=(os.getenv("DEFAULT_WHATSAPP_NUMBER") or "").strip() or None,
        whatsapp_app_secret=(os.getenv("WHATSAPP_APP_SECRET") or "").strip() or None,
        whatsapp_verify_token=(os.getenv("WHATSAPP_VERIFY_TOKEN") or "").strip() or None,
        whatsapp_api_version=os.getenv("WHATSAPP_API_VERSION", "v21.0").strip() or "v21.0",
        resend_api_key=(os.getenv("RESEND_API_KEY") or "").strip() or None,
        digest_from_email=(os.getenv("DIGEST_FROM_EMAIL") or "").strip() or None,
        digest_window_hours=_int_env("DIGEST_WINDOW_HOURS", 24),
        public_base_url=(os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/") or None,
        winback_default_days=_int_env("WINBACK_DEFAULT_DAYS", 45),
        port=_int_env("PORT", 8000),
        enabled=_bool_env("BUSINESS_AI_ENABLED", True),
    )


# Known-weak secrets that must never be accepted when auth is enforced in
# production. Mirrors Shri AI's fail-closed startup guard.
KNOWN_INSECURE_SECRETS = frozenset(
    {
        "change-me-to-a-real-32-byte-secret-in-production",
        "change-me-to-a-real-admin-secret",
        "secret",
        "changeme",
        "password",
        "admin",
        "test",
        "business_ai_secret",
    }
)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    level: str  # "error" | "warning"


def validate_environment(settings: Settings) -> list[ValidationIssue]:
    """Fail-closed startup check: refuse to run with weak/missing secrets
    once auth is actually enforced (production). In development, warn only.
    """
    issues: list[ValidationIssue] = []
    enforced = settings.auth_required or settings.app_env == "production"

    for name, value in (("JWT_SECRET_KEY", settings.jwt_secret_key), ("ADMIN_SECRET", settings.admin_secret)):
        if not value or len(value) < 16 or value.lower() in KNOWN_INSECURE_SECRETS:
            level = "error" if enforced else "warning"
            issues.append(
                ValidationIssue(
                    code=f"WEAK_{name}",
                    message=f"{name} is missing, too short, or a known-insecure default value.",
                    level=level,
                )
            )

    if not os.getenv("OPENAI_API_KEY"):
        issues.append(ValidationIssue(code="MISSING_OPENAI_API_KEY", message="OPENAI_API_KEY is not set.", level="error"))

    return issues
