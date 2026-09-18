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
    # Encrypts a tenant's WhatsApp access token / Razorpay key secret at
    # rest (see secrets_vault.py). Unset = those fields stay plaintext,
    # exactly as every tenant onboarded before this setting existed —
    # validate_environment() below fails closed on this in production,
    # same tier as JWT_SECRET_KEY/ADMIN_SECRET.
    secret_encryption_key: str | None

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

    # Phase 9: broad, app-wide abuse guards layered in FRONT of the
    # per-(tenant, session) question quota above — see rate_limiting.py
    # for why a single session-scoped counter can't catch either of
    # these on its own. Defaults are deliberately generous (a real
    # dashboard page load alone fires a dozen-plus API calls) — this is
    # an abuse guard, not a tight per-user quota; 0 disables a dimension.
    ip_requests_per_minute: int
    tenant_requests_per_minute: int

    # WhatsApp handoff (wa.me click-to-chat fallback link)
    default_whatsapp_number: str | None

    # WhatsApp Business Cloud API (platform-level: one Meta App shared by
    # every tenant's own WhatsApp Business number). Each tenant brings
    # their own phone_number_id + access token (see TenantConfig) — this
    # app never holds a tenant's WhatsApp credentials as platform secrets.
    whatsapp_app_secret: str | None
    whatsapp_verify_token: str | None
    whatsapp_api_version: str
    # Embedded Signup ("Connect WhatsApp" one-click onboarding). The App
    # ID is public (safe in frontend JS, unlike the secret above); both
    # being unset simply means the manual Cloud API flow is the only
    # option, which is the correct default until Business AI is an
    # approved Meta Tech Provider.
    whatsapp_app_id: str | None
    # WhatsApp Business Login "Configuration ID" — created in the Meta
    # App dashboard under WhatsApp -> Embedded Signup, identifies WHICH
    # signup flow/permissions to launch. Also a public, non-secret
    # identifier (passed to Meta's own JS SDK client-side), but distinct
    # from whatsapp_app_id — both are required together for the "Connect
    # WhatsApp" button to actually work.
    whatsapp_config_id: str | None

    # Owner daily digest email (optional — digest send is skipped, not
    # fatal, if these aren't set; this is an add-on, not core auth)
    resend_api_key: str | None
    digest_from_email: str | None
    # Reply-To for outgoing transactional email. Needed because
    # digest_from_email points at a no-mailbox sending address
    # (noreply@send.<domain>) — without this, a customer replying to any
    # automated email (password reset, alerts, etc.) has nowhere to land.
    # Unset = no Reply-To header is sent, same as before this setting existed.
    support_reply_to: str | None
    digest_window_hours: int
    public_base_url: str | None
    # Onboarding: notifies you when a new business signs up (otherwise
    # only visible by checking the admin panel) and notifies the owner
    # when you activate them. Optional — skipped gracefully if unset,
    # same as every other email feature above.
    platform_admin_email: str | None

    # Business AI's OWN Razorpay account, for collecting subscription
    # payment FROM tenants — distinct from a tenant's own razorpay_key_id/
    # secret (TenantConfig), which collects deposits from THAT tenant's
    # own customers into that tenant's own account. Unset = billing links
    # can't be sent yet; tenants without a price set are unaffected.
    platform_razorpay_key_id: str | None
    platform_razorpay_key_secret: str | None
    # Verifies X-Razorpay-Signature on POST /api/webhooks/razorpay (set in
    # the Razorpay dashboard's Webhooks screen to the same value). Unset =
    # the webhook route fails closed (rejects every event) and platform
    # payments fall back to the existing admin mark-paid confirmation —
    # never silently trusts an unverifiable "paid" claim.
    platform_razorpay_webhook_secret: str | None
    # Self-serve subscription price shown to a NEW owner during onboarding
    # (Phase 8) — an operator-configured business decision, not invented
    # by this app. Unset = the self-serve plan/payment step is skipped
    # entirely and a tenant activates exactly as it does today (free,
    # unpriced) — fully backward compatible with every tenant onboarded
    # before this setting existed.
    platform_subscription_price_inr: int | None
    # Phase 2 — real recurring billing. Each tier's Razorpay Plan ID,
    # created ONCE via RazorpayClient.create_plan() and pasted in here —
    # not created automatically at request time. All three unset (the
    # default, true for every deployment until this is set up on a real,
    # KYC'd Razorpay account) means self-serve checkout stays on the
    # existing one-time Payment Links path; nothing about that path
    # changes until these are configured.
    platform_razorpay_plan_id_starter: str | None
    platform_razorpay_plan_id_growth: str | None
    platform_razorpay_plan_id_scale: str | None

    # Customer win-back: platform-wide default "lapsed" threshold, used
    # when a tenant hasn't set their own TenantConfig.winback_after_days.
    winback_default_days: int

    # Phase 25 — Review aggregation (reviews.py). A PLATFORM-level
    # credential, deliberately NOT "bring your own" like WhatsApp/
    # Razorpay: a Google Cloud project + billing setup is a much higher-
    # friction ask for a small business owner than WhatsApp's Meta
    # Developer flow. Each tenant only brings their own `google_place_id`
    # (TenantConfig). Unset = the automated Google Places sync cron
    # skips every tenant gracefully; manual review logging (Zomato/
    # Swiggy, or Google without this key) is completely unaffected.
    google_places_api_key: str | None

    # Server
    port: int

    # Kill switch: set BUSINESS_AI_ENABLED=false to pause all AI operations
    # instantly (e.g. a cost spike or incident) without a redeploy.
    enabled: bool

    # Offsite backups: optional push to S3-compatible storage (AWS S3, Backblaze B2,
    # Cloudflare R2, MinIO, etc.). All unset by default — existing single-volume
    # deployments are completely unaffected.
    backup_s3_bucket: str | None = None
    backup_s3_access_key_id: str | None = None
    backup_s3_secret_access_key: str | None = None
    backup_s3_endpoint_url: str | None = None
    backup_s3_region: str | None = None

    # Endpoint-specific rate limits
    signup_requests_per_hour: int = 10
    ai_questions_per_minute: int = 30


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
        secret_encryption_key=(os.getenv("SECRET_ENCRYPTION_KEY") or "").strip() or None,
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
        ip_requests_per_minute=_int_env("IP_REQUESTS_PER_MINUTE", 120),
        tenant_requests_per_minute=_int_env("TENANT_REQUESTS_PER_MINUTE", 300),
        default_whatsapp_number=(os.getenv("DEFAULT_WHATSAPP_NUMBER") or "").strip() or None,
        whatsapp_app_secret=(os.getenv("WHATSAPP_APP_SECRET") or "").strip() or None,
        whatsapp_verify_token=(os.getenv("WHATSAPP_VERIFY_TOKEN") or "").strip() or None,
        whatsapp_api_version=os.getenv("WHATSAPP_API_VERSION", "v21.0").strip() or "v21.0",
        whatsapp_app_id=(os.getenv("WHATSAPP_APP_ID") or "").strip() or None,
        whatsapp_config_id=(os.getenv("WHATSAPP_CONFIG_ID") or "").strip() or None,
        resend_api_key=(os.getenv("RESEND_API_KEY") or "").strip() or None,
        digest_from_email=(os.getenv("DIGEST_FROM_EMAIL") or "").strip() or None,
        support_reply_to=(os.getenv("SUPPORT_REPLY_TO_EMAIL") or "").strip() or None,
        platform_admin_email=(os.getenv("PLATFORM_ADMIN_EMAIL") or "").strip() or None,
        platform_razorpay_key_id=(os.getenv("PLATFORM_RAZORPAY_KEY_ID") or "").strip() or None,
        platform_razorpay_key_secret=(os.getenv("PLATFORM_RAZORPAY_KEY_SECRET") or "").strip() or None,
        platform_razorpay_webhook_secret=(os.getenv("PLATFORM_RAZORPAY_WEBHOOK_SECRET") or "").strip() or None,
        platform_subscription_price_inr=(_int_env("PLATFORM_SUBSCRIPTION_PRICE_INR", 0) or None),
        platform_razorpay_plan_id_starter=(os.getenv("PLATFORM_RAZORPAY_PLAN_ID_STARTER") or "").strip() or None,
        platform_razorpay_plan_id_growth=(os.getenv("PLATFORM_RAZORPAY_PLAN_ID_GROWTH") or "").strip() or None,
        platform_razorpay_plan_id_scale=(os.getenv("PLATFORM_RAZORPAY_PLAN_ID_SCALE") or "").strip() or None,
        digest_window_hours=_int_env("DIGEST_WINDOW_HOURS", 24),
        public_base_url=(os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/") or None,
        winback_default_days=_int_env("WINBACK_DEFAULT_DAYS", 45),
        google_places_api_key=(os.getenv("GOOGLE_PLACES_API_KEY") or "").strip() or None,
        port=_int_env("PORT", 8000),
        enabled=_bool_env("BUSINESS_AI_ENABLED", True),
        backup_s3_bucket=(os.getenv("BACKUP_S3_BUCKET") or "").strip() or None,
        backup_s3_access_key_id=(os.getenv("BACKUP_S3_ACCESS_KEY_ID") or "").strip() or None,
        backup_s3_secret_access_key=(os.getenv("BACKUP_S3_SECRET_ACCESS_KEY") or "").strip() or None,
        backup_s3_endpoint_url=(os.getenv("BACKUP_S3_ENDPOINT_URL") or "").strip() or None,
        backup_s3_region=(os.getenv("BACKUP_S3_REGION") or "").strip() or None,
        signup_requests_per_hour=_int_env("SIGNUP_REQUESTS_PER_HOUR", 10),
        ai_questions_per_minute=_int_env("AI_QUESTIONS_PER_MINUTE", 30),
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

    # Deliberately ALWAYS a warning, never an "error" that blocks startup
    # even in production — unlike JWT_SECRET_KEY/ADMIN_SECRET (missing
    # either breaks the app outright), a missing SECRET_ENCRYPTION_KEY
    # just means WhatsApp/Razorpay secrets stay plaintext, exactly as
    # every tenant onboarded before this setting existed. Failing closed
    # here would break every already-deployed instance's next restart —
    # the opposite of the backward-compatible migration this setting is
    # supposed to be.
    if not settings.secret_encryption_key or len(settings.secret_encryption_key) < 16:
        issues.append(
            ValidationIssue(
                code="WEAK_SECRET_ENCRYPTION_KEY",
                message="SECRET_ENCRYPTION_KEY is missing or too short — WhatsApp/Razorpay secrets are stored in plaintext until this is set (see scripts/rotate_secrets.py to encrypt existing tenants once it is).",
                level="warning",
            )
        )

    if not os.getenv("OPENAI_API_KEY"):
        issues.append(ValidationIssue(code="MISSING_OPENAI_API_KEY", message="OPENAI_API_KEY is not set.", level="error"))

    return issues
