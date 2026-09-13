"""Admin WhatsApp bot: employee coordination command grammar, the core
grounded-answer pipeline shared with the customer-facing widget, and the
business-health/automation/timeline rendering engine (Phase 9 extraction
from app.py — this is the single most interconnected block in the
original file, so it stays as one cohesive module rather than being
artificially sliced further).

Exposes a fixed set of cross-cutting helpers on `ctx` (see the bottom of
register_admin_bot) for the other route modules that need them — auth
(signup notification), the customer /api/ask route (_process_question),
insights/automation/feedback routes (business-health snapshot, automation
firing, theme lookup) — everything else in this file is used only
internally and stays a private nested closure exactly as it was in the
original app.py.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Request
from pydantic import BaseModel

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
from business_ai.automation import (
    ActionType,
    AutomationRule,
    AutomationRuleStore,
    AutomationRun,
    AutomationRunStore,
    MAX_ATTEMPTS,
    RunStatus,
    TriggerType,
)
from business_ai.constants import RECURRING_FEEDBACK_THRESHOLD, TASK_ESCALATION_HOURS
from business_ai.digest import has_digest_content, render_owner_digest, render_owner_whatsapp_summary
from business_ai.employees import Employee, EmployeeStore
from business_ai.feedback import FeedbackStore
from business_ai.memory import SopStore
from business_ai.email_sender import EmailSendError, EmailSender
from business_ai.auth import Principal, UserStore, create_access_token, resolve_principal, AuthenticationError
from business_ai.config import Settings, load_settings, validate_environment
from business_ai.formatting import (
    _format_appointment_ist,
    _parse_appointment_to_utc,
    _short_task_id,
    _strip_citation_markers,
    _whatsapp_link,
)
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
from business_ai.inventory import UnitMismatchError
from business_ai.leads import Lead, LeadStore, lead_stage
from business_ai.payments import PaymentLinkError, RazorpayClient, verify_razorpay_webhook_signature
from business_ai.retrieval import OpenAIEmbeddingProvider, RetrievalEngine, VectorStore
from business_ai.revenue_radar import compute_revenue_leakage, render_revenue_radar_whatsapp
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
# Phase 12: "log sale 1500 haircut", "log expense 300", "log collection 2000 deposit"
_ADMIN_LOG_METRIC_RE = re.compile(r"^log\s+(sale|expense|collection)\s+([\d,]+(?:\.\d+)?)(?:\s+(.+))?$", re.IGNORECASE | re.DOTALL)
_OUTCOME_ALIASES = {"no-show": "no_show", "no_show": "no_show", "cancelled": "cancelled", "canceled": "cancelled", "completed": "completed"}
# Phase 17 (Restaurant Foundation): "log sale butter chicken x2". This
# deliberately matches ANY "log sale <text>", including one starting
# with a digit (a real menu item like "7 Up" or "2 Piece Chicken") — the
# caller checks whether the text resolves to a real menu item FIRST and
# only falls back to _ADMIN_LOG_METRIC_RE's numeric-amount grammar when
# it doesn't, rather than trying to exclude digit-leading text by
# pattern (which would silently misread a real numbered dish name).
_ADMIN_LOG_DISH_SALE_RE = re.compile(r"^log\s+sale\s+(.+?)(?:\s+x\s?(\d+))?$", re.IGNORECASE)
# "log purchase 10 kg chicken ₹4200 from Ramesh"
_ADMIN_LOG_PURCHASE_RE = re.compile(
    r"^log\s+purchase\s+([\d.]+)\s+(\S+)\s+(.+?)\s+₹\s?([\d,]+(?:\.\d+)?)(?:\s+from\s+(.+))?$", re.IGNORECASE
)
# "log waste 500 g paneer: spoiled" — colon-separated optional reason,
# the same idiom already established by "approve sop <theme>: <text>".
_ADMIN_LOG_WASTE_RE = re.compile(r"^log\s+waste\s+([\d.]+)\s+(\S+)\s+([^:]+?)(?:\s*:\s*(.+))?$", re.IGNORECASE)

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
        "• log sale/expense/collection <amount> [note] — record a manual entry\n"
        "• financials / sales report — last 30 days manual totals (owner/manager)\n"
        "• revenue radar / leakage — missed bookings, unpaid deposits, no-shows (owner/manager)\n"
        "• log sale <dish> [x<qty>] — log a menu-item sale (auto-priced, depletes stock)\n"
        "• log purchase <qty> <unit> <ingredient> ₹<amount> [from <supplier>]\n"
        "• log waste <qty> <unit> <ingredient> [: <reason>]\n"
        "• inventory / stock — items below par level (owner/manager)\n"
        "\nOr just type naturally — I'll do my best to understand "
        "(except money/outcome confirmations, which always need the exact commands above)."
    )


def register_admin_bot(app: FastAPI, svc, ctx) -> None:
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
                # Phase 11: an active, owner-approved self-evolution tone
                # version (if any) is the ONLY thing this pipeline lets
                # self-evolution influence — see evolution.py's module
                # docstring for the full safety boundary. Absent one
                # (the default for every tenant unless they've opted in
                # and approved a proposal), this is "" and behavior is
                # byte-for-byte identical to before this phase existed.
                active_tone_version = svc.evolution_versions.get_active(tenant_id, "assistant_tone")
                tone_instructions = active_tone_version.payload.get("tone_instructions", "") if active_tone_version else ""
                system_prompt = build_system_prompt(
                    tenant.business_name, tenant.assistant_name, channel=channel, tone_instructions=tone_instructions,
                )
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

    def _recommended_actions(snapshot: dict) -> list[str]:
        """Deterministic, data-grounded recommendations for the Owner
        Command Center — no LLM call on this path. Mirrors the spirit of
        generate_action_brief (only surface something the data actually
        supports, never pad the list) but computed for free from the same
        snapshot _business_health_snapshot already builds, so opening the
        dashboard never waits on, or costs, an OpenAI call. The digest
        email is still the place for the richer LLM-written brief."""
        actions: list[str] = []
        if snapshot["tasks_overdue"] > 0:
            noun = "task is" if snapshot["tasks_overdue"] == 1 else "tasks are"
            actions.append(f"{snapshot['tasks_overdue']} {noun} overdue — review team workload.")
        if snapshot["missed_opportunity_count"] > 0:
            noun = "lead hasn't" if snapshot["missed_opportunity_count"] == 1 else "leads haven't"
            actions.append(f"{snapshot['missed_opportunity_count']} {noun} been followed up — reach out to convert them.")
        themes_without_sop = snapshot["recurring_themes_total"] - snapshot["recurring_themes_with_sop"]
        if themes_without_sop > 0:
            noun = "issue has" if themes_without_sop == 1 else "issues have"
            actions.append(f"{themes_without_sop} recurring {noun} no approved fix yet — approve an SOP for the team.")
        if snapshot["dissatisfaction_count"] > 0:
            noun = "complaint" if snapshot["dissatisfaction_count"] == 1 else "complaints"
            actions.append(f"{snapshot['dissatisfaction_count']} customer {noun} this period — review and resolve.")
        return actions

    def _render_automated_action_line(run: AutomationRun, rule_name: str) -> str:
        ts = run.created_at[:16].replace("T", " ")
        action_desc = {"notify_owner": "notified the owner", "create_task": "created a follow-up task"}.get(
            run.action_type, run.action_type
        )
        return f'{ts} — "{rule_name}" {action_desc} ({run.target_type} {run.target_id})'

    # ---------------------------------------------------------- automation engine
    # Phase 6: trigger -> condition -> action rules. Every trigger below is a
    # pure, deterministic read of data that already exists (tasks/feedback/
    # leads) — no new signal is invented, and no existing store is
    # duplicated. See automation.py's module docstring for the overall
    # dedup/retry design.

    def _automation_candidates(tenant: TenantConfig, rule: AutomationRule) -> list[tuple[str, str, dict]]:
        """Every entity CURRENTLY matching `rule`'s trigger condition, as
        (target_type, target_id, metadata). Dedup/retry/give-up decisions
        happen in `_fire_automation_rule`, using this list plus
        automation_run_store history — this function only answers "is the
        condition true right now," nothing about whether it already fired."""
        now = time.time()
        params = rule.trigger_params

        if rule.trigger_type == TriggerType.TASK_OVERDUE:
            hours = float(params.get("hours", 24))
            cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - hours * 3600))
            overdue = svc.task_store.list_overdue(tenant.tenant_id)
            return [
                ("task", t.task_id, {"title": t.title, "due_at": t.due_at})
                for t in overdue if t.due_at and t.due_at < cutoff
            ]

        if rule.trigger_type == TriggerType.NEGATIVE_FEEDBACK_UNRESOLVED:
            hours = float(params.get("hours", 24))
            cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - hours * 3600))
            unresolved = svc.feedback_store.list_unresolved_negative(tenant.tenant_id)
            return [
                ("feedback", f.feedback_id, {"theme": f.theme, "raw_text": f.raw_text[:200]})
                for f in unresolved if f.created_at < cutoff
            ]

        if rule.trigger_type == TriggerType.RECURRING_FEEDBACK_THEME:
            min_count = int(params.get("min_count", RECURRING_FEEDBACK_THRESHOLD))
            window_hours = float(params.get("window_hours", 24 * 7))
            since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - window_hours * 3600))
            summaries = svc.feedback_store.summarize_by_theme(tenant.tenant_id, since_iso=since_iso)
            return [
                ("feedback_theme", f"theme:{s.theme}", {"count": s.count, "theme": s.theme})
                for s in summaries if s.count >= min_count
            ]

        if rule.trigger_type == TriggerType.DEPOSIT_UNPAID_AFTER_APPOINTMENT:
            hours = float(params.get("hours", 24))
            cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - hours * 3600))
            leads = svc.lead_store.list_for_tenant(tenant.tenant_id, limit=2000)
            return [
                ("lead", l.lead_id, {"name": l.name, "appointment_at": l.appointment_at})
                for l in leads
                if l.appointment_at and l.appointment_at < cutoff
                and l.deposit_link_sent_at and not l.deposit_paid_at and not l.appointment_outcome
            ]

        return []

    def _render_automation_notification(rule: AutomationRule, target_type: str, target_id: str, metadata: dict) -> str:
        custom = rule.action_params.get("message")
        if custom:
            try:
                return custom.format(**metadata)
            except (KeyError, IndexError):
                return custom
        if rule.trigger_type == TriggerType.TASK_OVERDUE:
            return f'🤖 Automation "{rule.name}": task "{metadata.get("title", target_id)}" is overdue.'
        if rule.trigger_type == TriggerType.NEGATIVE_FEEDBACK_UNRESOLVED:
            return f'🤖 Automation "{rule.name}": unresolved negative feedback ({metadata.get("theme", "unknown theme")}) needs attention.'
        if rule.trigger_type == TriggerType.RECURRING_FEEDBACK_THEME:
            return f'🤖 Automation "{rule.name}": recurring feedback theme "{metadata.get("theme", "?")}" reported {metadata.get("count", "?")} times.'
        if rule.trigger_type == TriggerType.DEPOSIT_UNPAID_AFTER_APPOINTMENT:
            who = metadata.get("name") or "A customer"
            return f'🤖 Automation "{rule.name}": {who}\'s deposit is still unpaid after their appointment.'
        return f'🤖 Automation "{rule.name}" triggered.'

    def _execute_automation_action(tenant: TenantConfig, rule: AutomationRule, target_type: str, target_id: str, metadata: dict) -> None:
        """Raises on failure — the caller records the run row either way.
        Both action types reuse existing, already-tested capabilities
        (management WhatsApp push, task creation); this function adds no
        new side-effect mechanism of its own."""
        if rule.action_type == ActionType.NOTIFY_OWNER:
            message = _render_automation_notification(rule, target_type, target_id, metadata)
            sent = _notify_management_whatsapp(tenant, message)
            if sent == 0:
                raise RuntimeError("No reachable WhatsApp recipient for owner notification.")
            return

        if rule.action_type == ActionType.CREATE_TASK:
            assignee_id = rule.action_params.get("assigned_to_employee_id")
            if not assignee_id:
                owners = [e for e in svc.employee_store.list_for_tenant(tenant.tenant_id) if e.role == "owner"]
                if not owners:
                    raise RuntimeError("No owner employee to assign the automated task to.")
                assignee_id = owners[0].employee_id
            title_template = rule.action_params.get("task_title") or f"[Automated] {rule.name}"
            try:
                title = title_template.format(**metadata)
            except (KeyError, IndexError):
                title = title_template
            lead_id = target_id if target_type == "lead" else None
            svc.task_store.create(
                tenant_id=tenant.tenant_id, title=title[:200],
                assigned_to_employee_id=assignee_id, assigned_by_employee_id=assignee_id,
                description=f"Auto-created by automation rule \"{rule.name}\" ({rule.trigger_type.value}).",
                customer_facing_lead_id=lead_id,
            )
            return

        raise RuntimeError(f"Unknown action type: {rule.action_type}")

    def _fire_automation_rule(tenant: TenantConfig, rule: AutomationRule) -> dict:
        """Evaluate one rule against current state and, for every matching
        target not already successfully handled (or given up on), execute
        the action exactly once — recording a run row whether it succeeds
        or fails. A recurring-feedback-theme target is the one exception
        allowed to re-fire after a prior success: only when its count has
        grown since the last successful alert, so a still-recurring issue
        can escalate again without ever spamming on an unchanged count."""
        fired: list[str] = []
        given_up: list[str] = []
        failed: list[str] = []
        try:
            candidates = _automation_candidates(tenant, rule)
        except Exception as exc:
            logger.warning("Automation rule %s condition check failed for tenant %s: %s", rule.rule_id, tenant.tenant_id, exc)
            return {"fired": fired, "given_up": given_up, "failed": failed}

        for target_type, target_id, metadata in candidates:
            history = svc.automation_run_store.history_for_target(tenant.tenant_id, rule.rule_id, target_id)
            successes = [r for r in history if r.status == RunStatus.SUCCESS.value]

            if successes:
                if rule.trigger_type != TriggerType.RECURRING_FEEDBACK_THEME:
                    continue  # one-shot trigger: never refire once handled
                last_success = max(successes, key=lambda r: r.created_at)
                if metadata.get("count", 0) <= last_success.metadata.get("count", 0):
                    continue  # no growth since the last time this fired
                # else: the theme kept recurring since the last alert — refire
            else:
                if any(r.status == RunStatus.GIVEN_UP.value for r in history):
                    given_up.append(target_id)
                    continue
                failed_attempts = sum(1 for r in history if r.status == RunStatus.FAILED.value)
                if failed_attempts >= MAX_ATTEMPTS:
                    svc.automation_run_store.record(
                        tenant_id=tenant.tenant_id, rule_id=rule.rule_id, trigger_type=rule.trigger_type.value,
                        target_type=target_type, target_id=target_id, action_type=rule.action_type.value,
                        status=RunStatus.GIVEN_UP, error=f"Gave up after {failed_attempts} failed attempts.",
                        metadata=metadata,
                    )
                    given_up.append(target_id)
                    continue

            try:
                _execute_automation_action(tenant, rule, target_type, target_id, metadata)
            except Exception as exc:
                svc.automation_run_store.record(
                    tenant_id=tenant.tenant_id, rule_id=rule.rule_id, trigger_type=rule.trigger_type.value,
                    target_type=target_type, target_id=target_id, action_type=rule.action_type.value,
                    status=RunStatus.FAILED, error=str(exc), metadata=metadata,
                )
                failed.append(target_id)
                continue

            svc.automation_run_store.record(
                tenant_id=tenant.tenant_id, rule_id=rule.rule_id, trigger_type=rule.trigger_type.value,
                target_type=target_type, target_id=target_id, action_type=rule.action_type.value,
                status=RunStatus.SUCCESS, metadata=metadata,
            )
            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=None, action="automation_action_executed",
                target_type=target_type, target_id=target_id,
                metadata={"rule_id": rule.rule_id, "rule_name": rule.name, "action_type": rule.action_type.value},
            )
            fired.append(target_id)

        return {"fired": fired, "given_up": given_up, "failed": failed}

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
        "automation_rule_created": "{actor} created automation rule \"{rule_name}\"",
        "automation_rule_updated": "{actor} updated automation rule \"{rule_name}\"",
        "automation_rule_deleted": "{actor} deleted automation rule \"{rule_name}\"",
        "automation_kill_switch": "{actor} turned automation {state} for this business",
        "automation_action_executed": "🤖 Automation \"{rule_name}\" took action automatically",
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
            description = template.format(actor=actor, role="?", amount_inr="?", outcome="?", rule_name="?", state="?")
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

        # Dish-name lookup takes precedence over the generic numeric
        # grammar for "log sale ..." — checked FIRST, matching ANY text
        # (including one that starts with a digit, e.g. a real menu item
        # like "7 Up" or "2 Piece Chicken"). Only when this text does
        # NOT match a real menu item do we fall through to the generic
        # "log sale <amount> [note]" parsing below — a real bug caught
        # in this phase's own live verification: without this ordering,
        # "log sale 7 up x2" would have been silently misread as a ₹7
        # sale with note "up x2" instead of 2x a real "7 Up" menu item.
        dish_sale_match = _ADMIN_LOG_DISH_SALE_RE.match(raw)
        dish_menu_item = None
        if dish_sale_match:
            dish_menu_item = svc.menu_store.find_by_name(tenant.tenant_id, dish_sale_match.group(1).strip())

        log_metric_match = _ADMIN_LOG_METRIC_RE.match(raw) if dish_menu_item is None else None
        if log_metric_match:
            metric_type, amount_text, note = log_metric_match.group(1).lower(), log_metric_match.group(2), log_metric_match.group(3)
            try:
                amount = int(round(float(amount_text.replace(",", ""))))
            except ValueError:
                reply(f'Couldn\'t read "{amount_text}" as an amount. Try "log {metric_type} 1500 optional note".')
                return True
            if amount <= 0:
                reply("Amount must be a positive number.")
                return True
            metric = svc.metric_store.record(
                tenant_id=tenant.tenant_id, metric_type=metric_type, amount_inr=amount,
                note=(note or "").strip(), reported_by_employee_id=employee.employee_id,
            )
            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="metric_logged",
                target_type="business_metric", target_id=metric.metric_id,
                metadata={"metric_type": metric_type, "amount_inr": amount},
            )
            reply(f"✅ Logged {metric_type}: ₹{amount}" + (f" — {note.strip()}" if note else "") + ".")
            return True

        if dish_sale_match:
            dish_name, qty_text = dish_sale_match.group(1).strip(), dish_sale_match.group(2)
            quantity = int(qty_text) if qty_text else 1
            menu_item = dish_menu_item
            if menu_item is None:
                reply(
                    f'Couldn\'t find "{dish_name}" on the menu, and it doesn\'t look like "log sale <amount> [note]" either. '
                    f'Add "{dish_name}" to the menu from the dashboard first, or use "log sale <amount> [note]" for a non-menu sale.'
                )
                return True
            amount = menu_item.price_inr * quantity
            metric = svc.metric_store.record(
                tenant_id=tenant.tenant_id, metric_type="sale", amount_inr=amount, note=f"{quantity}x {menu_item.name}",
                reported_by_employee_id=employee.employee_id, menu_item_id=menu_item.menu_item_id, quantity=quantity,
            )
            # Recipe-based stock depletion is a best-effort side effect —
            # a failure here must never undo or block the sale record
            # itself, same "the primary action always wins" discipline
            # as every other best-effort push in this codebase.
            depleted_any = False
            for line in svc.menu_store.get_recipe(tenant.tenant_id, menu_item.menu_item_id):
                try:
                    svc.inventory_store.adjust_quantity(tenant.tenant_id, line.ingredient_name, delta=-(line.quantity * quantity), unit=line.unit)
                    depleted_any = True
                except Exception as exc:  # noqa: BLE001 - depletion must never block the sale itself
                    logger.warning("Stock depletion failed for tenant %s ingredient %s: %s", tenant.tenant_id, line.ingredient_name, exc)
            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="dish_sale_logged",
                target_type="business_metric", target_id=metric.metric_id,
                metadata={"menu_item_id": menu_item.menu_item_id, "quantity": quantity, "amount_inr": amount},
            )
            reply(f"✅ Logged sale: {quantity}x {menu_item.name} — ₹{amount}." + (" Stock updated." if depleted_any else ""))
            return True

        purchase_match = _ADMIN_LOG_PURCHASE_RE.match(raw)
        if purchase_match:
            qty_text, unit, ingredient, amount_text, supplier_name = purchase_match.groups()
            ingredient = ingredient.strip()
            try:
                quantity = float(qty_text)
                amount = int(round(float(amount_text.replace(",", ""))))
            except ValueError:
                reply('Couldn\'t read that purchase. Try "log purchase 10 kg chicken ₹4200 from Ramesh".')
                return True
            if quantity <= 0:
                reply("Quantity must be a positive number.")
                return True
            # Adjust inventory FIRST: if the unit conflicts with how this
            # ingredient is already tracked, reject the whole command
            # before recording anything — a purchase logged but not
            # reflected in stock (because the unit check failed after
            # the fact) would be worse than not logging it at all.
            try:
                svc.inventory_store.adjust_quantity(tenant.tenant_id, ingredient, delta=quantity, unit=unit)
            except UnitMismatchError as exc:
                reply(f"⚠️ {exc}")
                return True
            supplier = svc.supplier_store.find_by_name(tenant.tenant_id, supplier_name) if supplier_name else None
            purchase = svc.purchase_store.record(
                tenant_id=tenant.tenant_id, ingredient_name=ingredient, quantity=quantity, unit=unit, amount_inr=amount,
                supplier_id=supplier.supplier_id if supplier else None, reported_by_employee_id=employee.employee_id,
            )
            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="purchase_logged",
                target_type="purchase", target_id=purchase.purchase_id,
                metadata={"ingredient_name": ingredient, "quantity": quantity, "amount_inr": amount},
            )
            reply(
                f"✅ Logged purchase: {quantity:g}{unit} {ingredient} — ₹{amount}"
                + (f" from {supplier_name.strip()}" if supplier_name else "") + "."
            )
            return True

        waste_match = _ADMIN_LOG_WASTE_RE.match(raw)
        if waste_match:
            qty_text, unit, ingredient, reason = waste_match.groups()
            ingredient = ingredient.strip()
            try:
                quantity = float(qty_text)
            except ValueError:
                reply('Couldn\'t read that quantity. Try "log waste 500 g paneer: spoiled".')
                return True
            if quantity <= 0:
                reply("Quantity must be a positive number.")
                return True
            # Best-effort cost estimate from this ingredient's own average
            # purchase price — honestly ₹0 when there's no purchase
            # history yet, never a guessed number (same discipline as
            # Revenue Radar's "estimated" figure).
            purchase_summary = svc.purchase_store.sum_for_ingredient(tenant.tenant_id, ingredient)
            estimated_cost = 0
            if purchase_summary["total_quantity"] > 0:
                estimated_cost = round(purchase_summary["total_amount_inr"] / purchase_summary["total_quantity"] * quantity)
            # Adjust inventory FIRST — same reasoning as the purchase
            # handler above: never record a wastage entry that doesn't
            # actually reflect in stock because of a unit conflict.
            try:
                svc.inventory_store.adjust_quantity(tenant.tenant_id, ingredient, delta=-quantity, unit=unit)
            except UnitMismatchError as exc:
                reply(f"⚠️ {exc}")
                return True
            entry = svc.wastage_store.record(
                tenant_id=tenant.tenant_id, ingredient_name=ingredient, quantity=quantity, unit=unit,
                reason=(reason or "other").strip(), estimated_cost_inr=estimated_cost,
                reported_by_employee_id=employee.employee_id,
            )
            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=employee.employee_id, action="wastage_logged",
                target_type="wastage_entry", target_id=entry.wastage_id,
                metadata={"ingredient_name": ingredient, "quantity": quantity, "reason": entry.reason},
            )
            reply(
                f"✅ Logged waste: {quantity:g}{unit} {ingredient} ({entry.reason})"
                + (f" — est. ₹{estimated_cost}" if estimated_cost else "") + "."
            )
            return True

        if clean in ("inventory", "stock"):
            if not can_manage:
                reply("Only an owner or manager can view inventory.")
                return True
            low_stock = svc.inventory_store.list_low_stock(tenant.tenant_id)
            if not low_stock:
                reply("✅ Nothing below par level right now. Try \"log purchase\" to receive stock, or set par levels from the dashboard.")
                return True
            lines = ["⚠️ Low stock:"]
            for item in low_stock:
                lines.append(f"• {item.ingredient_name}: {item.quantity_on_hand:g}{item.unit} (par {item.par_level:g}{item.unit})")
            reply("\n".join(lines))
            return True

        if clean in ("financials", "sales report"):
            if not can_manage:
                reply("Only an owner or manager can view financials.")
                return True
            since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30 * 86400))
            summary = svc.metric_store.summary_for_tenant(tenant.tenant_id, since_iso=since_iso)
            if not summary:
                reply("No manual sales/expense/collection entries logged in the last 30 days. Try \"log sale 1500 haircut\".")
                return True
            lines = [f"💰 Last 30 days (manual entries):"]
            for s in sorted(summary, key=lambda x: x.metric_type):
                lines.append(f"• {s.metric_type.capitalize()}: ₹{s.total_inr} ({s.entry_count} entr{'y' if s.entry_count == 1 else 'ies'})")
            reply("\n".join(lines))
            return True

        if clean in ("revenue radar", "leakage"):
            if not can_manage:
                reply("Only an owner or manager can view the revenue radar.")
                return True
            report = compute_revenue_leakage(
                tenant.tenant_id, lead_store=svc.lead_store, analytics_store=svc.analytics_store,
                deposit_amount_inr=tenant.deposit_amount_inr,
            )
            reply(render_revenue_radar_whatsapp(report))
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

    ctx._resolve = _resolve
    ctx._require = _require
    ctx._notify_admin_of_signup = _notify_admin_of_signup
    ctx._notify_owner_of_activation = _notify_owner_of_activation
    ctx._notify_admin_of_self_activation = _notify_admin_of_self_activation
    ctx._activation_blocker = _activation_blocker
    ctx._notify_management_whatsapp = _notify_management_whatsapp
    ctx._process_question = _process_question
    ctx._business_health_snapshot = _business_health_snapshot
    ctx._recommended_actions = _recommended_actions
    ctx._render_automated_action_line = _render_automated_action_line
    ctx._fire_automation_rule = _fire_automation_rule
    ctx._recurring_feedback_lines = _recurring_feedback_lines
    ctx._overdue_lines_by_employee = _overdue_lines_by_employee
    ctx._resolve_theme_key = _resolve_theme_key
    ctx._send_admin_bot_message = _send_admin_bot_message
