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

from business_ai.alerts import render_dissatisfaction_alert, render_review_request
from business_ai.analytics import AnalyticsStore
from business_ai.digest import has_digest_content, render_owner_digest
from business_ai.email_sender import EmailSendError, EmailSender
from business_ai.auth import Principal, UserStore, create_access_token, resolve_principal, AuthenticationError
from business_ai.config import Settings, load_settings, validate_environment
from business_ai.generation import (
    GroundedAnswer,
    OpenAIGenerationProvider,
    build_abstention_answer,
    build_system_prompt,
    build_user_prompt,
    evaluate_evidence_gate,
    validate_llm_draft,
)
from business_ai.ingestion import IngestionError, SourceStore, extract_pdf_text, fetch_website_text, ingest_text
from business_ai.leads import Lead, LeadStore
from business_ai.payments import PaymentLinkError, RazorpayClient
from business_ai.retrieval import OpenAIEmbeddingProvider, RetrievalEngine, VectorStore
from business_ai.security import InvalidTenantIdError, UnsafeUrlError, validate_tenant_id
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


class TenantConfigUpdate(BaseModel):
    assistant_name: str | None = None
    welcome_message: str | None = None
    whatsapp_number: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_access_token: str | None = None
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

    def _send_dissatisfaction_alert(*, tenant: TenantConfig, query: str, answer_text: str, session_id: str) -> None:
        """Best-effort: a slow/failed alert email must never break the
        customer's actual chat response, so failures are swallowed here,
        not raised — this mirrors the digest-run route's per-tenant
        failure collection, just for a single synchronous event instead
        of a batch job."""
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
            pack = engine.retrieve(query, tenant_id=tenant_id, top_k=5)

            gate = evaluate_evidence_gate(pack)
            if gate.should_abstain:
                is_dissatisfied = svc.generator().classify_dissatisfaction(query=query)
                answer = build_abstention_answer(pack, gate, shows_dissatisfaction=is_dissatisfied)
            else:
                system_prompt = build_system_prompt(tenant.business_name, tenant.assistant_name)
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

        svc.tenant_registry.register(
            TenantConfig(
                tenant_id=tenant_id, business_name=request.business_name, owner_email=user.email,
                status=TenantStatus.PROVISIONING,
            )
        )

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
                    reply_text = "Thanks for your message — our team will get back to you shortly."
                elif answer.status.value == "answered":  # type: ignore[union-attr]
                    reply_text = _strip_citation_markers(answer.answer_text)  # type: ignore[union-attr]
                else:
                    reply_text = answer.abstention_reason or "Let me connect you with someone from the team who can help."  # type: ignore[union-attr]

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
        return {"leads": [l.model_dump() for l in svc.lead_store.list_for_tenant(tenant_id)]}

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

        if svc.vector_store.count_for_tenant(target_tenant_id) == 0:
            raise HTTPException(status_code=400, detail="Cannot activate: no knowledge sources ingested yet.")

        updated = svc.tenant_registry.update_status(target_tenant_id, TenantStatus.ACTIVE)
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
            if not has_digest_content(new_leads, analytics):
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "no activity in window"})
                continue

            try:
                action_items = svc.generator().generate_action_brief(
                    business_name=tenant.business_name, total_questions=analytics.total_questions,
                    answered_count=analytics.answered_count, abstention_count=analytics.abstention_count,
                    buying_intent_count=analytics.buying_intent_count, dissatisfaction_count=analytics.dissatisfaction_count,
                    new_leads_count=len(new_leads), recent_knowledge_gaps=analytics.recent_knowledge_gaps,
                )
            except Exception as exc:  # noqa: BLE001 - a failed advisory brief must not block the digest itself
                logger.warning("Action brief generation failed for tenant %s: %s", tenant.tenant_id, exc)
                action_items = []

            subject, html = render_owner_digest(
                tenant, new_leads=new_leads, analytics=analytics,
                window_hours=svc.settings.digest_window_hours, dashboard_url=dashboard_url,
                action_items=action_items,
            )
            try:
                sender.send(to=tenant.owner_email, subject=subject, html_body=html)
                sent.append(tenant.tenant_id)
            except EmailSendError as exc:
                failed.append({"tenant_id": tenant.tenant_id, "error": str(exc)})

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
