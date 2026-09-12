"""FastAPI application: wires auth, tenants, RAG, leads, and analytics
together behind HTTP routes.

Security posture learned directly from Shri AI's own mistakes this
session, applied correctly from day one here:
  - CSP is scoped to what this app's inline-script frontend actually needs
    (script-src/style-src 'unsafe-inline'), not a bare default-src 'self'
    that would silently break every page's JavaScript in a real browser.
  - Every route resolves the caller's Principal from the Authorization
    header ONLY — no client-suppliable identity headers.
  - Every tenant-scoped action goes through the single `authorize()`
    chokepoint in tenant.py.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# Explicit path, not the default upward-search: this app can be launched
# with a cwd outside the project (e.g. a dev-server runner invoked from a
# sibling directory), so relying on load_dotenv()'s implicit cwd-walk
# would silently no-op in that case.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from business_ai.alerts import (
    render_billing_link_email,
    render_dissatisfaction_alert,
    render_new_tenant_signup_alert,
    render_review_request,
    render_task_escalation_alert,
    render_tenant_activated_email,
    render_tenant_self_activated_notice,
    render_urgent_feedback_alert,
)
from business_ai.analytics import AnalyticsStore
from business_ai.audit import AuditLogStore
from business_ai.digest import has_digest_content, render_owner_digest, render_owner_whatsapp_summary
from business_ai.employees import Employee, EmployeeStore
from business_ai.feedback import FeedbackStore
from business_ai.memory import SopStore
from business_ai.email_sender import EmailSendError, EmailSender
from business_ai.auth import Principal, UserStore, create_access_token, resolve_principal, AuthenticationError
from business_ai.config import Settings, load_settings, validate_environment
from business_ai.generation import (
    GroundedAnswer,
    OpenAIGenerationProvider,
    build_abstention_answer,
    build_system_prompt,
    build_user_prompt,
    default_abstention_message,
    evaluate_evidence_gate,
    is_hindi_script,
    validate_llm_draft,
)
from business_ai.ingestion import IngestionError, SourceStore, extract_pdf_text, fetch_website_text, ingest_text
from business_ai.leads import Lead, LeadStore, lead_stage
from business_ai.payments import PaymentLinkError, RazorpayClient
from business_ai.retrieval import OpenAIEmbeddingProvider, RetrievalEngine, VectorStore
from business_ai.security import InvalidTenantIdError, UnsafeUrlError, validate_tenant_id
from business_ai.tasks import Task, TaskStore
from business_ai.tenant import (
    TenantAction,
    TenantConfig,
    TenantNotActiveError,
    TenantNotFoundError,
    TenantRegistry,
    TenantStatus,
    UnauthorizedError,
    authorize,
)
from business_ai.usage_limiter import AccessDecision, ReservationResult, UsageLimiter
from business_ai.whatsapp import (
    REENGAGEMENT_WINDOW_CLOSED_CODE,
    MetaEmbeddedSignupClient,
    MetaEmbeddedSignupError,
    WhatsAppClient,
    WhatsAppInboxStore,
    WhatsAppSendError,
    parse_webhook_payload,
    verify_webhook_signature,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# Anchored to this project's own directory, never to the launching
# process's cwd. A cwd-relative "data" path is a real cross-project
# isolation hazard: this app could otherwise be started from a sibling
# project's directory and silently read/write that project's own
# data/ (found in dev when a mismatched schema surfaced the collision
# before any actual write happened).
DATA_ROOT = PROJECT_ROOT / "data"
STATIC_DIR = PROJECT_ROOT / "static"

# Automation windows (implementation constants, not tenant/platform-tunable
# knobs — see winback_after_days/deposit_amount_inr on TenantConfig for the
# dials that genuinely vary per business). Each of these is read by an
# admin/*/run endpoint meant to be invoked once a day by an external cron;
# there's no in-process scheduler in this app.
REENGAGEMENT_MIN_AGE_HOURS = 48  # give a lead a fair chance to book on their own first
REENGAGEMENT_MAX_AGE_HOURS = 24 * 14  # older than this is stale — don't blast old history on first run
REMINDER_WINDOW_START_HOURS = 20  # a ~24h-before reminder, with slack for cron timing drift
REMINDER_WINDOW_END_HOURS = 28
# A feedback theme reported at least this many times in one window is a
# pattern worth surfacing to management (digest, action brief, scorecard),
# not a one-off complaint.
RECURRING_FEEDBACK_THRESHOLD = 3
# A task overdue by more than this is a proactive-alert-worthy problem,
# not just a line in tomorrow's digest — see /api/v1/admin/task-escalation/run.
TASK_ESCALATION_HOURS = 48


class Services:
    """Everything a request handler needs, built once at startup."""

    def __init__(self, settings: Settings, data_root: Path = DATA_ROOT) -> None:
        self.settings = settings
        self.data_root = data_root
        self.user_store = UserStore(data_root / "users.db")
        self.tenant_registry = TenantRegistry(data_root / "tenants.db")
        self.usage_limiter = UsageLimiter(data_root / "usage.db", settings)
        self.lead_store = LeadStore(data_root / "leads.db")
        self.analytics_store = AnalyticsStore(data_root / "analytics.db")
        self.source_store = SourceStore(data_root / "sources.db")
        self.vector_store = VectorStore(data_root / "chroma")
        self.whatsapp_inbox = WhatsAppInboxStore(data_root / "whatsapp_inbox.db")
        self.employee_store = EmployeeStore(data_root / "employees.db")
        self.task_store = TaskStore(data_root / "tasks.db")
        self.audit_log = AuditLogStore(data_root / "audit.db")
        self.feedback_store = FeedbackStore(data_root / "feedback.db")
        self.sop_store = SopStore(data_root / "sops.db")

    def embeddings(self):
        return OpenAIEmbeddingProvider(model_name=self.settings.embedding_model, api_key=_openai_key())

    def generator(self):
        return OpenAIGenerationProvider(
            model_name=self.settings.llm_model, api_key=_openai_key(), max_output_tokens=self.settings.max_output_tokens
        )

    def email_sender(self) -> EmailSender:
        return EmailSender(
            api_key=self.settings.resend_api_key or "", from_address=self.settings.digest_from_email or ""
        )

    def whatsapp_client(self) -> WhatsAppClient:
        return WhatsAppClient(api_version=self.settings.whatsapp_api_version)

    def razorpay_client(self) -> RazorpayClient:
        return RazorpayClient()

    def meta_signup_client(self) -> MetaEmbeddedSignupClient:
        return MetaEmbeddedSignupClient(api_version=self.settings.whatsapp_api_version)


def _openai_key() -> str:
    import os

    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    return key


# ==============================================================================
# Request/response models
# ==============================================================================


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=6, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    business_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: str
    password: str


class AskRequest(BaseModel):
    query: str = Field(min_length=1)
    session_id: str | None = None


class LeadRequest(BaseModel):
    session_id: str
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    message: str | None = None


class WebsiteIngestRequest(BaseModel):
    url: str
    label: str | None = None


class GapPublishRequest(BaseModel):
    answer_text: str = Field(min_length=1, max_length=2000)


class AppointmentRequest(BaseModel):
    appointment_at: str = Field(min_length=1)  # ISO 8601 datetime, e.g. "2026-09-20T16:00:00"


class DepositPaidRequest(BaseModel):
    amount_inr: int | None = None  # defaults to the tenant's configured deposit_amount_inr if unset


class AppointmentOutcomeRequest(BaseModel):
    outcome: str  # "completed" | "no_show" | "cancelled"


class EmbeddedSignupRequest(BaseModel):
    # Both handed to the frontend directly by Meta's Embedded Signup
    # popup callback — see MetaEmbeddedSignupClient's docstring.
    code: str = Field(min_length=1)
    phone_number_id: str = Field(min_length=1)


class BillingLinkRequest(BaseModel):
    amount_inr: int = Field(gt=0)


class MarkPaidRequest(BaseModel):
    amount_inr: int | None = None


class CreateEmployeeRequest(BaseModel):
    whatsapp_number: str
    name: str
    role: str = "staff"  # "owner" | "manager" | "staff"


class CreateTaskRequest(BaseModel):
    title: str
    assigned_to_employee_id: str
    description: str | None = None
    due_at: str | None = None  # parsed the same way as appointment_at (local business time -> UTC)
    approval_required: bool = False
    customer_facing_lead_id: str | None = None  # enables the verified-outcome customer ping on completion


class ApproveSopRequest(BaseModel):
    theme: str
    text: str


class UpdateEmployeeRequest(BaseModel):
    role: str | None = None
    active: bool | None = None


class TenantConfigUpdate(BaseModel):
    assistant_name: str | None = None
    welcome_message: str | None = None
    whatsapp_number: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_access_token: str | None = None
    owner_whatsapp_number: str | None = None
    admin_notify_template_name: str | None = None
    review_link: str | None = None
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    deposit_amount_inr: int | None = None
    winback_after_days: int | None = None


def _slugify_tenant_id(business_name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", business_name.lower()).strip("-")[:32] or "biz"
    return f"{base}-{secrets.token_hex(3)}"


def _whatsapp_link(number: str | None, message: str) -> str | None:
    if not number:
        return None
    import urllib.parse

    return f"https://wa.me/{number}?text={urllib.parse.quote(message)}"


# Single-timezone V1: every pilot business is in India, and there is no
# per-tenant timezone configuration anywhere in this app yet. A bare
# datetime typed into the dashboard's <input type="datetime-local"> is
# assumed to be the business's own local (IST) wall-clock time; real
# multi-timezone support is a stated future scope item, not silently
# guessed at here.
IST = timezone(timedelta(hours=5, minutes=30))


def _parse_appointment_to_utc(raw: str) -> str:
    """Parses an owner-entered appointment datetime into the sortable UTC
    ISO string every other timestamp in this app already uses. Raises
    ValueError on anything unparseable — callers turn that into a 400."""
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_appointment_ist(iso_utc: str) -> str:
    """The reverse direction, for a human-readable line in a customer-
    facing reminder/win-back message — always shown in the business's
    own local time, never raw UTC."""
    dt_utc = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(IST).strftime("%A, %d %b at %I:%M %p")


_CITATION_MARKER_RE = re.compile(r"\s*\(seg_[a-zA-Z0-9_]+\)")


def _strip_citation_markers(text: str) -> str:
    """The grounded-generation prompt instructs the model to inline
    "(seg_abc123)" citation markers after each claim (see
    generation.build_system_prompt) — the website widget keeps these
    invisible to a real customer by never rendering answer_text raw as
    the only signal (a separate "Sourced from X" chip carries the actual
    citation). WhatsApp has no such chip; sending literal segment IDs
    straight to a customer's phone would look broken, so the WhatsApp
    reply path strips them. The underlying grounding/citation validation
    in generation.validate_llm_draft is untouched — this is purely a
    presentation-layer cleanup for one channel's plain-text medium."""
    return _CITATION_MARKER_RE.sub("", text).strip()


# ==============================================================================
# Admin WhatsApp bot — employee coordination command grammar
# ==============================================================================
# Deliberately deterministic keyword parsing for Phase 0, no LLM call: this
# needs to be correct and free, not clever. "assign <title> to <name> [by
# <date>]" and "reassign <id> to <name>" are the only two-clause commands;
# everything else is "<verb> <short task id> [reason]" or a bare keyword.
_ADMIN_ASSIGN_RE = re.compile(
    r"^assign\s+(.+?)\s+to\s+(.+?)(?:\s+for\s+lead\s+(\S+))?(?:\s+by\s+(.+))?$", re.IGNORECASE
)
_ADMIN_REASSIGN_RE = re.compile(r"^reassign\s+(\S+)\s+to\s+(.+)$", re.IGNORECASE)
_ADMIN_STATUS_VERBS = frozenset({"start", "done", "blocked", "cancel", "approve", "reject"})
_ADMIN_FEEDBACK_RE = re.compile(r"^feedback\s+(.+)$", re.IGNORECASE | re.DOTALL)
_ADMIN_SOP_RE = re.compile(r"^approve sop\s+(.+?)\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)
_ADMIN_SUGGEST_SOP_RE = re.compile(r"^suggest sop\s+(.+)$", re.IGNORECASE)
_ADMIN_MARK_PAID_RE = re.compile(r"^mark\s+paid\s+(\S+)(?:\s+(\d+))?$", re.IGNORECASE)
_ADMIN_MARK_OUTCOME_RE = re.compile(r"^mark\s+(completed|no-show|no_show|cancelled|canceled)\s+(\S+)$", re.IGNORECASE)
_OUTCOME_ALIASES = {"no-show": "no_show", "no_show": "no_show", "cancelled": "cancelled", "canceled": "cancelled", "completed": "completed"}

_FEEDBACK_THEME_LABELS = {
    "equipment_or_supplies": "Equipment/supplies",
    "software_or_tools": "Software/tools",
    "scheduling_or_shifts": "Scheduling/shifts",
    "communication_or_coordination": "Communication/coordination",
    "training_or_process": "Training/process",
    "workload_or_staffing": "Workload/staffing",
    "customer_related": "Customer-related",
    "pay_or_compensation": "Pay/compensation",
    "safety_or_compliance": "Safety/compliance",
    "other": "Other",
}


def _admin_bot_help_text() -> str:
    return (
        "Commands:\n"
        "• today / status — your daily summary\n"
        "• tasks — open tasks\n"
        "• my tasks — tasks assigned to you\n"
        "• overdue — overdue tasks, by name\n"
        "• assign <task> to <name> [by <YYYY-MM-DD HH:MM>] (owner/manager)\n"
        "• start/done/blocked/cancel <task id> [reason]\n"
        "• approve/reject <task id> [reason] (owner/manager)\n"
        "• reassign <task id> to <name> (owner/manager)\n"
        "• feedback <what's going on> — report a concern or suggestion\n"
        "• feedback themes — recurring issues (owner/manager)\n"
        "• scorecard / health — business snapshot (owner/manager)\n"
        "• approve sop <theme>: <note text> — save team guidance (owner)\n"
        "• suggest sop <theme> — draft guidance from recent reports (owner)\n"
        "• mark paid <lead id> [<amount>] — confirm a deposit received\n"
        "• mark completed/no-show/cancelled <lead id> — record what happened\n"
        "• timeline — recent activity (owner/manager)\n"
        "\nOr just type naturally — I'll do my best to understand "
        "(except money/outcome confirmations, which always need the exact commands above)."
    )


def _short_task_id(task_id: str) -> str:
    return task_id[-6:]


# ==============================================================================
# App factory
# ==============================================================================


def create_app(services: Services | None = None) -> FastAPI:
    settings = load_settings()
    svc = services or Services(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        issues = validate_environment(svc.settings)
        errors = [i for i in issues if i.level == "error"]
        if errors and (svc.settings.auth_required or svc.settings.app_env == "production"):
            raise RuntimeError("Startup validation failed: " + "; ".join(i.message for i in errors))
        yield

    app = FastAPI(title="Business AI", version="0.1.0", lifespan=lifespan)

    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            # Scoped to what this app's inline-script/inline-style static
            # pages actually need — NOT a bare default-src 'self' (that
            # mistake, made and fixed in Shri AI this same session, would
            # silently break every page's JavaScript in a real browser).
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'"
            )
            return response

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=svc.settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        is_api_path = request.url.path.startswith(("/api/", "/healthz"))
        if exc.status_code == 404 and not is_api_path and STATIC_DIR.is_dir():
            page = STATIC_DIR / "404.html"
            if page.is_file():
                return FileResponse(page, status_code=404)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)

    def _resolve(authorization: str | None) -> Principal | None:
        try:
            return resolve_principal(authorization, svc.settings)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def _require(authorization: str | None) -> Principal:
        principal = _resolve(authorization)
        if principal is None:
            raise HTTPException(status_code=401, detail="Authentication required.")
        return principal

    def _notify_admin_of_signup(tenant: TenantConfig) -> None:
        """Best-effort, same pattern as every other optional email
        feature: silently skipped if PLATFORM_ADMIN_EMAIL /
        RESEND_API_KEY / DIGEST_FROM_EMAIL aren't all configured, never
        allowed to fail the signup itself."""
        if not (svc.settings.platform_admin_email and svc.settings.resend_api_key and svc.settings.digest_from_email):
            return
        dashboard_url = f"{svc.settings.public_base_url}/dashboard" if svc.settings.public_base_url else None
        subject, html = render_new_tenant_signup_alert(
            business_name=tenant.business_name, owner_email=tenant.owner_email,
            tenant_id=tenant.tenant_id, dashboard_url=dashboard_url,
        )
        try:
            svc.email_sender().send(to=svc.settings.platform_admin_email, subject=subject, html_body=html)
        except EmailSendError as exc:
            logger.warning("Failed to send new-signup admin notification for tenant %s: %s", tenant.tenant_id, exc)

    def _notify_owner_of_activation(tenant: TenantConfig) -> None:
        if not (svc.settings.resend_api_key and svc.settings.digest_from_email):
            return
        chat_url = f"{svc.settings.public_base_url}/chat?tenant_id={tenant.tenant_id}" if svc.settings.public_base_url else None
        subject, html = render_tenant_activated_email(
            business_name=tenant.business_name, assistant_name=tenant.assistant_name, chat_url=chat_url,
        )
        try:
            svc.email_sender().send(to=tenant.owner_email, subject=subject, html_body=html)
        except EmailSendError as exc:
            logger.warning("Failed to send activation email for tenant %s: %s", tenant.tenant_id, exc)

    def _notify_admin_of_self_activation(tenant: TenantConfig) -> None:
        """Visibility only, never a gate — see self_activate_tenant."""
        if not (svc.settings.platform_admin_email and svc.settings.resend_api_key and svc.settings.digest_from_email):
            return
        dashboard_url = f"{svc.settings.public_base_url}/dashboard" if svc.settings.public_base_url else None
        subject, html = render_tenant_self_activated_notice(
            business_name=tenant.business_name, tenant_id=tenant.tenant_id, dashboard_url=dashboard_url,
        )
        try:
            svc.email_sender().send(to=svc.settings.platform_admin_email, subject=subject, html_body=html)
        except EmailSendError as exc:
            logger.warning("Failed to send self-activation notice for tenant %s: %s", tenant.tenant_id, exc)

    def _activation_blocker(tenant_id: str) -> str | None:
        """The one bar every activation path must clear — admin-triggered
        or owner self-service alike. Returns a human-readable reason to
        block, or None when clear to activate."""
        if svc.vector_store.count_for_tenant(tenant_id) == 0:
            return "Cannot activate: no knowledge sources ingested yet."
        tenant = svc.tenant_registry.get_config(tenant_id)
        if tenant.subscription_price_inr and tenant.billing_status != "paid":
            return "Payment is required before activating — check your email for the payment link."
        return None

    def _notify_management_whatsapp(tenant: TenantConfig, body: str) -> int:
        """Best-effort push to EVERY owner/manager on the tenant's
        employee roster — a fast, far-more-likely-to-be-seen companion to
        email for the things that most benefit from it: an instant
        complaint/urgent-feedback alert, and the daily digest. Generalizes
        the old single-scalar owner_whatsapp_number push (Phase 0's
        EmployeeStore roster is the source of truth now; the scalar still
        bootstraps into it, see EmployeeStore.ensure_owner_bootstrap).

        Requires a connected business number to send FROM; silently
        unavailable, not an error, when missing — email stays the
        guaranteed channel regardless. A business-initiated free-text
        message to someone who hasn't messaged the business's line in the
        last 24h fails Meta's customer-service-window rule exactly like it
        would for a customer; when that happens AND the tenant has
        configured an approved template (admin_notify_template_name), a
        short fixed fallback notice goes out as a template instead — real
        text content still only ever reaches someone inside the live
        window. Returns how many recipients were actually reached."""
        if not (tenant.whatsapp_phone_number_id and tenant.whatsapp_access_token):
            return 0
        svc.employee_store.ensure_owner_bootstrap(tenant.tenant_id, tenant.owner_whatsapp_number)
        recipients = [
            e for e in svc.employee_store.list_for_tenant(tenant.tenant_id) if e.role in ("owner", "manager")
        ]
        sent_count = 0
        for recipient in recipients:
            try:
                svc.whatsapp_client().send_text(
                    phone_number_id=tenant.whatsapp_phone_number_id, access_token=tenant.whatsapp_access_token,
                    to=recipient.whatsapp_number, body=body,
                )
                sent_count += 1
                continue
            except WhatsAppSendError as exc:
                if exc.error_code != REENGAGEMENT_WINDOW_CLOSED_CODE or not tenant.admin_notify_template_name:
                    logger.warning("Management WhatsApp push failed for tenant %s: %s", tenant.tenant_id, exc)
                    continue
            try:
                svc.whatsapp_client().send_template(
                    phone_number_id=tenant.whatsapp_phone_number_id, access_token=tenant.whatsapp_access_token,
                    to=recipient.whatsapp_number, template_name=tenant.admin_notify_template_name,
                )
                sent_count += 1
            except WhatsAppSendError as exc:
                logger.warning("Management WhatsApp template fallback failed for tenant %s: %s", tenant.tenant_id, exc)
        return sent_count

    def _send_dissatisfaction_alert(*, tenant: TenantConfig, query: str, answer_text: str, session_id: str) -> None:
        """Best-effort: a slow/failed alert must never break the
        customer's actual chat response, so failures are swallowed here,
        not raised — this mirrors the digest-run route's per-tenant
        failure collection, just for a single synchronous event instead
        of a batch job. Tries management's WhatsApp AND email independently
        — neither gates the other."""
        _notify_management_whatsapp(
            tenant,
            f"⚠️ A customer sounds unhappy on {tenant.business_name}'s assistant.\n\n"
            f'They said: "{query}"\n\nOpen your dashboard for the full conversation.',
        )

        if not svc.settings.resend_api_key or not svc.settings.digest_from_email:
            return
        dashboard_url = f"{svc.settings.public_base_url}/dashboard" if svc.settings.public_base_url else None
        subject, html = render_dissatisfaction_alert(
            business_name=tenant.business_name, query=query, answer_text=answer_text or None,
            session_id=session_id, dashboard_url=dashboard_url,
        )
        try:
            svc.email_sender().send(to=tenant.owner_email, subject=subject, html_body=html)
        except EmailSendError as exc:
            logger.warning("Failed to send dissatisfaction alert for tenant %s: %s", tenant.tenant_id, exc)

    def _send_urgent_feedback_alert(tenant: TenantConfig, employee: Employee, feedback_text: str, classification) -> None:
        """Instant alert for high-urgency employee feedback — same
        "don't wait for tomorrow's digest" shape as
        _send_dissatisfaction_alert, mirrored for the team side of the
        business. Best-effort on both channels; never blocks the
        employee's own acknowledgment reply."""
        theme_label = _FEEDBACK_THEME_LABELS.get(classification.theme, classification.theme)
        _notify_management_whatsapp(
            tenant,
            f"🚨 Urgent feedback from {employee.name} ({theme_label}):\n\n"
            f'"{feedback_text}"\n\n{classification.suggested_action}',
        )
        if not svc.settings.resend_api_key or not svc.settings.digest_from_email:
            return
        dashboard_url = f"{svc.settings.public_base_url}/dashboard" if svc.settings.public_base_url else None
        subject, html = render_urgent_feedback_alert(
            business_name=tenant.business_name, employee_name=employee.name, theme_label=theme_label,
            raw_text=feedback_text, suggested_action=classification.suggested_action, dashboard_url=dashboard_url,
        )
        try:
            svc.email_sender().send(to=tenant.owner_email, subject=subject, html_body=html)
        except EmailSendError as exc:
            logger.warning("Failed to send urgent feedback alert for tenant %s: %s", tenant.tenant_id, exc)

    def _process_question(
        *, tenant: TenantConfig, tenant_id: str, session_id: str, query: str, channel: str = "web",
    ) -> tuple[ReservationResult, GroundedAnswer | None]:
        """The single grounded-answer pipeline shared by every customer-
        facing channel: retrieval -> abstention gate -> generation ->
        citation validation -> analytics logging -> dissatisfaction alert.
        Both /api/ask (web widget) and the WhatsApp webhook call this, so
        a business's RAG knowledge, lead/intent signals, and complaint
        alerts behave identically no matter which channel a customer used.

        Returns (reservation_result, answer). answer is None exactly when
        reservation_result.decision != ALLOW (quota/rate-limit denial) —
        callers translate that into an HTTP error or a graceful WhatsApp
        reply, per channel.
        """
        if len(query) > svc.settings.max_query_length:
            return (
                ReservationResult(
                    decision=AccessDecision.DENY,
                    reason=f"QUERY_TOO_LONG: exceeds {svc.settings.max_query_length} characters.",
                ),
                None,
            )

        req_id = f"req_{secrets.token_hex(8)}"
        res = svc.usage_limiter.check_and_reserve(tenant_id, session_id, req_id, quota_override=tenant.question_quota)
        if res.decision != AccessDecision.ALLOW:
            return res, None

        try:
            engine = RetrievalEngine(svc.vector_store, svc.embeddings())
            retrieval_query = query
            if is_hindi_script(query):
                # Live-validated (see generation.translate_to_english_for_
                # retrieval's docstring): a Devanagari query embedded as-is
                # scores well below the abstention threshold against
                # English-language content even when the answer is right
                # there; translating for retrieval only recovers most of
                # that gap. Best-effort — falls back to the original text
                # on any failure, never blocks the request.
                try:
                    retrieval_query = svc.generator().translate_to_english_for_retrieval(text=query)
                except Exception as exc:  # noqa: BLE001 - a failed translation must not block retrieval
                    logger.warning("Hindi query translation for retrieval failed: %s", exc)
            pack = engine.retrieve(retrieval_query, tenant_id=tenant_id, top_k=5)
            if retrieval_query != query:
                # The customer's ORIGINAL text is what generation sees and
                # what abstention-language-detection runs on — only the
                # embedding lookup used the translated version.
                pack = pack.model_copy(update={"query": query})

            gate = evaluate_evidence_gate(pack)
            if gate.should_abstain:
                is_dissatisfied = svc.generator().classify_dissatisfaction(query=query)
                answer = build_abstention_answer(pack, gate, shows_dissatisfaction=is_dissatisfied)
            else:
                system_prompt = build_system_prompt(tenant.business_name, tenant.assistant_name, channel=channel)
                user_prompt = build_user_prompt(pack)
                draft = svc.generator().generate(system_prompt=system_prompt, user_prompt=user_prompt)
                answer = validate_llm_draft(draft, pack)
        except Exception:
            svc.usage_limiter.release_reservation(req_id)
            raise

        if answer.status.value in ("insufficient_evidence", "error"):
            svc.usage_limiter.release_reservation(req_id)
        else:
            svc.usage_limiter.finalize_reservation(req_id)

        svc.analytics_store.log_turn(
            tenant_id=tenant_id, session_id=session_id, query=query, answer_status=answer.status.value,
            shows_buying_intent=answer.shows_buying_intent, suggested_handoff=answer.suggested_handoff,
            shows_dissatisfaction=answer.shows_dissatisfaction, channel=channel,
        )

        if answer.shows_dissatisfaction:
            _send_dissatisfaction_alert(tenant=tenant, query=query, answer_text=answer.answer_text, session_id=session_id)

        return res, answer

    # -------------------------------------------------------------- health
    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    # -------------------------------------------------------------- auth
    @app.post("/api/auth/signup")
    def signup(request: SignupRequest) -> dict:
        tenant_id = _slugify_tenant_id(request.business_name)
        try:
            tenant_id = validate_tenant_id(tenant_id)
        except InvalidTenantIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            user = svc.user_store.create_user(
                email=request.email, password=request.password, name=request.name,
                business_name=request.business_name, tenant_id=tenant_id, role="owner",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        new_tenant = svc.tenant_registry.register(
            TenantConfig(
                tenant_id=tenant_id, business_name=request.business_name, owner_email=user.email,
                status=TenantStatus.PROVISIONING,
            )
        )
        _notify_admin_of_signup(new_tenant)

        principal = Principal.owner(user.user_id, tenant_id)
        token = create_access_token(principal, svc.settings)
        return {"access_token": token, "tenant_id": tenant_id, "role": "owner"}

    @app.post("/api/auth/login")
    def login(request: LoginRequest) -> dict:
        user = svc.user_store.get_user_by_email(request.email)
        if user and svc.user_store.verify_password(request.password, user.password_hash, user.salt):
            principal = Principal(principal_id=user.user_id, tenant_id=user.tenant_id, role=user.role)
            token = create_access_token(principal, svc.settings)
            return {"access_token": token, "tenant_id": user.tenant_id, "role": user.role}

        if svc.settings.admin_secret and secrets.compare_digest(request.password, svc.settings.admin_secret):
            principal = Principal.platform_admin(request.email or "admin")
            token = create_access_token(principal, svc.settings)
            return {"access_token": token, "tenant_id": None, "role": "platform_admin"}

        raise HTTPException(status_code=401, detail="Invalid email or password.")

    @app.get("/api/auth/me")
    def me(authorization: str | None = Header(default=None)) -> dict:
        principal = _require(authorization)
        return {"principal_id": principal.principal_id, "tenant_id": principal.tenant_id, "role": principal.role}

    # -------------------------------------------------------------- chat
    @app.post("/api/ask")
    def ask(
        request: AskRequest,
        req: Request,
        tenant_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        principal = _resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.QUERY_ASSISTANT, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        session_id = request.session_id or f"sess_{secrets.token_hex(8)}"

        res, answer = _process_question(
            tenant=tenant, tenant_id=tenant_id, session_id=session_id, query=request.query, channel="web",
        )
        if res.decision != AccessDecision.ALLOW:
            status_code = 429 if res.decision == AccessDecision.RATE_LIMITED else 400
            raise HTTPException(
                status_code=status_code,
                detail={"error": res.reason, "questions_remaining": res.questions_remaining, "questions_limit": res.questions_limit},
            )
        assert answer is not None  # guaranteed by res.decision == ALLOW

        quota_status = svc.usage_limiter.get_session_status(tenant_id, session_id, quota_override=tenant.question_quota)
        whatsapp_url = None
        if answer.suggested_handoff or answer.status.value == "insufficient_evidence":
            wa_number = tenant.whatsapp_number or svc.settings.default_whatsapp_number
            whatsapp_url = _whatsapp_link(wa_number, f"Hi, I was chatting with your assistant about: {request.query}")

        return {
            "session_id": session_id,
            "status": answer.status.value,
            "answer_text": answer.answer_text,
            "abstention_reason": answer.abstention_reason,
            "citations_used": [c.model_dump() for c in answer.citations_used],
            "shows_buying_intent": answer.shows_buying_intent,
            "suggested_handoff": answer.suggested_handoff,
            "shows_dissatisfaction": answer.shows_dissatisfaction,
            "whatsapp_url": whatsapp_url,
            "questions_remaining": quota_status["questions_remaining"],
            "questions_limit": quota_status["questions_limit"],
        }

    # -------------------------------------------------------------- whatsapp
    @app.get("/api/whatsapp/webhook")
    def verify_whatsapp_webhook(req: Request):
        """Meta's one-time (and re-verified-on-change) webhook handshake:
        a GET with query params `hub.mode=subscribe`, `hub.verify_token`,
        `hub.challenge`. Dotted names aren't valid Python identifiers, so
        these are read directly off req.query_params rather than declared
        as typed route parameters. No auth: this is Meta calling us,
        unauthenticated, before the integration exists at all — the
        shared secret IS the verify-token match itself."""
        params = req.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")

        if mode != "subscribe" or not svc.settings.whatsapp_verify_token or token != svc.settings.whatsapp_verify_token:
            raise HTTPException(status_code=403, detail="Webhook verification failed.")
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(challenge or "")

    def _send_admin_bot_message(tenant: TenantConfig, to_wa_id: str, body: str) -> bool:
        """Proactive/reply send to any recognized internal roster number
        (owner, manager, or staff), for task-assignment notices and
        command replies to a specific employee — see
        _notify_management_whatsapp for the "everyone who manages this
        tenant" broadcast variant used by alerts/digest."""
        if not (tenant.whatsapp_phone_number_id and tenant.whatsapp_access_token and to_wa_id):
            return False
        try:
            svc.whatsapp_client().send_text(
                phone_number_id=tenant.whatsapp_phone_number_id, access_token=tenant.whatsapp_access_token,
                to=to_wa_id, body=body,
            )
            return True
        except WhatsAppSendError as exc:
            logger.warning("Admin-bot WhatsApp push failed for tenant %s: %s", tenant.tenant_id, exc)
            return False

    def _find_employee_by_name(tenant_id: str, name_fragment: str) -> Employee | None:
        fragment = name_fragment.strip().lower()
        matches = [e for e in svc.employee_store.list_for_tenant(tenant_id) if fragment in e.name.lower()]
        return matches[0] if len(matches) == 1 else None

    def _find_task_by_short_id(tenant_id: str, short_id: str) -> Task | None:
        short_id = short_id.strip().lower()
        if not short_id:
            return None
        matches = [t for t in svc.task_store.list_for_tenant(tenant_id) if t.task_id.lower().endswith(short_id)]
        return matches[0] if len(matches) == 1 else None

    def _find_lead_by_short_id(tenant_id: str, short_id: str) -> Lead | None:
        """Same short-id-suffix convention as _find_task_by_short_id, so
        an owner can link a task to "the customer who complained" (e.g.
        `assign call back the customer to Ravi for lead a1b2c3`) without
        typing a full lead_id."""
        short_id = short_id.strip().lower()
        if not short_id:
            return None
        matches = [l for l in svc.lead_store.list_for_tenant(tenant_id, limit=1000) if l.lead_id.lower().endswith(short_id)]
        return matches[0] if len(matches) == 1 else None

    def _maybe_verify_outcome_with_customer(tenant: TenantConfig, task: Task) -> None:
        """The "Verified Outcome" step of Insight -> Task -> Owner ->
        Deadline -> Verified Outcome: when a task linked to a real
        customer conversation (customer_facing_lead_id) closes to "done",
        best-effort ping that SAME customer to confirm the issue is
        actually resolved, over the same channel/credentials as every
        other customer-facing send. Fire-and-forget, same honest "no
        webhook, no reconciliation" shape as every other one-way
        WhatsApp send in this app — never blocks task completion."""
        if not task.customer_facing_lead_id:
            return
        if not (tenant.whatsapp_phone_number_id and tenant.whatsapp_access_token):
            return
        lead = svc.lead_store.get(tenant.tenant_id, task.customer_facing_lead_id)
        if not (lead and lead.phone):
            return
        try:
            svc.whatsapp_client().send_text(
                phone_number_id=tenant.whatsapp_phone_number_id, access_token=tenant.whatsapp_access_token,
                to=lead.phone,
                body=f"Hi! Following up on your recent request with {tenant.business_name} — "
                     "has this been resolved to your satisfaction? Just reply to let us know.",
            )
        except WhatsAppSendError as exc:
            logger.warning("Verified-outcome ping failed for tenant %s task %s: %s", tenant.tenant_id, task.task_id, exc)

    def _render_task_list(tasks: list[Task], employees_by_id: dict[str, Employee], *, show_assignee: bool) -> str:
        if not tasks:
            return "No open tasks. 🎉"
        lines = []
        for t in tasks:
            due = f" (due {t.due_at[:16].replace('T', ' ')} UTC)" if t.due_at else ""
            who = ""
            if show_assignee and t.assigned_to_employee_id in employees_by_id:
                who = f" — {employees_by_id[t.assigned_to_employee_id].name}"
            lines.append(f"[{_short_task_id(t.task_id)}] {t.title}{due}{who} ({t.status})")
        return "\n".join(lines)

    def _overdue_lines_by_employee(tasks: list[Task], employees_by_id: dict[str, Employee]) -> list[str]:
        """One line per employee with overdue work — reused by the
        `overdue` command, the daily digest, and the `scorecard` command,
        so "who owns what's overdue" is computed exactly once."""
        by_employee: dict[str, list[Task]] = {}
        for t in tasks:
            by_employee.setdefault(t.assigned_to_employee_id, []).append(t)
        lines = []
        for emp_id, emp_tasks in by_employee.items():
            name = employees_by_id[emp_id].name if emp_id in employees_by_id else "Unassigned"
            titles = "; ".join(f"{t.title} [{_short_task_id(t.task_id)}]" for t in emp_tasks)
            lines.append(f"{name}: {len(emp_tasks)} overdue — {titles}")
        return lines

    def _render_overdue_list(tasks: list[Task], employees_by_id: dict[str, Employee]) -> str:
        if not tasks:
            return "No overdue tasks. 🎉"
        return "⚠️ Overdue:\n" + "\n".join(_overdue_lines_by_employee(tasks, employees_by_id))

    def _recurring_feedback_lines(tenant_id: str, *, since_iso: str | None = None) -> list[str]:
        """Themes that have crossed the "this is a pattern, not a one-off"
        threshold — reused by the digest, the action brief, and the
        `scorecard` command."""
        lines = []
        for s in svc.feedback_store.summarize_by_theme(tenant_id, since_iso=since_iso):
            if s.count >= RECURRING_FEEDBACK_THRESHOLD:
                label = _FEEDBACK_THEME_LABELS.get(s.theme, s.theme)
                lines.append(f"{label}: {s.count} report(s), {s.negative_count} negative")
        return lines

    def _resolve_theme_key(fragment: str) -> str | None:
        """Matches a human-typed theme reference ("software/tools",
        "Software", the raw key "software_or_tools") against the fixed
        FEEDBACK_THEMES taxonomy — never a free-form new theme, so SOPs
        stay attached to the same buckets FeedbackStore aggregates by."""
        clean = fragment.strip().lower()
        for key, label in _FEEDBACK_THEME_LABELS.items():
            if clean == key or clean == label.lower() or clean in label.lower():
                return key
        return None

    def _render_feedback_theme_summary(tenant_id: str, summaries: list) -> str:
        if not summaries:
            return "No feedback logged yet."
        lines = ["📋 Recurring themes:"]
        for s in summaries[:10]:
            label = _FEEDBACK_THEME_LABELS.get(s.theme, s.theme)
            sop = svc.sop_store.get_for_theme(tenant_id, s.theme)
            status = " ✅ has approved guidance" if sop else ""
            lines.append(f"{label}: {s.count} report(s), {s.negative_count} negative (last {s.most_recent_at[:10]}){status}")
        return "\n".join(lines)

    def _record_feedback(
        tenant: TenantConfig, employee: Employee, feedback_text: str, message_id: str | None, reply
    ) -> None:
        """Shared by the deterministic `feedback <text>` command and the
        NL fallback (see _handle_admin_bot_message) — one classification
        + storage + SOP-lookup + alert path, however the intent was
        detected."""
        classification = svc.generator().classify_feedback_sentiment(text=feedback_text)
        svc.feedback_store.record(
            tenant_id=tenant.tenant_id, employee_id=employee.employee_id, raw_text=feedback_text,
            sentiment=classification.sentiment, theme=classification.theme, urgency=classification.urgency,
            root_cause_hint=classification.root_cause_hint, suggested_action=classification.suggested_action,
            source_message_id=message_id,
        )
        existing_sop = svc.sop_store.get_for_theme(tenant.tenant_id, classification.theme)
        sop_note = f'\n\nWe know about this — current guidance: "{existing_sop.text}"' if existing_sop else ""
        if classification.urgency == "high":
            reply(f"Got it — this sounds urgent, I've flagged it to management right away.{sop_note}")
            _send_urgent_feedback_alert(tenant, employee, feedback_text, classification)
        else:
            reply(f"Got it — logged, thanks. I'll flag it to management if it's part of a pattern.{sop_note}")

    def _missed_opportunity_leads(tenant_id: str, *, since_iso: str | None = None) -> list[Lead]:
        """A lead whose conversation showed real buying intent
        (AnalyticsStore, joined by session_id) but who never progressed
        past "new"/"reengaged" — a stronger, more specific signal than
        the generic re-engagement candidate list, since it's grounded in
        an actual expressed interest, not just silence after first
        contact."""
        intent_sessions = svc.analytics_store.session_ids_with_buying_intent(tenant_id, since_iso=since_iso)
        if not intent_sessions:
            return []
        leads = svc.lead_store.list_for_tenant(tenant_id, since_iso=since_iso, limit=1000)
        return [l for l in leads if l.session_id in intent_sessions and lead_stage(l) in ("new", "reengaged")]

    def _render_missed_opportunity_lines(leads: list[Lead]) -> list[str]:
        return [f"{l.name or l.phone or l.email or l.lead_id[-6:]} — showed interest, no booking yet" for l in leads[:10]]

    def _compute_trend(current: int, prior: int) -> str:
        """Deterministic window-over-window comparison — no LLM, no
        estimation. `prior` is the equal-length window immediately
        before the current one, so "trend" always means "vs the same
        span of time right before this one," never vs. an arbitrary
        baseline."""
        if prior == 0:
            return "▲ new" if current > 0 else "→ flat"
        pct = round(100 * (current - prior) / prior)
        if pct > 0:
            return f"▲ {pct}%"
        if pct < 0:
            return f"▼ {abs(pct)}%"
        return "→ flat"

    def _business_health_snapshot(tenant: TenantConfig, *, window_hours: int) -> dict:
        """One place computing the transparent, component-based health
        view used by both the on-demand `scorecard` command and
        `GET /api/business-health` — never a single opaque score, always
        named numbers an owner can trace back to a real source. Also
        computes deterministic window-over-window trends for the
        headline numbers (see _compute_trend) — comparison, not
        prediction."""
        now = time.time()
        since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - window_hours * 3600))
        prior_since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 2 * window_hours * 3600))

        all_tasks = [t for t in svc.task_store.list_for_tenant(tenant.tenant_id) if t.created_at >= since_iso]
        done_tasks = [t for t in all_tasks if t.status == "done"]
        prior_done_count = len([
            t for t in svc.task_store.list_for_tenant(tenant.tenant_id)
            if prior_since_iso <= t.created_at < since_iso and t.status == "done"
        ])
        employees_by_id = {e.employee_id: e for e in svc.employee_store.list_for_tenant(tenant.tenant_id)}
        overdue_tasks = svc.task_store.list_overdue(tenant.tenant_id)
        analytics = svc.analytics_store.summary_for_tenant(tenant.tenant_id, since_iso=since_iso)
        recurring_themes = [
            s for s in svc.feedback_store.summarize_by_theme(tenant.tenant_id, since_iso=since_iso)
            if s.count >= RECURRING_FEEDBACK_THRESHOLD
        ]
        themes_with_sop = sum(1 for s in recurring_themes if svc.sop_store.get_for_theme(tenant.tenant_id, s.theme))
        missed_opportunities = _missed_opportunity_leads(tenant.tenant_id, since_iso=since_iso)
        leads_in_window = svc.lead_store.list_for_tenant(tenant.tenant_id, since_iso=since_iso, limit=1000)
        leads_prior_count = len([
            l for l in svc.lead_store.list_for_tenant(tenant.tenant_id, since_iso=prior_since_iso, limit=2000)
            if l.created_at < since_iso
        ])
        converted_count = svc.lead_store.count_converted(tenant.tenant_id, since_iso=since_iso)
        converted_prior_and_current = svc.lead_store.count_converted(tenant.tenant_id, since_iso=prior_since_iso)
        converted_prior_only = converted_prior_and_current - converted_count
        confirmed_revenue = svc.lead_store.sum_confirmed_revenue(tenant.tenant_id, since_iso=since_iso)
        revenue_prior_and_current = svc.lead_store.sum_confirmed_revenue(tenant.tenant_id, since_iso=prior_since_iso)
        revenue_prior_only = revenue_prior_and_current - confirmed_revenue
        feedback_current_total = sum(s.count for s in svc.feedback_store.summarize_by_theme(tenant.tenant_id, since_iso=since_iso))
        feedback_prior_and_current_total = sum(
            s.count for s in svc.feedback_store.summarize_by_theme(tenant.tenant_id, since_iso=prior_since_iso)
        )
        feedback_prior_only = feedback_prior_and_current_total - feedback_current_total

        return {
            "tasks_assigned": len(all_tasks),
            "tasks_done": len(done_tasks),
            "tasks_overdue": len(overdue_tasks),
            "overdue_lines": _overdue_lines_by_employee(overdue_tasks, employees_by_id),
            "recurring_feedback_lines": _recurring_feedback_lines(tenant.tenant_id, since_iso=since_iso),
            "recurring_themes_total": len(recurring_themes),
            "recurring_themes_with_sop": themes_with_sop,
            "questions_asked": analytics.total_questions,
            "questions_answered": analytics.answered_count,
            "dissatisfaction_count": analytics.dissatisfaction_count,
            "buying_intent_count": analytics.buying_intent_count,
            "missed_opportunity_lines": _render_missed_opportunity_lines(missed_opportunities),
            "missed_opportunity_count": len(missed_opportunities),
            "leads_count": len(leads_in_window),
            "converted_count": converted_count,
            "conversion_rate_pct": round(100 * converted_count / len(leads_in_window)) if leads_in_window else 0,
            "confirmed_revenue_inr": confirmed_revenue,
            "trends": {
                "leads": _compute_trend(len(leads_in_window), leads_prior_count),
                "tasks_done": _compute_trend(len(done_tasks), prior_done_count),
                "converted": _compute_trend(converted_count, converted_prior_only),
                "revenue_inr": _compute_trend(confirmed_revenue, revenue_prior_only),
                "feedback_volume": _compute_trend(feedback_current_total, feedback_prior_only),
            },
        }

    def _render_business_health(snapshot: dict, *, window_label: str) -> str:
        trends = snapshot["trends"]
        lines = [
            f"📈 Business health — {window_label}",
            "",
            f"Tasks: {snapshot['tasks_done']}/{snapshot['tasks_assigned']} completed "
            f"({trends['tasks_done']} vs prior period), {snapshot['tasks_overdue']} overdue",
        ]
        lines.extend(f"  • {line}" for line in snapshot["overdue_lines"])
        lines.append(f"Customer questions: {snapshot['questions_asked']} ({snapshot['questions_answered']} answered)")
        if snapshot["dissatisfaction_count"]:
            lines.append(f"⚠️ {snapshot['dissatisfaction_count']} customer complaint(s)")
        if snapshot["buying_intent_count"]:
            lines.append(f"{snapshot['buying_intent_count']} showed buying interest")
        lines.append(
            f"Leads: {snapshot['leads_count']} ({trends['leads']}), {snapshot['converted_count']} converted "
            f"({trends['converted']}) ({snapshot['conversion_rate_pct']}%) — confirmed revenue "
            f"₹{snapshot['confirmed_revenue_inr']} ({trends['revenue_inr']})"
        )
        if snapshot["missed_opportunity_lines"]:
            noun = "opportunity" if snapshot["missed_opportunity_count"] == 1 else "opportunities"
            lines.append(f"⚠️ {snapshot['missed_opportunity_count']} missed {noun}:")
            lines.extend(f"  • {line}" for line in snapshot["missed_opportunity_lines"])
        if snapshot["recurring_feedback_lines"]:
            lines.append("")
            lines.append(f"Recurring employee feedback ({trends['feedback_volume']} in volume vs prior period):")
            lines.extend(f"  • {line}" for line in snapshot["recurring_feedback_lines"])
            lines.append(f"  ({snapshot['recurring_themes_with_sop']}/{snapshot['recurring_themes_total']} have approved guidance)")
        else:
            lines.append("No recurring employee issues.")
        return "\n".join(lines)

    _TIMELINE_DESCRIPTIONS = {
        "employee_added": "{actor} added an employee to the roster",
        "role_changed": "{actor} changed an employee's role to {role}",
        "employee_deactivated": "{actor} deactivated an employee",
        "task_assigned": "{actor} assigned a task",
        "task_reassigned": "{actor} reassigned a task",
        "task_started": "{actor} started a task",
        "task_done": "{actor} completed a task",
        "task_blocked": "{actor} marked a task blocked",
        "task_cancelled": "{actor} cancelled a task",
        "task_approved": "{actor} approved a task",
        "task_rejected": "{actor} sent a task back for rework",
        "sop_approved": "{actor} approved team guidance for a theme",
        "feedback_resolved": "{actor} marked a feedback item resolved",
        "deposit_confirmed_paid": "{actor} confirmed a deposit of ₹{amount_inr}",
        "appointment_outcome_recorded": "{actor} recorded an appointment as {outcome}",
    }

    def _render_timeline_line(entry, employees_by_id: dict[str, Employee]) -> str:
        actor = "Someone"
        if entry.actor_employee_id and entry.actor_employee_id in employees_by_id:
            actor = employees_by_id[entry.actor_employee_id].name
        elif entry.actor_employee_id is None:
            actor = "The dashboard"
        template = _TIMELINE_DESCRIPTIONS.get(entry.action, entry.action)
        try:
            description = template.format(actor=actor, **entry.metadata)
        except (KeyError, IndexError):
            description = template.format(actor=actor, role="?", amount_inr="?", outcome="?")
        return f"{entry.created_at[:16].replace('T', ' ')} — {description}"

    def _render_timeline(entries, employees_by_id: dict[str, Employee]) -> str:
        if not entries:
            return "No recorded activity yet."
        return "🕒 Recent activity:\n" + "\n".join(_render_timeline_line(e, employees_by_id) for e in entries)

    def _daily_pulse_text(tenant: TenantConfig, employee: Employee) -> str:
        """Reuses the exact same summary as the daily digest; deliberately
        skips the LLM action-brief call (see generate_action_brief) to
        keep an on-demand reply fast and free, unlike the once-a-day push
        which can afford the extra call. Owner/manager get the full
        tenant-wide pulse (extended with named overdue-task ownership,
        not just a count); staff get their own open-task view only."""
        since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - svc.settings.digest_window_hours * 3600))
        window_label = "today" if svc.settings.digest_window_hours <= 24 else f"the last {svc.settings.digest_window_hours}h"
        if employee.role in ("owner", "manager"):
            new_leads = svc.lead_store.list_for_tenant(tenant.tenant_id, since_iso=since_iso)
            analytics = svc.analytics_store.summary_for_tenant(tenant.tenant_id, since_iso=since_iso)
            summary = render_owner_whatsapp_summary(
                tenant, new_leads=new_leads, analytics=analytics,
                open_gaps_count=len(svc.analytics_store.list_open_gaps(tenant.tenant_id)),
                window_label=window_label, action_items=None,
            )
            employees_by_id = {e.employee_id: e for e in svc.employee_store.list_for_tenant(tenant.tenant_id)}
            overdue = svc.task_store.list_overdue(tenant.tenant_id)
            return summary + "\n\n" + _render_overdue_list(overdue, employees_by_id)
        my_open = svc.task_store.list_open_for_tenant(tenant.tenant_id, assigned_to_employee_id=employee.employee_id)
        return (
            f"👋 Hi {employee.name}! You have {len(my_open)} open task(s).\n"
            + _render_task_list(my_open, {}, show_assignee=False)
        )

    def _find_task_by_title_fragment(tenant_id: str, employee: Employee, fragment: str, can_manage: bool) -> Task | None:
        """Fuzzy title match against the relevant open-task pool — used
        by the NL fallback's task_status_update intent, which gets a
        free-text description ("the shelf restock") rather than an exact
        short id. Word-set containment, not literal substring: an LLM's
        task_reference commonly reorders or drops words relative to the
        original title ("shelf restock" for a task titled "restock shelf
        3"), so a strict substring check would wrongly report no match
        on cases a person would obviously consider a match. Staff only
        match against their own open tasks, same scoping as the
        deterministic `my tasks`/`tasks` commands."""
        fragment_words = set(fragment.strip().lower().split())
        if not fragment_words:
            return None
        candidates = (
            svc.task_store.list_open_for_tenant(tenant_id)
            if can_manage
            else svc.task_store.list_open_for_tenant(tenant_id, assigned_to_employee_id=employee.employee_id)
        )
        matches = [t for t in candidates if fragment_words.issubset(set(t.title.lower().split()))]
        return matches[0] if len(matches) == 1 else None

    def _try_deterministic_admin_command(
        tenant: TenantConfig, employee: Employee, raw: str, clean: str, can_manage: bool,
        message_id: str | None, reply,
    ) -> bool:
        """Every EXACT-syntax admin-bot command — free, instant, 100%
        predictable. Returns True if `raw`/`clean` matched something
        (and a reply was already sent), False if nothing matched, in
        which case the caller (_handle_admin_bot_message) falls back to
        NL classification. Also reused BY the NL path itself: once an
        intent is extracted, its slots are reformatted into the exact
        syntax below and replayed through this same function, so every
        permission check / lookup / store call / notification exists in
        exactly one place regardless of how the command was phrased."""
        if clean in ("today", "status"):
            reply(_daily_pulse_text(tenant, employee))
            return True

        if clean == "my tasks":
            my_tasks = svc.task_store.list_open_for_tenant(tenant.tenant_id, assigned_to_employee_id=employee.employee_id)
            reply(_render_task_list(my_tasks, {}, show_assignee=False))
            return True

        if clean == "tasks":
            employees_by_id = {e.employee_id: e for e in svc.employee_store.list_for_tenant(tenant.tenant_id)}
            tasks = (
                svc.task_store.list_open_for_tenant(tenant.tenant_id)
                if can_manage
                else svc.task_store.list_open_for_tenant(tenant.tenant_id, assigned_to_employee_id=employee.employee_id)
            )
            reply(_render_task_list(tasks, employees_by_id, show_assignee=can_manage))
            return True

        if clean == "overdue":
            employees_by_id = {e.employee_id: e for e in svc.employee_store.list_for_tenant(tenant.tenant_id)}
            overdue = svc.task_store.list_overdue(tenant.tenant_id)
            if not can_manage:
                overdue = [t for t in overdue if t.assigned_to_employee_id == employee.employee_id]
            reply(_render_overdue_list(overdue, employees_by_id))
            return True

        if clean in ("help", "commands"):
            reply(_admin_bot_help_text())
            return True

        if clean == "feedback themes":
            if not can_manage:
                reply("Only an owner or manager can view feedback themes.")
                return True
            reply(_render_feedback_theme_summary(tenant.tenant_id, svc.feedback_store.summarize_by_theme(tenant.tenant_id)))
            return True

        suggest_sop_match = _ADMIN_SUGGEST_SOP_RE.match(raw)
        if suggest_sop_match:
            if employee.role != "owner":
                reply("Only the owner can request a suggested guidance note.")
                return True
            theme_key = _resolve_theme_key(suggest_sop_match.group(1))
            if theme_key is None:
                reply(f'Couldn\'t match "{suggest_sop_match.group(1)}" to a feedback theme. Try "feedback themes" to see the list.')
                return True
            recent_texts = [f.raw_text for f in svc.feedback_store.list_for_tenant(tenant.tenant_id, theme=theme_key, limit=5)]
            if not recent_texts:
                reply(f'No feedback reports found yet for "{_FEEDBACK_THEME_LABELS.get(theme_key, theme_key)}".')
                return True
            try:
                draft = svc.generator().draft_sop_note(
                    theme_label=_FEEDBACK_THEME_LABELS.get(theme_key, theme_key), recent_feedback_texts=recent_texts,
                )
            except Exception as exc:  # noqa: BLE001 - a failed draft must not crash the command
                logger.warning("SOP draft generation failed for tenant %s: %s", tenant.tenant_id, exc)
                reply("Couldn't generate a draft right now — try again shortly, or write your own with \"approve sop\".")
                return True
            reply(
                f'📝 Draft guidance for "{_FEEDBACK_THEME_LABELS.get(theme_key, theme_key)}":\n\n"{draft}"\n\n'
                f'Edit as needed, then approve with:\napprove sop {theme_key}: {draft}'
            )
            return True

        sop_match = _ADMIN_SOP_RE.match(raw)
        if sop_match:
            if employee.role != "owner":
                reply("Only the owner can approve team guidance for a theme.")
                return True
            theme_fragment, note_text = sop_match.group(1), sop_match.group(2)
            theme_key = _resolve_theme_key(theme_fragment)
            if theme_key is None:
                reply(f'Couldn\'t match "{theme_fragment}" to a feedback theme. Try "feedback themes" to see the list.')
                return True
            svc.sop_store.approve(
                tenant_id=tenant.tenant_id, theme=theme_key, text=note_text.strip(),
                approved_by_employee_id=employee.employee_id,
            )
            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="sop_approved",
                target_type="sop", target_id=theme_key,
            )
            reply(f'✅ Saved guidance for "{_FEEDBACK_THEME_LABELS.get(theme_key, theme_key)}". Employees reporting this will now see it.')
            return True

        mark_paid_match = _ADMIN_MARK_PAID_RE.match(raw)
        if mark_paid_match:
            lead_short_id, amount_text = mark_paid_match.group(1), mark_paid_match.group(2)
            lead = _find_lead_by_short_id(tenant.tenant_id, lead_short_id)
            if lead is None:
                reply(f'Couldn\'t find a customer matching "{lead_short_id}".')
                return True
            amount = int(amount_text) if amount_text else tenant.deposit_amount_inr
            if not amount:
                reply("No amount given and no deposit amount configured. Try \"mark paid <id> <amount>\".")
                return True
            try:
                svc.lead_store.mark_deposit_paid(tenant.tenant_id, lead.lead_id, amount)
            except ValueError as exc:
                reply(str(exc))
                return True
            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="deposit_confirmed_paid",
                target_type="lead", target_id=lead.lead_id, metadata={"amount_inr": amount},
            )
            reply(f"✅ Recorded ₹{amount} received from {lead.name or lead.phone or lead.email}.")
            return True

        mark_outcome_match = _ADMIN_MARK_OUTCOME_RE.match(raw)
        if mark_outcome_match:
            outcome = _OUTCOME_ALIASES[mark_outcome_match.group(1).lower()]
            lead_short_id = mark_outcome_match.group(2)
            lead = _find_lead_by_short_id(tenant.tenant_id, lead_short_id)
            if lead is None:
                reply(f'Couldn\'t find a customer matching "{lead_short_id}".')
                return True
            svc.lead_store.record_appointment_outcome(tenant.tenant_id, lead.lead_id, outcome)
            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="appointment_outcome_recorded",
                target_type="lead", target_id=lead.lead_id, metadata={"outcome": outcome},
            )
            reply(f"✅ Recorded {lead.name or lead.phone or lead.email}'s appointment as {outcome.replace('_', ' ')}.")
            return True

        if clean in ("scorecard", "health"):
            if not can_manage:
                reply("Only an owner or manager can view the business scorecard.")
                return True
            window_label = "today" if svc.settings.digest_window_hours <= 24 else f"the last {svc.settings.digest_window_hours}h"
            snapshot = _business_health_snapshot(tenant, window_hours=svc.settings.digest_window_hours)
            reply(_render_business_health(snapshot, window_label=window_label))
            return True

        if clean == "timeline":
            if not can_manage:
                reply("Only an owner or manager can view the activity timeline.")
                return True
            employees_by_id = {e.employee_id: e for e in svc.employee_store.list_for_tenant(tenant.tenant_id)}
            entries = svc.audit_log.list_for_tenant(tenant.tenant_id, limit=20)
            reply(_render_timeline(entries, employees_by_id))
            return True

        feedback_match = _ADMIN_FEEDBACK_RE.match(raw)
        if feedback_match:
            _record_feedback(tenant, employee, feedback_match.group(1).strip(), message_id, reply)
            return True

        assign_match = _ADMIN_ASSIGN_RE.match(raw)
        if assign_match:
            if not can_manage:
                reply("Only an owner or manager can assign tasks.")
                return True
            title, name_fragment, lead_short_id, when = (
                assign_match.group(1), assign_match.group(2), assign_match.group(3), assign_match.group(4)
            )
            assignee = _find_employee_by_name(tenant.tenant_id, name_fragment)
            if assignee is None:
                reply(f'Couldn\'t find exactly one active employee matching "{name_fragment}". Check the roster and try again.')
                return True
            customer_facing_lead_id = None
            if lead_short_id:
                lead = _find_lead_by_short_id(tenant.tenant_id, lead_short_id)
                if lead is None:
                    reply(f'Couldn\'t find a customer matching lead id "{lead_short_id}".')
                    return True
                customer_facing_lead_id = lead.lead_id
            due_at = None
            if when:
                try:
                    due_at = _parse_appointment_to_utc(when.strip())
                except ValueError:
                    reply(f'Couldn\'t understand the due date "{when}". Use YYYY-MM-DD or YYYY-MM-DD HH:MM.')
                    return True
            task = svc.task_store.create(
                tenant_id=tenant.tenant_id, title=title.strip(), assigned_to_employee_id=assignee.employee_id,
                assigned_by_employee_id=employee.employee_id, due_at=due_at,
                customer_facing_lead_id=customer_facing_lead_id,
            )
            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="task_assigned",
                target_type="task", target_id=task.task_id, metadata={"assigned_to": assignee.employee_id},
            )
            due_note = f" — due {due_at[:16].replace('T', ' ')} UTC" if due_at else ""
            reply(f'✅ Task [{_short_task_id(task.task_id)}] created for {assignee.name}: "{task.title}"{due_note}.')
            _send_admin_bot_message(
                tenant, assignee.whatsapp_number,
                f'📋 New task from {employee.name}: "{task.title}"{due_note}. '
                f'Reply "done {_short_task_id(task.task_id)}" when finished.',
            )
            return True

        reassign_match = _ADMIN_REASSIGN_RE.match(raw)
        if reassign_match:
            if not can_manage:
                reply("Only an owner or manager can reassign tasks.")
                return True
            short_id, name_fragment = reassign_match.group(1), reassign_match.group(2)
            task = _find_task_by_short_id(tenant.tenant_id, short_id)
            if task is None:
                reply(f'Couldn\'t find a task matching "{short_id}".')
                return True
            assignee = _find_employee_by_name(tenant.tenant_id, name_fragment)
            if assignee is None:
                reply(f'Couldn\'t find exactly one active employee matching "{name_fragment}".')
                return True
            svc.task_store.reassign(tenant.tenant_id, task.task_id, assignee.employee_id)
            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="task_reassigned",
                target_type="task", target_id=task.task_id, metadata={"to": assignee.employee_id},
            )
            reply(f'🔁 Reassigned "{task.title}" to {assignee.name}.')
            return True

        parts = raw.split(None, 1)
        verb = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        if verb in _ADMIN_STATUS_VERBS:
            id_parts = rest.split(None, 1)
            short_id = id_parts[0] if id_parts else ""
            reason = id_parts[1] if len(id_parts) > 1 else None
            task = _find_task_by_short_id(tenant.tenant_id, short_id)
            if task is None:
                reply(f'Couldn\'t find a task matching "{short_id}". Try "tasks" to see open work.')
                return True

            if verb in ("approve", "reject") and not can_manage:
                reply("Only an owner or manager can approve or reject a task.")
                return True
            if verb in ("start", "done", "blocked") and not (can_manage or task.assigned_to_employee_id == employee.employee_id):
                reply("You can only update the status of tasks assigned to you.")
                return True

            if verb == "start":
                svc.task_store.update_status(tenant.tenant_id, task.task_id, "in_progress")
                svc.audit_log.record(
                    tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="task_started",
                    target_type="task", target_id=task.task_id,
                )
                reply(f'▶️ Marked "{task.title}" as in progress.')
            elif verb == "done":
                assigner = svc.employee_store.get(tenant.tenant_id, task.assigned_by_employee_id)
                if task.approval_required:
                    svc.task_store.update_status(tenant.tenant_id, task.task_id, "awaiting_approval")
                    reply(f'✅ "{task.title}" marked ready for approval.')
                    if assigner:
                        _send_admin_bot_message(
                            tenant, assigner.whatsapp_number,
                            f'👀 {employee.name} finished "{task.title}" — reply "approve {short_id}" '
                            f'or "reject {short_id} <reason>".',
                        )
                else:
                    svc.task_store.update_status(tenant.tenant_id, task.task_id, "done")
                    svc.audit_log.record(
                        tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="task_done",
                        target_type="task", target_id=task.task_id,
                    )
                    reply(f'🎉 Marked "{task.title}" as done. Nice work.')
                    if assigner and assigner.employee_id != employee.employee_id:
                        _send_admin_bot_message(tenant, assigner.whatsapp_number, f'✅ {employee.name} finished "{task.title}".')
                    _maybe_verify_outcome_with_customer(tenant, task)
            elif verb == "blocked":
                svc.task_store.update_status(tenant.tenant_id, task.task_id, "blocked", block_reason=reason)
                svc.audit_log.record(
                    tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="task_blocked",
                    target_type="task", target_id=task.task_id, metadata={"reason": reason},
                )
                reply(f'🚧 Marked "{task.title}" as blocked' + (f": {reason}" if reason else "") + ".")
            elif verb == "cancel":
                svc.task_store.update_status(tenant.tenant_id, task.task_id, "cancelled")
                svc.audit_log.record(
                    tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="task_cancelled",
                    target_type="task", target_id=task.task_id,
                )
                reply(f'🗑️ Cancelled "{task.title}".')
            elif verb == "approve":
                svc.task_store.approve(tenant.tenant_id, task.task_id, employee.employee_id)
                svc.audit_log.record(
                    tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="task_approved",
                    target_type="task", target_id=task.task_id,
                )
                reply(f'✅ Approved "{task.title}".')
                _maybe_verify_outcome_with_customer(tenant, task)
            elif verb == "reject":
                svc.task_store.reject(tenant.tenant_id, task.task_id, reason=reason)
                svc.audit_log.record(
                    tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="task_rejected",
                    target_type="task", target_id=task.task_id, metadata={"reason": reason},
                )
                reply(f'↩️ Sent "{task.title}" back to in progress' + (f": {reason}" if reason else "") + ".")
            return True

        return False

    def _handle_nl_admin_command(
        tenant: TenantConfig, employee: Employee, raw: str, can_manage: bool, message_id: str | None, reply,
    ) -> None:
        """Fallback for messages that matched no deterministic syntax —
        one narrow structured-output call extracts intent + slots, then
        every action is replayed through _try_deterministic_admin_command
        (assign/status-update) or the shared _record_feedback helper
        (feedback), so nothing here re-implements permission checks,
        lookups, or notifications."""
        today_iso = time.strftime("%Y-%m-%d", time.gmtime())
        try:
            classification = svc.generator().classify_employee_message(
                text=raw, current_date_iso=today_iso, employee_role=employee.role,
            )
        except Exception as exc:  # noqa: BLE001 - never break the webhook turn over a best-effort NL classification
            logger.warning("NL admin-bot classification failed for tenant %s: %s", tenant.tenant_id, exc)
            reply(_admin_bot_help_text())
            return

        if classification.intent == "feedback" and classification.feedback_text:
            _record_feedback(tenant, employee, classification.feedback_text, message_id, reply)
            return

        if classification.intent == "assign_task" and classification.task_title and classification.assignee_name:
            synthetic = f"assign {classification.task_title} to {classification.assignee_name}"
            if classification.due_date_iso:
                synthetic += f" by {classification.due_date_iso}"
            if _try_deterministic_admin_command(tenant, employee, synthetic, synthetic.lower(), can_manage, message_id, reply):
                return
            reply(_admin_bot_help_text())
            return

        if classification.intent == "task_status_update" and classification.task_reference and classification.new_status:
            task = _find_task_by_title_fragment(tenant.tenant_id, employee, classification.task_reference, can_manage)
            if task is None:
                reply(
                    f'I think you mean a task about "{classification.task_reference}" but couldn\'t find exactly '
                    'one match. Try "tasks" to see open work and reply with its exact id.'
                )
                return
            synthetic = f"{classification.new_status} {_short_task_id(task.task_id)}"
            if _try_deterministic_admin_command(tenant, employee, synthetic, synthetic.lower(), can_manage, message_id, reply):
                return
            reply(_admin_bot_help_text())
            return

        if classification.intent == "report_request" and classification.report_type:
            mapped = "feedback themes" if classification.report_type == "feedback_themes" else classification.report_type
            if _try_deterministic_admin_command(tenant, employee, mapped, mapped, can_manage, message_id, reply):
                return

        reply(_admin_bot_help_text())

    def _handle_admin_bot_message(
        tenant: TenantConfig, employee: Employee, from_wa_id: str, text: str, *, message_id: str | None = None
    ) -> None:
        """The admin bot's command dispatcher for anyone in the tenant's
        employee roster (owner, manager, or staff). Deterministic keyword
        parsing is tried first (free, instant, exact); only a message
        that matches nothing pays for an NL classification call — see
        _try_deterministic_admin_command and _handle_nl_admin_command."""
        if not (tenant.whatsapp_phone_number_id and tenant.whatsapp_access_token):
            return
        raw = text.strip()
        clean = raw.rstrip("?!. ").lower()
        can_manage = employee.role in ("owner", "manager")

        def reply(body: str) -> None:
            _send_admin_bot_message(tenant, from_wa_id, body)

        if _try_deterministic_admin_command(tenant, employee, raw, clean, can_manage, message_id, reply):
            return
        _handle_nl_admin_command(tenant, employee, raw, can_manage, message_id, reply)

    @app.post("/api/whatsapp/webhook")
    async def receive_whatsapp_webhook(req: Request) -> dict:
        """Inbound WhatsApp messages, from Meta. Routes to the owning
        tenant by phone_number_id (never anything client-suppliable —
        see TenantRegistry.find_by_whatsapp_phone_number_id), then runs
        the exact same grounded-answer pipeline as the website widget via
        _process_question, so a business's knowledge base, lead capture,
        and complaint alerts behave identically on both channels.

        Always returns 200 for a validly-signed request, even when a
        specific tenant/message fails downstream — Meta interprets a
        non-200 as "redeliver this", and retry-storming ourselves over a
        single tenant's misconfiguration or a transient OpenAI error
        would only make things worse. A genuinely invalid signature (not
        really from Meta) is the one case that gets rejected outright.
        """
        raw_body = await req.body()
        signature = req.headers.get("x-hub-signature-256")
        if not svc.settings.whatsapp_app_secret or not verify_webhook_signature(
            raw_body=raw_body, signature_header=signature, app_secret=svc.settings.whatsapp_app_secret
        ):
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")

        import json

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"status": "ignored", "reason": "invalid_json"}

        for msg in parse_webhook_payload(payload):
            try:
                tenant = svc.tenant_registry.find_by_whatsapp_phone_number_id(msg.phone_number_id)
                if tenant is None:
                    logger.warning("WhatsApp message for unknown phone_number_id %s", msg.phone_number_id)
                    continue
                try:
                    authorize(None, TenantAction.QUERY_ASSISTANT, target_tenant_id=tenant.tenant_id, registry=svc.tenant_registry)
                except UnauthorizedError:
                    continue  # tenant exists but isn't ACTIVE — don't answer

                if not svc.whatsapp_inbox.claim(msg.message_id, tenant.tenant_id):
                    continue  # already processed (Meta redelivery)

                # Bootstrap: a tenant configured before the employee roster
                # existed gets its owner_whatsapp_number auto-registered as
                # an owner-role roster row, so it keeps working with zero
                # action on the owner's part (see EmployeeStore.ensure_owner_bootstrap).
                svc.employee_store.ensure_owner_bootstrap(tenant.tenant_id, tenant.owner_whatsapp_number)
                employee = svc.employee_store.find_by_whatsapp(tenant.tenant_id, msg.wa_id)
                if employee is not None:
                    _handle_admin_bot_message(tenant, employee, msg.wa_id, msg.text, message_id=msg.message_id)
                    continue  # internal admin-bot message, not a customer — no lead, no RAG, no quota spent

                session_id = f"wa_{msg.wa_id}"
                if not svc.lead_store.exists_for_session(tenant.tenant_id, session_id):
                    # A real, Meta-verified phone number on first contact —
                    # richer than the web widget's optional, self-typed
                    # lead form. Best-effort: a lead-capture hiccup must
                    # never stop the customer from getting their answer.
                    try:
                        svc.lead_store.create(
                            tenant_id=tenant.tenant_id, session_id=session_id, name=msg.contact_name,
                            phone=msg.wa_id, message=msg.text, source="whatsapp",
                        )
                    except ValueError as exc:
                        logger.warning("WhatsApp lead capture failed for tenant %s: %s", tenant.tenant_id, exc)

                res, answer = _process_question(
                    tenant=tenant, tenant_id=tenant.tenant_id, session_id=session_id, query=msg.text, channel="whatsapp",
                )
                if res.decision != AccessDecision.ALLOW:
                    reply_text = (
                        "आपके संदेश के लिए धन्यवाद — हमारी टीम जल्द ही आपसे संपर्क करेगी।"
                        if is_hindi_script(msg.text)
                        else "Thanks for your message — our team will get back to you shortly."
                    )
                elif answer.status.value == "answered":  # type: ignore[union-attr]
                    reply_text = _strip_citation_markers(answer.answer_text)  # type: ignore[union-attr]
                else:
                    reply_text = answer.abstention_reason or default_abstention_message(msg.text)  # type: ignore[union-attr]

                if not (tenant.whatsapp_phone_number_id and tenant.whatsapp_access_token):
                    logger.warning("Tenant %s has no WhatsApp credentials configured to reply with.", tenant.tenant_id)
                    continue
                try:
                    svc.whatsapp_client().send_text(
                        phone_number_id=tenant.whatsapp_phone_number_id, access_token=tenant.whatsapp_access_token,
                        to=msg.wa_id, body=reply_text,
                    )
                except WhatsAppSendError as exc:
                    logger.error("Failed to send WhatsApp reply for tenant %s: %s", tenant.tenant_id, exc)
            except Exception as exc:  # noqa: BLE001 - one bad message must never break the rest of the batch or the webhook's 200
                logger.error("Unhandled error processing a WhatsApp message: %s", exc)

        return {"status": "ok"}

    # -------------------------------------------------------------- leads
    @app.post("/api/leads")
    def create_lead(request: LeadRequest, tenant_id: str) -> dict:
        # Deliberately unauthenticated on the *write* side: a website
        # visitor submitting their own contact info isn't logged in. The
        # tenant must simply exist and be active — this is what lets a
        # real customer leave their number without creating an account.
        try:
            svc.tenant_registry.get_active_tenant(tenant_id)
        except (TenantNotFoundError, TenantNotActiveError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        try:
            lead = svc.lead_store.create(
                tenant_id=tenant_id, session_id=request.session_id, name=request.name,
                phone=request.phone, email=request.email, message=request.message,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"lead_id": lead.lead_id}

    @app.get("/api/leads")
    def list_leads(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_LEADS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"leads": [{**l.model_dump(), "stage": lead_stage(l)} for l in svc.lead_store.list_for_tenant(tenant_id)]}

    @app.post("/api/leads/{lead_id}/request-review")
    def request_review(lead_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            # Reuses VIEW_LEADS: same owner/staff who can see leads are the
            # ones who'd know a service was actually completed and it's
            # appropriate to ask for a review.
            tenant = authorize(principal, TenantAction.VIEW_LEADS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        lead = svc.lead_store.get(tenant_id, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail=f"No lead '{lead_id}' for this business.")
        if not tenant.review_link:
            raise HTTPException(status_code=400, detail="Add a review link in your assistant settings first.")

        # A WhatsApp-sourced lead has a real phone number but no email —
        # follow up on the same channel the customer actually used,
        # rather than requiring an email address that was never
        # collected. This is the same render_review_request copy, just
        # delivered over WhatsApp's free-form text instead of HTML email.
        if lead.source == "whatsapp" and lead.phone:
            if not (tenant.whatsapp_phone_number_id and tenant.whatsapp_access_token):
                raise HTTPException(
                    status_code=400,
                    detail="This lead came from WhatsApp, but you haven't connected a WhatsApp number yet.",
                )
            message = (
                f"Hi! This is {tenant.assistant_name}, {tenant.business_name}'s assistant. "
                f"We'd really appreciate it if you could share a quick review of your experience: {tenant.review_link}"
            )
            try:
                svc.whatsapp_client().send_text(
                    phone_number_id=tenant.whatsapp_phone_number_id, access_token=tenant.whatsapp_access_token,
                    to=lead.phone, body=message,
                )
            except WhatsAppSendError as exc:
                if exc.error_code == REENGAGEMENT_WINDOW_CLOSED_CODE:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "This customer's 24-hour WhatsApp window has closed. Sending a message now requires "
                            "a Meta-approved message template, which isn't set up yet — wait for their next "
                            "message, or follow up another way."
                        ),
                    ) from exc
                raise HTTPException(status_code=502, detail=f"Could not send the WhatsApp review request: {exc}") from exc
            return {"sent_to": lead.phone, "channel": "whatsapp"}

        if not lead.email:
            raise HTTPException(status_code=400, detail="This lead has no email address to send a review request to.")
        if not svc.settings.resend_api_key or not svc.settings.digest_from_email:
            raise HTTPException(status_code=400, detail="Email sending is not configured for this deployment.")

        subject, html = render_review_request(
            business_name=tenant.business_name, assistant_name=tenant.assistant_name, review_link=tenant.review_link,
        )
        try:
            svc.email_sender().send(to=lead.email, subject=subject, html_body=html)
        except EmailSendError as exc:
            raise HTTPException(status_code=502, detail=f"Could not send the review request: {exc}") from exc
        return {"sent_to": lead.email, "channel": "email"}

    @app.put("/api/leads/{lead_id}/appointment")
    def set_lead_appointment(
        lead_id: str, request: AppointmentRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        """Manual for now — there's no live calendar/slot booking yet
        (a deliberately deferred, much larger feature). This is the
        foundation both reminders and win-back read from."""
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_LEADS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        try:
            appointment_utc = _parse_appointment_to_utc(request.appointment_at)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="appointment_at must be a valid date/time.") from exc

        updated = svc.lead_store.set_appointment(tenant_id, lead_id, appointment_utc)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"No lead '{lead_id}' for this business.")
        return updated.model_dump()

    @app.post("/api/leads/{lead_id}/deposit-link")
    def send_deposit_link(lead_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.VIEW_LEADS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        lead = svc.lead_store.get(tenant_id, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail=f"No lead '{lead_id}' for this business.")
        if not tenant.deposit_amount_inr:
            raise HTTPException(status_code=400, detail="Set a deposit amount in your assistant settings first.")

        # Confirm we can actually deliver the link BEFORE creating one at
        # Razorpay — an undeliverable link left dangling in the tenant's
        # own Razorpay dashboard is a small but real papercut to avoid.
        if lead.source == "whatsapp" and lead.phone:
            if not (tenant.whatsapp_phone_number_id and tenant.whatsapp_access_token):
                raise HTTPException(
                    status_code=400,
                    detail="This lead came from WhatsApp, but you haven't connected a WhatsApp number yet.",
                )
            channel = "whatsapp"
        elif lead.email:
            if not svc.settings.resend_api_key or not svc.settings.digest_from_email:
                raise HTTPException(status_code=400, detail="Email sending is not configured for this deployment.")
            channel = "email"
        else:
            raise HTTPException(status_code=400, detail="This lead has no WhatsApp number or email to send a deposit link to.")

        try:
            payment_url = svc.razorpay_client().create_payment_link(
                key_id=tenant.razorpay_key_id or "", key_secret=tenant.razorpay_key_secret or "",
                amount_inr=tenant.deposit_amount_inr, description=f"Booking deposit — {tenant.business_name}",
                customer_name=lead.name, customer_phone=lead.phone if channel == "whatsapp" else None,
            )
        except PaymentLinkError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        message = (
            f"To confirm your booking with {tenant.business_name}, please complete a deposit of "
            f"₹{tenant.deposit_amount_inr} here: {payment_url}"
        )

        if channel == "whatsapp":
            try:
                svc.whatsapp_client().send_text(
                    phone_number_id=tenant.whatsapp_phone_number_id, access_token=tenant.whatsapp_access_token,
                    to=lead.phone, body=message,
                )
            except WhatsAppSendError as exc:
                raise HTTPException(status_code=502, detail=f"Could not send the deposit link: {exc}") from exc
            svc.lead_store.mark_deposit_link_sent(tenant_id, lead_id)
            return {"sent_to": lead.phone, "channel": "whatsapp", "payment_url": payment_url}

        try:
            svc.email_sender().send(
                to=lead.email, subject=f"Confirm your booking with {tenant.business_name}", html_body=f"<p>{message}</p>",
            )
        except EmailSendError as exc:
            raise HTTPException(status_code=502, detail=f"Could not send the deposit link: {exc}") from exc
        svc.lead_store.mark_deposit_link_sent(tenant_id, lead_id)
        return {"sent_to": lead.email, "channel": "email", "payment_url": payment_url}

    @app.post("/api/leads/{lead_id}/deposit-paid")
    def mark_lead_deposit_paid(
        lead_id: str, request: DepositPaidRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        """Owner-confirmed only — the exact same request-and-confirm
        shape as platform billing's mark-paid (app.py's admin_mark_paid),
        just at the tenant-customer level. A deposit LINK being sent
        never implies payment; this is the one action that does."""
        principal = _resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.VIEW_LEADS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        amount = request.amount_inr if request.amount_inr is not None else tenant.deposit_amount_inr
        if not amount:
            raise HTTPException(status_code=400, detail="No amount given and no deposit amount configured for this business.")
        try:
            updated = svc.lead_store.mark_deposit_paid(tenant_id, lead_id, amount)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail=f"No lead '{lead_id}' for this business.")
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="deposit_confirmed_paid",
            target_type="lead", target_id=lead_id, metadata={"amount_inr": amount},
        )
        return updated.model_dump()

    @app.post("/api/leads/{lead_id}/appointment-outcome")
    def record_lead_appointment_outcome(
        lead_id: str, request: AppointmentOutcomeRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_LEADS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        try:
            updated = svc.lead_store.record_appointment_outcome(tenant_id, lead_id, request.outcome)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail=f"No lead '{lead_id}' for this business.")
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="appointment_outcome_recorded",
            target_type="lead", target_id=lead_id, metadata={"outcome": request.outcome},
        )
        return updated.model_dump()

    # -------------------------------------------------------------- analytics
    @app.get("/api/analytics")
    def get_analytics(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_ANALYTICS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return svc.analytics_store.summary_for_tenant(tenant_id).model_dump()

    @app.get("/api/analytics/gaps")
    def list_gaps(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_ANALYTICS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"gaps": [g.model_dump() for g in svc.analytics_store.list_open_gaps(tenant_id)]}

    # -------------------------------------------------------------- knowledge
    @app.post("/api/knowledge/website")
    def ingest_website(request: WebsiteIngestRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.INGEST_KNOWLEDGE, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        try:
            text = fetch_website_text(request.url)
        except UnsafeUrlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        source_id = f"web_{secrets.token_hex(6)}"
        label = request.label or request.url
        try:
            result = ingest_text(
                text=text, tenant_id=tenant_id, source_id=source_id, source_label=label, source_url=request.url,
                embeddings=svc.embeddings(), store=svc.vector_store,
            )
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        svc.source_store.record(tenant_id=tenant_id, source_id=source_id, label=label, source_type="website", chunks_indexed=result.chunks_indexed)
        return {"source_id": source_id, "chunks_indexed": result.chunks_indexed}

    @app.post("/api/knowledge/upload")
    async def upload_knowledge(
        tenant_id: str,
        file: UploadFile = File(...),
        authorization: str | None = Header(default=None),
    ) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.INGEST_KNOWLEDGE, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        content = await file.read()
        from business_ai.security import MAX_UPLOAD_BYTES

        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="File exceeds the maximum allowed upload size.")

        filename = file.filename or "upload"
        try:
            if filename.lower().endswith(".pdf"):
                text = extract_pdf_text(content)
                source_type = "pdf"
            else:
                text = content.decode("utf-8", errors="replace")
                source_type = "text"
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        source_id = f"file_{secrets.token_hex(6)}"
        try:
            result = ingest_text(
                text=text, tenant_id=tenant_id, source_id=source_id, source_label=filename, source_url=None,
                embeddings=svc.embeddings(), store=svc.vector_store,
            )
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        svc.source_store.record(tenant_id=tenant_id, source_id=source_id, label=filename, source_type=source_type, chunks_indexed=result.chunks_indexed)
        return {"source_id": source_id, "chunks_indexed": result.chunks_indexed}

    @app.get("/api/knowledge/sources")
    def list_sources(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_ANALYTICS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"sources": [s.model_dump() for s in svc.source_store.list_for_tenant(tenant_id)]}

    def _load_gap_or_404(tenant_id: str, turn_id: str):
        gap = svc.analytics_store.get_gap(tenant_id, turn_id)
        if gap is None:
            raise HTTPException(status_code=404, detail=f"No open knowledge gap '{turn_id}' for this business.")
        return gap

    @app.post("/api/knowledge/gaps/{turn_id}/draft")
    def draft_gap_answer(turn_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.INGEST_KNOWLEDGE, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        gap = _load_gap_or_404(tenant_id, turn_id)
        try:
            draft = svc.generator().draft_faq_answer(
                business_name=tenant.business_name, assistant_name=tenant.assistant_name, question=gap.query
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not generate a draft: {exc}") from exc
        return {"turn_id": turn_id, "question": gap.query, "draft_answer": draft}

    @app.post("/api/knowledge/gaps/{turn_id}/publish")
    def publish_gap_answer(
        turn_id: str, request: GapPublishRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.INGEST_KNOWLEDGE, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        gap = _load_gap_or_404(tenant_id, turn_id)
        text = f"Q: {gap.query}\nA: {request.answer_text}"
        source_id = f"gap_{turn_id}"
        try:
            result = ingest_text(
                text=text, tenant_id=tenant_id, source_id=source_id, source_label=f"FAQ: {gap.query[:60]}",
                source_url=None, embeddings=svc.embeddings(), store=svc.vector_store,
            )
        except IngestionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        svc.source_store.record(
            tenant_id=tenant_id, source_id=source_id, label=f"FAQ: {gap.query[:60]}",
            source_type="faq", chunks_indexed=result.chunks_indexed,
        )
        svc.analytics_store.mark_gap_resolved(tenant_id, query=gap.query)
        return {"source_id": source_id, "chunks_indexed": result.chunks_indexed}

    # -------------------------------------------------------------- tenant self-config
    @app.get("/api/tenant/public")
    def get_tenant_public(tenant_id: str) -> dict:
        """Unauthenticated, for the embeddable customer chat widget: only
        the fields safe to show an anonymous website visitor, never
        owner_email or question_quota."""
        try:
            tenant = authorize(None, TenantAction.VIEW_PUBLIC_INFO, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "business_name": tenant.business_name,
            "assistant_name": tenant.assistant_name,
            "welcome_message": tenant.welcome_message,
            "whatsapp_number": tenant.whatsapp_number,
        }

    @app.get("/api/tenant")
    def get_tenant(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.VIEW_ANALYTICS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return tenant.model_dump()

    @app.put("/api/tenant")
    def update_tenant(request: TenantConfigUpdate, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_ASSISTANT, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        fields = {k: v for k, v in request.model_dump().items() if v is not None}
        updated = svc.tenant_registry.update_config(tenant_id, **fields)
        return updated.model_dump()

    @app.get("/api/tenant/whatsapp/embedded-signup-status")
    def whatsapp_embedded_signup_status() -> dict:
        """Unauthenticated, read-only: whether the "Connect WhatsApp"
        one-click flow can even be offered on this deployment yet. The
        dashboard uses this to decide whether to show that button at
        all, rather than showing it and failing on click — this is a
        platform-wide capability flag, not tenant data."""
        return {"available": bool(svc.settings.whatsapp_app_id and svc.settings.whatsapp_app_secret)}

    @app.post("/api/tenant/whatsapp/embedded-signup")
    def connect_whatsapp_embedded_signup(
        request: EmbeddedSignupRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        """Completes WhatsApp Embedded Signup for one tenant: exchanges
        the authorization code Meta's popup handed the frontend for a
        long-lived access token, server-side, using Business AI's own
        Meta App credentials — then stores it in the exact same
        TenantConfig fields the manual flow already uses. Same
        authorization as the manual flow (MANAGE_ASSISTANT): whoever can
        change assistant settings can connect WhatsApp either way."""
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_ASSISTANT, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if not (svc.settings.whatsapp_app_id and svc.settings.whatsapp_app_secret):
            raise HTTPException(
                status_code=400,
                detail="One-click WhatsApp connection isn't available on this deployment yet — use the manual connection fields below instead.",
            )

        try:
            access_token = svc.meta_signup_client().exchange_code_for_token(
                app_id=svc.settings.whatsapp_app_id, app_secret=svc.settings.whatsapp_app_secret, code=request.code,
            )
        except MetaEmbeddedSignupError as exc:
            raise HTTPException(status_code=502, detail=f"Could not connect WhatsApp: {exc}") from exc

        updated = svc.tenant_registry.update_config(
            tenant_id, whatsapp_phone_number_id=request.phone_number_id, whatsapp_access_token=access_token,
        )
        return {"whatsapp_phone_number_id": updated.whatsapp_phone_number_id, "connected": True}

    # -------------------------------------------------------------- employees & tasks (admin WhatsApp bot)

    @app.post("/api/employees")
    def create_employee(request: CreateEmployeeRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EMPLOYEES, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        try:
            employee = svc.employee_store.add(
                tenant_id=tenant_id, whatsapp_number=request.whatsapp_number, name=request.name, role=request.role,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="employee_added",
            target_type="employee", target_id=employee.employee_id, metadata={"role": employee.role},
        )
        return employee.model_dump()

    @app.get("/api/employees")
    def list_employees(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EMPLOYEES, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"employees": [e.model_dump() for e in svc.employee_store.list_for_tenant(tenant_id, active_only=False)]}

    @app.put("/api/employees/{employee_id}")
    def update_employee(
        employee_id: str, request: UpdateEmployeeRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EMPLOYEES, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        updated = None
        if request.role is not None:
            try:
                updated = svc.employee_store.set_role(tenant_id, employee_id, request.role)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            svc.audit_log.record(
                tenant_id=tenant_id, actor_employee_id=None, action="role_changed",
                target_type="employee", target_id=employee_id, metadata={"role": request.role},
            )
        if request.active is False:
            updated = svc.employee_store.deactivate(tenant_id, employee_id)
            svc.audit_log.record(
                tenant_id=tenant_id, actor_employee_id=None, action="employee_deactivated",
                target_type="employee", target_id=employee_id,
            )
        if updated is None:
            updated = svc.employee_store.get(tenant_id, employee_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Unknown employee_id for this business.")
        return updated.model_dump()

    @app.post("/api/tasks")
    def create_task(request: CreateTaskRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Dashboard-side task creation — the WhatsApp `assign` command's
        equivalent for a caller that already knows the exact
        assigned_to_employee_id (e.g. from GET /api/employees) rather
        than typing a name to fuzzy-match. The only way today to set
        customer_facing_lead_id (linking a task to the lead/conversation
        it originated from) without going through the store directly,
        which is what enables the verified-outcome ping on completion."""
        principal = _resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.ASSIGN_TASK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        assignee = svc.employee_store.get(tenant_id, request.assigned_to_employee_id)
        if assignee is None:
            raise HTTPException(status_code=404, detail="Unknown assigned_to_employee_id for this business.")
        if request.customer_facing_lead_id and svc.lead_store.get(tenant_id, request.customer_facing_lead_id) is None:
            raise HTTPException(status_code=404, detail="Unknown customer_facing_lead_id for this business.")

        due_at = None
        if request.due_at:
            try:
                due_at = _parse_appointment_to_utc(request.due_at)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid due_at: {exc}") from exc

        task = svc.task_store.create(
            tenant_id=tenant_id, title=request.title, description=request.description,
            assigned_to_employee_id=assignee.employee_id, assigned_by_employee_id=principal.principal_id,
            due_at=due_at, approval_required=request.approval_required,
            customer_facing_lead_id=request.customer_facing_lead_id,
        )
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="task_assigned",
            target_type="task", target_id=task.task_id, metadata={"assigned_to": assignee.employee_id, "via": "api"},
        )
        due_note = f" — due {due_at[:16].replace('T', ' ')} UTC" if due_at else ""
        _send_admin_bot_message(
            tenant, assignee.whatsapp_number,
            f'📋 New task: "{task.title}"{due_note}. Reply "done {_short_task_id(task.task_id)}" when finished.',
        )
        return task.model_dump()

    @app.get("/api/tasks")
    def list_tasks(tenant_id: str, authorization: str | None = Header(default=None), status: str | None = None) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_TASKS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"tasks": [t.model_dump() for t in svc.task_store.list_for_tenant(tenant_id, status=status)]}

    @app.get("/api/feedback")
    def list_feedback(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "items": [f.model_dump() for f in svc.feedback_store.list_for_tenant(tenant_id)],
            "themes": [t.model_dump() for t in svc.feedback_store.summarize_by_theme(tenant_id)],
        }

    @app.post("/api/feedback/{feedback_id}/resolve")
    def resolve_feedback(feedback_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        updated = svc.feedback_store.mark_resolved(tenant_id, feedback_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Unknown feedback_id for this business.")
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="feedback_resolved",
            target_type="feedback", target_id=feedback_id,
        )
        return updated.model_dump()

    @app.get("/api/business-health")
    def get_business_health(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """The dashboard's read of the same transparent, component-based
        snapshot the `scorecard`/`health` WhatsApp command renders as
        text — one computation (_business_health_snapshot), two
        presentations. Gated the same as feedback: aggregated management
        insight, not something staff sees."""
        principal = _resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _business_health_snapshot(tenant, window_hours=svc.settings.digest_window_hours)

    @app.get("/api/timeline")
    def get_timeline(tenant_id: str, authorization: str | None = Header(default=None), limit: int = 50) -> dict:
        """A read over the existing audit log, not a new event-store —
        every entry here is something AuditLogStore already recorded for
        an unrelated reason (permissions, dispute resolution); this
        endpoint just renders it chronologically. Gated the same as
        feedback/business-health: aggregated management insight."""
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"entries": [e.model_dump() for e in svc.audit_log.list_for_tenant(tenant_id, limit=min(limit, 200))]}

    @app.get("/api/sops")
    def list_sops(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"sops": [s.model_dump() for s in svc.sop_store.list_for_tenant(tenant_id)]}

    @app.post("/api/sops")
    def approve_sop(request: ApproveSopRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_SOPS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        theme_key = _resolve_theme_key(request.theme)
        if theme_key is None:
            raise HTTPException(status_code=400, detail=f"Unknown feedback theme '{request.theme}'.")
        try:
            note = svc.sop_store.approve(
                tenant_id=tenant_id, theme=theme_key, text=request.text, approved_by_employee_id=principal.principal_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="sop_approved",
            target_type="sop", target_id=theme_key,
        )
        return note.model_dump()

    # -------------------------------------------------------------- platform admin
    @app.get("/api/v1/admin/tenants")
    def admin_list_tenants(authorization: str | None = Header(default=None)) -> dict:
        principal = _require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")
        return {"tenants": [t.model_dump() for t in svc.tenant_registry.list_all()]}

    @app.post("/api/v1/admin/tenants/{target_tenant_id}/activate")
    def admin_activate(target_tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _require(authorization)
        try:
            authorize(principal, TenantAction.ACTIVATE_TENANT, target_tenant_id=target_tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        blocker = _activation_blocker(target_tenant_id)
        if blocker:
            raise HTTPException(status_code=400, detail=blocker)

        updated = svc.tenant_registry.update_status(target_tenant_id, TenantStatus.ACTIVE)
        _notify_owner_of_activation(updated)
        return updated.model_dump()

    @app.post("/api/tenant/activate")
    def self_activate_tenant(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Lets the OWNER activate their own tenant once they've cleared
        the same bar an admin would check — knowledge ingested, and paid
        if a subscription price was set. Removes the founder from the
        loop as the default path; admin activation (above) still works
        unchanged for anyone who wants to do it by hand, and `suspend`
        remains the escape hatch if a self-activated tenant needs to be
        pulled back."""
        principal = _resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.MANAGE_ASSISTANT, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if tenant.status == TenantStatus.SUSPENDED:
            raise HTTPException(status_code=403, detail="This account has been suspended — contact support.")
        if tenant.status == TenantStatus.ACTIVE:
            raise HTTPException(status_code=400, detail="Already active.")

        blocker = _activation_blocker(tenant_id)
        if blocker:
            raise HTTPException(status_code=400, detail=blocker)

        updated = svc.tenant_registry.update_status(tenant_id, TenantStatus.ACTIVE)
        _notify_admin_of_self_activation(updated)
        return updated.model_dump()

    @app.post("/api/v1/admin/tenants/{target_tenant_id}/billing-link")
    def admin_send_billing_link(
        target_tenant_id: str, request: BillingLinkRequest, authorization: str | None = Header(default=None)
    ) -> dict:
        """Generates a Business AI subscription payment link (platform's
        own Razorpay account, never the tenant's) and emails it to the
        owner. Same "send a link, confirm manually" shape as every other
        payment feature in this app — see payments.py's own scope note
        on why there's no webhook-based auto-confirmation."""
        principal = _require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")
        try:
            tenant = svc.tenant_registry.get_config(target_tenant_id)
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if not (svc.settings.platform_razorpay_key_id and svc.settings.platform_razorpay_key_secret):
            raise HTTPException(status_code=400, detail="Platform Razorpay isn't configured (PLATFORM_RAZORPAY_KEY_ID/SECRET).")
        if not (svc.settings.resend_api_key and svc.settings.digest_from_email):
            raise HTTPException(status_code=400, detail="Email sending is not configured for this deployment.")

        try:
            payment_url = svc.razorpay_client().create_payment_link(
                key_id=svc.settings.platform_razorpay_key_id, key_secret=svc.settings.platform_razorpay_key_secret,
                amount_inr=request.amount_inr, description=f"Business AI subscription — {tenant.business_name}",
                customer_name=tenant.business_name,
            )
        except PaymentLinkError as exc:
            raise HTTPException(status_code=502, detail=f"Could not create payment link: {exc}") from exc

        subject, html = render_billing_link_email(
            business_name=tenant.business_name, assistant_name=tenant.assistant_name,
            amount_inr=request.amount_inr, payment_url=payment_url,
        )
        try:
            svc.email_sender().send(to=tenant.owner_email, subject=subject, html_body=html)
        except EmailSendError as exc:
            raise HTTPException(status_code=502, detail=f"Could not send the billing email: {exc}") from exc

        updated = svc.tenant_registry.update_config(
            target_tenant_id, subscription_price_inr=request.amount_inr, billing_status="invoiced",
            billing_link_sent_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        return {"sent_to": tenant.owner_email, "payment_url": payment_url, "billing_status": updated.billing_status}

    @app.post("/api/v1/admin/tenants/{target_tenant_id}/mark-paid")
    def admin_mark_paid(
        target_tenant_id: str, request: MarkPaidRequest, authorization: str | None = Header(default=None)
    ) -> dict:
        """Manual payment confirmation — the admin checked their own
        Razorpay dashboard and is recording it here. No webhook-based
        auto-confirmation exists (same honest limitation as deposit
        links); see README's known-limitations section."""
        principal = _require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")
        try:
            svc.tenant_registry.get_config(target_tenant_id)
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        fields: dict = {"billing_status": "paid", "billing_paid_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if request.amount_inr is not None:
            fields["subscription_price_inr"] = request.amount_inr
        updated = svc.tenant_registry.update_config(target_tenant_id, **fields)
        return updated.model_dump()

    @app.post("/api/v1/admin/tenants/{target_tenant_id}/suspend")
    def admin_suspend(target_tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = _require(authorization)
        try:
            authorize(principal, TenantAction.SUSPEND_TENANT, target_tenant_id=target_tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        updated = svc.tenant_registry.update_status(target_tenant_id, TenantStatus.SUSPENDED)
        return updated.model_dump()

    @app.post("/api/v1/admin/digest/run")
    def admin_run_digest(authorization: str | None = Header(default=None)) -> dict:
        """Sends the owner digest to every ACTIVE tenant with activity in
        the window. Meant to be triggered by an external scheduler
        (Railway cron / GitHub Actions scheduled workflow hitting this
        endpoint) — no in-process scheduler here; that would be a new
        background-thread lifecycle to manage for something that only
        needs to fire once a day."""
        principal = _require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        if not svc.settings.resend_api_key or not svc.settings.digest_from_email:
            return {
                "sent": [], "skipped": [], "failed": [],
                "note": "Digest email is not configured (RESEND_API_KEY / DIGEST_FROM_EMAIL).",
            }

        sender = svc.email_sender()
        since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - svc.settings.digest_window_hours * 3600))
        dashboard_url = f"{svc.settings.public_base_url}/dashboard" if svc.settings.public_base_url else None

        sent: list[str] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "not active"})
                continue

            new_leads = svc.lead_store.list_for_tenant(tenant.tenant_id, since_iso=since_iso)
            analytics = svc.analytics_store.summary_for_tenant(tenant.tenant_id, since_iso=since_iso)
            employees_by_id = {e.employee_id: e for e in svc.employee_store.list_for_tenant(tenant.tenant_id)}
            overdue_lines = _overdue_lines_by_employee(svc.task_store.list_overdue(tenant.tenant_id), employees_by_id)
            recurring_lines = _recurring_feedback_lines(tenant.tenant_id, since_iso=since_iso)
            if not has_digest_content(
                new_leads, analytics, overdue_summary_lines=overdue_lines, recurring_feedback_lines=recurring_lines
            ):
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "no activity in window"})
                continue

            try:
                action_items = svc.generator().generate_action_brief(
                    business_name=tenant.business_name, total_questions=analytics.total_questions,
                    answered_count=analytics.answered_count, abstention_count=analytics.abstention_count,
                    buying_intent_count=analytics.buying_intent_count, dissatisfaction_count=analytics.dissatisfaction_count,
                    new_leads_count=len(new_leads), recent_knowledge_gaps=analytics.recent_knowledge_gaps,
                    overdue_task_lines=overdue_lines, recurring_feedback_lines=recurring_lines,
                )
            except Exception as exc:  # noqa: BLE001 - a failed advisory brief must not block the digest itself
                logger.warning("Action brief generation failed for tenant %s: %s", tenant.tenant_id, exc)
                action_items = []

            subject, html = render_owner_digest(
                tenant, new_leads=new_leads, analytics=analytics,
                window_hours=svc.settings.digest_window_hours, dashboard_url=dashboard_url,
                action_items=action_items, overdue_summary_lines=overdue_lines, recurring_feedback_lines=recurring_lines,
            )
            try:
                sender.send(to=tenant.owner_email, subject=subject, html_body=html)
                sent.append(tenant.tenant_id)
            except EmailSendError as exc:
                failed.append({"tenant_id": tenant.tenant_id, "error": str(exc)})

            # Bonus fast path alongside the guaranteed email above — never
            # gates it, never blocks it, failure here is invisible to the
            # caller by design (see _notify_management_whatsapp's docstring).
            window_label = "today" if svc.settings.digest_window_hours <= 24 else f"the last {svc.settings.digest_window_hours}h"
            whatsapp_summary = render_owner_whatsapp_summary(
                tenant, new_leads=new_leads, analytics=analytics,
                open_gaps_count=len(svc.analytics_store.list_open_gaps(tenant.tenant_id)),
                window_label=window_label, action_items=action_items,
                overdue_summary_lines=overdue_lines, recurring_feedback_lines=recurring_lines,
            )
            _notify_management_whatsapp(tenant, whatsapp_summary)

        return {"sent": sent, "skipped": skipped, "failed": failed}

    def _send_whatsapp_best_effort(tenant: TenantConfig, lead: Lead, body: str) -> bool:
        """Shared by every automation job below. WhatsApp is the only
        delivery channel wired up for reminders/re-engagement/win-back —
        a tenant without WhatsApp connected, or a lead without a phone
        number, is skipped, not an error: these are background batch
        jobs over many tenants, not a single user-facing request."""
        if not (tenant.whatsapp_phone_number_id and tenant.whatsapp_access_token and lead.phone):
            return False
        try:
            svc.whatsapp_client().send_text(
                phone_number_id=tenant.whatsapp_phone_number_id, access_token=tenant.whatsapp_access_token,
                to=lead.phone, body=body,
            )
            return True
        except WhatsAppSendError as exc:
            logger.warning("Automation WhatsApp send failed for tenant %s lead %s: %s", tenant.tenant_id, lead.lead_id, exc)
            return False

    @app.post("/api/v1/admin/reengagement/run")
    def admin_run_reengagement(authorization: str | None = Header(default=None)) -> dict:
        """Missed-lead re-engagement: leaving contact info at all is
        already the buying-intent signal (see leads.list_for_reengagement)
        — no separate analytics join needed. External-cron-triggered,
        same shape as the digest above."""
        principal = _require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        now = time.time()
        older_than_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - REENGAGEMENT_MIN_AGE_HOURS * 3600))
        newer_than_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - REENGAGEMENT_MAX_AGE_HOURS * 3600))

        sent: list[str] = []
        skipped: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                continue
            for lead in svc.lead_store.list_for_reengagement(
                tenant.tenant_id, older_than_iso=older_than_iso, newer_than_iso=newer_than_iso
            ):
                body = (
                    f"Hi! This is {tenant.assistant_name} from {tenant.business_name}. Just checking in on your "
                    "recent message — happy to help you book, or answer anything else!"
                )
                if _send_whatsapp_best_effort(tenant, lead, body):
                    svc.lead_store.mark_reengaged(tenant.tenant_id, lead.lead_id)
                    sent.append(lead.lead_id)
                else:
                    skipped.append({"lead_id": lead.lead_id, "tenant_id": tenant.tenant_id, "reason": "no WhatsApp channel available"})
        return {"sent": sent, "skipped": skipped}

    @app.post("/api/v1/admin/reminders/run")
    def admin_run_reminders(authorization: str | None = Header(default=None)) -> dict:
        """Appointment reminders (see leads.list_for_reminders). Meant to
        run once daily; the ~24h window (with slack for cron drift) means
        a single daily run catches every upcoming appointment exactly
        once before it happens."""
        principal = _require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        now = time.time()
        window_start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + REMINDER_WINDOW_START_HOURS * 3600))
        window_end_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + REMINDER_WINDOW_END_HOURS * 3600))

        sent: list[str] = []
        skipped: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                continue
            for lead in svc.lead_store.list_for_reminders(
                tenant.tenant_id, window_start_iso=window_start_iso, window_end_iso=window_end_iso
            ):
                when = _format_appointment_ist(lead.appointment_at)
                body = f"Reminder: your appointment with {tenant.business_name} is on {when}. Reply if you need to reschedule!"
                if _send_whatsapp_best_effort(tenant, lead, body):
                    svc.lead_store.mark_reminder_sent(tenant.tenant_id, lead.lead_id)
                    sent.append(lead.lead_id)
                else:
                    skipped.append({"lead_id": lead.lead_id, "tenant_id": tenant.tenant_id, "reason": "no WhatsApp channel available"})
        return {"sent": sent, "skipped": skipped}

    @app.post("/api/v1/admin/winback/run")
    def admin_run_winback(authorization: str | None = Header(default=None)) -> dict:
        """Customer win-back (see leads.list_for_winback). Deliberately
        scoped to WhatsApp-sourced leads: a WhatsApp session_id is stable
        per phone number forever (see whatsapp.py), so that Lead row
        already IS a durable customer record. A web-chat session_id is
        random per page load, so there's no reliable way yet to recognize
        the same person returning to the website across visits."""
        principal = _require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        sent: list[str] = []
        skipped: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                continue
            threshold_days = (
                tenant.winback_after_days if tenant.winback_after_days is not None else svc.settings.winback_default_days
            )
            cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - threshold_days * 86400))
            for lead in svc.lead_store.list_for_winback(tenant.tenant_id, source="whatsapp", cutoff_iso=cutoff_iso):
                body = (
                    f"We miss you at {tenant.business_name}! It's been a while — we'd love to see you again "
                    "whenever you're ready."
                )
                if _send_whatsapp_best_effort(tenant, lead, body):
                    svc.lead_store.mark_winback_sent(tenant.tenant_id, lead.lead_id)
                    sent.append(lead.lead_id)
                else:
                    skipped.append({"lead_id": lead.lead_id, "tenant_id": tenant.tenant_id, "reason": "no WhatsApp channel available"})
        return {"sent": sent, "skipped": skipped}

    @app.post("/api/v1/admin/task-escalation/run")
    def admin_run_task_escalation(authorization: str | None = Header(default=None)) -> dict:
        """Proactive alert for tasks overdue by more than
        TASK_ESCALATION_HOURS — a daily digest mention isn't enough for
        something that's been sitting for two days. Meant to be polled
        more often than the once-a-day digest (e.g. every few hours);
        idempotent either way since escalation is deduped per-task via
        TaskStore.mark_reminder_sent (reused as "already escalated", the
        same field name/shape as the lead-reminder marker). Consolidates
        every escalating task into ONE message per tenant per run, not
        one ping per task."""
        principal = _require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        escalation_cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - TASK_ESCALATION_HOURS * 3600))
        dashboard_url = f"{svc.settings.public_base_url}/dashboard" if svc.settings.public_base_url else None
        escalated: list[str] = []
        skipped: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                continue
            overdue = svc.task_store.list_overdue(tenant.tenant_id)
            to_escalate = [
                t for t in overdue if t.due_at and t.due_at < escalation_cutoff and t.reminder_sent_at is None
            ]
            if not to_escalate:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "nothing newly escalating"})
                continue

            employees_by_id = {e.employee_id: e for e in svc.employee_store.list_for_tenant(tenant.tenant_id)}
            overdue_lines = _overdue_lines_by_employee(to_escalate, employees_by_id)
            sent_count = _notify_management_whatsapp(
                tenant, "🔴 Task escalation — significantly overdue:\n" + "\n".join(overdue_lines)
            )
            if svc.settings.resend_api_key and svc.settings.digest_from_email:
                subject, html = render_task_escalation_alert(
                    business_name=tenant.business_name, overdue_lines=overdue_lines, dashboard_url=dashboard_url,
                )
                try:
                    svc.email_sender().send(to=tenant.owner_email, subject=subject, html_body=html)
                except EmailSendError as exc:
                    logger.warning("Task escalation email failed for tenant %s: %s", tenant.tenant_id, exc)
            for t in to_escalate:
                svc.task_store.mark_reminder_sent(tenant.tenant_id, t.task_id)
            escalated.append(tenant.tenant_id)
            if sent_count == 0:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "no WhatsApp recipient reachable (email attempted)"})
        return {"escalated": escalated, "skipped": skipped}

    # -------------------------------------------------------------- static pages
    for route, filename in (
        ("/", "index.html"), ("/login", "login.html"), ("/login.html", "login.html"),
        ("/chat", "chat.html"), ("/chat.html", "chat.html"),
        ("/dashboard", "dashboard.html"), ("/dashboard.html", "dashboard.html"),
        ("/404", "404.html"), ("/404.html", "404.html"),
    ):
        def _make_handler(fname: str):
            def _handler() -> FileResponse:
                return FileResponse(STATIC_DIR / fname)

            return _handler

        app.get(route)(_make_handler(filename))

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
