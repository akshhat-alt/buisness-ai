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

Phase 9 restructuring: every route used to live directly in this file's
`create_app()` (it grew to ~3,400 lines across 8 phases). The route
HANDLERS now live in `business_ai.routers.*`, one module per domain, each
exposing a `register_X(app, svc, ctx)` function. `create_app()` below is
now purely an orchestrator: build `Services`, build the FastAPI app and
its middleware, build one shared `RouteContext`, then call every
`register_X`. Nothing about request handling changed — this is a pure
structural move, verified by the full existing test suite plus a live
smoke pass (see PHASE9.md / the Phase 9 commit message for the
verification record). See `routing_context.py` for why a shared `ctx`
object (not FastAPI's Depends machinery) was the lowest-risk way to let
routes in different files keep calling the same cross-cutting helpers
(auth resolution, the grounded-answer pipeline, business-health
snapshotting, automation firing) they always did.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Explicit path, not the default upward-search: this app can be launched
# with a cwd outside the project (e.g. a dev-server runner invoked from a
# sibling directory), so relying on load_dotenv()'s implicit cwd-walk
# would silently no-op in that case.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from business_ai.analytics import AnalyticsStore
from business_ai.audit import AuditLogStore
from business_ai.automation import AutomationRuleStore, AutomationRunStore
from business_ai.constants import DATA_ROOT, STATIC_DIR
from business_ai.employees import EmployeeStore
from business_ai.feedback import FeedbackStore
from business_ai.memory import SopStore
from business_ai.email_sender import EmailSender
from business_ai.config import Settings, load_settings, validate_environment
from business_ai.generation import OpenAIGenerationProvider
from business_ai.ingestion import SourceStore
from business_ai.leads import LeadStore
from business_ai.observability import RequestContextMiddleware, configure_logging
from business_ai.payments import RazorpayClient
from business_ai.rate_limiting import FixedWindowRateLimiter, RateLimitMiddleware
from business_ai.retrieval import OpenAIEmbeddingProvider, VectorStore
from business_ai.routing_context import RouteContext
from business_ai.tasks import TaskStore
from business_ai.tenant import TenantRegistry
from business_ai.usage_limiter import UsageLimiter
from business_ai.whatsapp import MetaEmbeddedSignupClient, WhatsAppClient, WhatsAppInboxStore
from business_ai.auth import UserStore

from business_ai.routers.admin_bot import register_admin_bot
from business_ai.routers.admin_routes import register_admin
from business_ai.routers.automation_routes import register_automation
from business_ai.routers.auth_routes import register_auth
from business_ai.routers.customer_routes import register_customer
from business_ai.routers.dependency_routes import register_dependency
from business_ai.routers.feedback_routes import register_feedback
from business_ai.routers.insights_routes import register_insights
from business_ai.routers.knowledge_routes import register_knowledge
from business_ai.routers.leads_routes import register_leads
from business_ai.routers.static_pages import register_static_pages
from business_ai.routers.team_routes import register_team
from business_ai.routers.tenant_settings_routes import register_tenant_settings
from business_ai.routers.webhook_routes import register_webhooks

logger = logging.getLogger(__name__)


class Services:
    """Everything a request handler needs, built once at startup."""

    def __init__(self, settings: Settings, data_root: Path = DATA_ROOT) -> None:
        self.settings = settings
        self.data_root = data_root
        self.user_store = UserStore(data_root / "users.db")
        self.tenant_registry = TenantRegistry(data_root / "tenants.db", secret_encryption_key=settings.secret_encryption_key)
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
        self.automation_rule_store = AutomationRuleStore(data_root / "automation_rules.db")
        self.automation_run_store = AutomationRunStore(data_root / "automation_runs.db")

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


def create_app(services: Services | None = None) -> FastAPI:
    configure_logging()
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
    # A fresh pair of in-memory limiters per app instance (see
    # rate_limiting.py) — every test/tenant gets isolated state, and a
    # process restart in production simply resets the window, which is
    # correct for an abuse guard (nothing durable to lose).
    app.add_middleware(
        RateLimitMiddleware,
        ip_limiter=FixedWindowRateLimiter(limit_per_minute=svc.settings.ip_requests_per_minute),
        tenant_limiter=FixedWindowRateLimiter(limit_per_minute=svc.settings.tenant_requests_per_minute),
    )
    # Outermost middleware (added last = wraps everything else) so its
    # duration measurement covers the full request, including CORS/
    # security-header/rate-limit handling, and its request_id is
    # available to every log line any inner layer or route handler
    # produces — including a 429 rate-limit rejection.
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        is_api_path = request.url.path.startswith(("/api/", "/healthz"))
        if exc.status_code == 404 and not is_api_path and STATIC_DIR.is_dir():
            page = STATIC_DIR / "404.html"
            if page.is_file():
                return FileResponse(page, status_code=404)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)

    ctx = RouteContext()

    # admin_bot is registered first: it's the one module that PUBLISHES
    # cross-cutting helpers onto ctx (auth resolution, the grounded-answer
    # pipeline, business-health snapshotting, automation firing) that
    # every other module below reads back via ctx. Registration order
    # otherwise doesn't matter — ctx attribute lookups happen at request
    # time, long after every register_* call below has returned — but
    # this order keeps the wiring easy to read top-to-bottom.
    register_admin_bot(app, svc, ctx)
    register_auth(app, svc, ctx)
    register_customer(app, svc, ctx)
    register_leads(app, svc, ctx)
    register_insights(app, svc, ctx)
    register_knowledge(app, svc, ctx)
    register_tenant_settings(app, svc, ctx)
    register_team(app, svc, ctx)
    register_feedback(app, svc, ctx)
    register_dependency(app, svc, ctx)
    register_automation(app, svc, ctx)
    register_webhooks(app, svc, ctx)
    register_admin(app, svc, ctx)
    register_static_pages(app, svc, ctx)

    return app
