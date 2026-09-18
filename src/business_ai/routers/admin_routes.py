"""Platform-admin surface: tenant lifecycle (list/activate/suspend),
platform billing (send-link/mark-paid), and every cron-triggered
`/run` endpoint (automation, digest, reengagement, reminders, winback,
task-escalation) — Phase 9 extraction from app.py. Every route here is
platform_admin-only; there's no in-process scheduler anywhere in this
app, by deliberate design (see ARCHITECTURE.md) — each `/run` endpoint
is meant to be hit by an external cron.
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Header, HTTPException

from business_ai.alerts import (
    render_billing_link_email,
    render_rating_drop_alert,
    render_task_escalation_alert,
    render_tenant_activated_email,
)
from business_ai.digest import has_digest_content, render_owner_digest, render_owner_whatsapp_summary
from business_ai.email_sender import EmailSendError
from business_ai.formatting import _format_appointment_ist, _whatsapp_link
from business_ai.constants import (
    DEPENDENCY_RISK_RENOTIFY_HOURS,
    INVENTORY_ALERT_RENOTIFY_HOURS,
    RATING_DROP_ALERT_THRESHOLD,
    REENGAGEMENT_MAX_AGE_HOURS,
    REENGAGEMENT_MIN_AGE_HOURS,
    REMINDER_WINDOW_END_HOURS,
    REMINDER_WINDOW_START_HOURS,
    RECURRING_FEEDBACK_THRESHOLD,
    TASK_ESCALATION_HOURS,
)
from business_ai.dependency_graph import compute_dependency_snapshot
from business_ai.leads import Lead
from business_ai.payments import PaymentLinkError
from business_ai.reviews import GooglePlacesError, GooglePlacesReviewClient
from business_ai.scorecard import build_weekly_scorecard_data, has_scorecard_content, render_weekly_scorecard_email, render_weekly_scorecard_whatsapp
from business_ai.schemas import BillingLinkRequest, MarkPaidRequest, SetPlanRequest
from business_ai.tenant import TenantAction, TenantConfig, TenantNotFoundError, TenantStatus, UnauthorizedError, authorize
from business_ai.usage_meter import current_period
from business_ai.whatsapp import REENGAGEMENT_WINDOW_CLOSED_CODE, WhatsAppSendError

logger = logging.getLogger(__name__)


def register_admin(app: FastAPI, svc, ctx) -> None:
    def _send_rating_drop_alert(
        *, tenant: TenantConfig, platform: str, previous_rating: float, new_rating: float, review_count: int | None,
    ) -> None:
        """Best-effort on both channels, mirroring
        admin_bot.py's _send_dissatisfaction_alert exactly — a failed
        alert must never fail the sync run itself."""
        ctx._notify_management_whatsapp(
            tenant,
            f"⚠️ Your {platform.capitalize()} rating dropped from {previous_rating:.1f} to {new_rating:.1f} stars.",
        )
        if not svc.settings.resend_api_key or not svc.settings.digest_from_email:
            return
        dashboard_url = f"{svc.settings.public_base_url}/dashboard" if svc.settings.public_base_url else None
        subject, html = render_rating_drop_alert(
            business_name=tenant.business_name, platform=platform, previous_rating=previous_rating,
            new_rating=new_rating, review_count=review_count, dashboard_url=dashboard_url,
        )
        try:
            svc.email_sender().send(to=tenant.owner_email, subject=subject, html_body=html)
        except EmailSendError as exc:
            logger.warning("Failed to send rating-drop alert for tenant %s: %s", tenant.tenant_id, exc)

    @app.post("/api/v1/admin/automation/run")
    def admin_run_automation(authorization: str | None = Header(default=None)) -> dict:
        """The one cron entrypoint for the entire automation engine — meant
        to be invoked periodically by an external cron, same convention as
        every other admin/*/run endpoint. Fail-closed order: tenant must
        be ACTIVE, then automation_enabled must be true, then each of the
        tenant's own enabled rules is evaluated via _fire_automation_rule.
        Idempotent: re-running this immediately after a successful run
        does nothing new, since dedup lives in automation_run_store."""
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        processed: dict[str, dict] = {}
        skipped_tenants: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                continue
            if not tenant.automation_enabled:
                skipped_tenants.append({"tenant_id": tenant.tenant_id, "reason": "automation kill switch is off"})
                continue
            rules = svc.automation_rule_store.list_for_tenant(tenant.tenant_id, enabled_only=True)
            if not rules:
                continue
            tenant_result: dict[str, list[str]] = {"fired": [], "given_up": [], "failed": []}
            for rule in rules:
                outcome = ctx._fire_automation_rule(tenant, rule)
                for key in tenant_result:
                    tenant_result[key].extend(f"{rule.rule_id}:{t}" for t in outcome[key])
            if any(tenant_result.values()):
                processed[tenant.tenant_id] = tenant_result
        return {"processed": processed, "skipped": skipped_tenants}

    # -------------------------------------------------------------- platform admin
    @app.get("/api/v1/admin/tenants")
    def admin_list_tenants(authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")
        tenants = []
        for t in svc.tenant_registry.list_all():
            data = t.model_dump()
            # Onboarding-progress checklist (Phase 8) — lets an admin see
            # exactly where a stuck signup is without opening their
            # dashboard, reusing existing per-tenant stores; no new state.
            data["onboarding"] = {
                "knowledge_sources": len(svc.source_store.list_for_tenant(t.tenant_id)),
                "whatsapp_connected": bool(t.whatsapp_phone_number_id),
                "employees": len(svc.employee_store.list_for_tenant(t.tenant_id)),
                "automation_rules": len(svc.automation_rule_store.list_for_tenant(t.tenant_id)),
            }
            tenants.append(data)
        return {"tenants": tenants}

    @app.post("/api/v1/admin/tenants/{target_tenant_id}/activate")
    def admin_activate(target_tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._require(authorization)
        try:
            authorize(principal, TenantAction.ACTIVATE_TENANT, target_tenant_id=target_tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        blocker = ctx._activation_blocker(target_tenant_id)
        if blocker:
            raise HTTPException(status_code=400, detail=blocker)

        updated = svc.tenant_registry.update_status(target_tenant_id, TenantStatus.ACTIVE)
        ctx._notify_owner_of_activation(updated)
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
        principal = ctx._require(authorization)
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
                amount_inr=request.amount_inr, description=f"Bizistic subscription — {tenant.business_name}",
                customer_name=tenant.business_name, reference_id=target_tenant_id,
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
        principal = ctx._require(authorization)
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

    @app.post("/api/v1/admin/tenants/{target_tenant_id}/set-plan")
    def admin_set_plan(target_tenant_id: str, request: SetPlanRequest, authorization: str | None = Header(default=None)) -> dict:
        """Phase 1 monetization infrastructure: platform_admin-only, same
        as billing-link/mark-paid — a tenant's own owner cannot grant
        itself a paid plan's features for free, so this deliberately
        does NOT go through TenantConfigUpdate (the owner self-service
        schema in tenant_settings_routes.py)."""
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")
        try:
            svc.tenant_registry.get_config(target_tenant_id)
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        updated = svc.tenant_registry.update_config(target_tenant_id, plan=request.plan)
        return updated.model_dump()

    @app.get("/api/v1/admin/tenants/{target_tenant_id}/usage")
    def admin_get_usage(target_tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")
        try:
            tenant = svc.tenant_registry.get_config(target_tenant_id)
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "plan": tenant.plan,
            "period": current_period(),
            "usage": svc.usage_meter_store.get_usage(tenant_id=target_tenant_id),
        }

    @app.post("/api/v1/admin/tenants/{target_tenant_id}/suspend")
    def admin_suspend(target_tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._require(authorization)
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
        principal = ctx._require(authorization)
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
            overdue_lines = ctx._overdue_lines_by_employee(svc.task_store.list_overdue(tenant.tenant_id), employees_by_id)
            recurring_lines = ctx._recurring_feedback_lines(tenant.tenant_id, since_iso=since_iso)
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
            ctx._notify_management_whatsapp(tenant, whatsapp_summary)

        return {"sent": sent, "skipped": skipped, "failed": failed}

    @app.post("/api/v1/admin/weekly-scorecard/run")
    def admin_run_weekly_scorecard(authorization: str | None = Header(default=None)) -> dict:
        """Phase 13: a week-over-week rollup (tasks completed, customer
        dissatisfaction rate, manual sales/expense/collections, top
        recurring feedback theme, open single-point-of-failure risk
        count) built entirely from data every earlier phase already
        collects — no new store. Meant to be triggered weekly by the
        same external-cron convention as every other admin/*/run
        endpoint; unlike the daily digest this has no persistent dedup
        because the caller controls the cadence (calling it twice in one
        week just resends the same week's numbers, which is harmless,
        not a data-integrity problem)."""
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        now = time.time()
        one_week_ago_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7 * 86400))
        two_weeks_ago_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 14 * 86400))

        sent: list[str] = []
        skipped: list[dict] = []
        failed: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "not active"})
                continue

            snapshot = compute_dependency_snapshot(
                tenant.tenant_id, employee_store=svc.employee_store, task_store=svc.task_store,
                sop_store=svc.sop_store, lead_store=svc.lead_store,
            )
            high_severity_count = len([r for r in snapshot["risks"] if r["severity"] == "high"])

            data = build_weekly_scorecard_data(
                tenant.tenant_id, one_week_ago_iso=one_week_ago_iso, two_weeks_ago_iso=two_weeks_ago_iso,
                task_store=svc.task_store, analytics_store=svc.analytics_store, metric_store=svc.metric_store,
                feedback_store=svc.feedback_store, high_severity_risk_count=high_severity_count,
            )
            if not has_scorecard_content(data):
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "no activity this week"})
                continue

            if svc.settings.resend_api_key and svc.settings.digest_from_email:
                subject, html = render_weekly_scorecard_email(tenant, data)
                try:
                    svc.email_sender().send(to=tenant.owner_email, subject=subject, html_body=html)
                except EmailSendError as exc:
                    failed.append({"tenant_id": tenant.tenant_id, "error": str(exc)})

            whatsapp_body = render_weekly_scorecard_whatsapp(tenant, data)
            ctx._notify_management_whatsapp(tenant, whatsapp_body)
            sent.append(tenant.tenant_id)

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
        principal = ctx._require(authorization)
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
        principal = ctx._require(authorization)
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
        principal = ctx._require(authorization)
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
        principal = ctx._require(authorization)
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
            overdue_lines = ctx._overdue_lines_by_employee(to_escalate, employees_by_id)
            sent_count = ctx._notify_management_whatsapp(
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

    @app.post("/api/v1/admin/dependency-scan/run")
    def admin_run_dependency_scan(authorization: str | None = Header(default=None)) -> dict:
        """Phase 10's proactive half of Business Dependency Intelligence:
        computes the same Business Map GET /api/dependency/map renders,
        and notifies the owner/manager roster about any NEW high-severity
        risk (a bus-factor-1 process) they haven't already been told about
        recently. Dedup reuses AuditLogStore exactly like every other
        dedup in this codebase (WhatsAppInboxStore.claim(),
        TaskStore.reminder_sent_at, the Automation Engine's run history) —
        existence of a recent `dependency_risk_flagged` audit entry for
        the same (tenant, target_id) means "already told them, don't
        repeat it every single day this stays true." Medium/low-severity
        risks (workload/knowledge/customer concentration) are surfaced
        on-demand in the dashboard's Business Map, not pushed proactively —
        only a single point of failure is urgent enough to interrupt an
        owner's day over."""
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        renotify_cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - DEPENDENCY_RISK_RENOTIFY_HOURS * 3600)
        )
        notified: list[str] = []
        skipped: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                continue
            snapshot = compute_dependency_snapshot(
                tenant.tenant_id, employee_store=svc.employee_store, task_store=svc.task_store,
                sop_store=svc.sop_store, lead_store=svc.lead_store,
            )
            high_severity = [r for r in snapshot["risks"] if r["severity"] == "high"]
            if not high_severity:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "no high-severity risk"})
                continue

            already_flagged = {
                e.target_id for e in svc.audit_log.list_for_tenant(tenant.tenant_id, action="dependency_risk_flagged")
                if e.created_at >= renotify_cutoff
            }
            new_risks = [r for r in high_severity if r["target_id"] not in already_flagged]
            if not new_risks:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "already flagged recently"})
                continue

            lines = "\n".join(f"• {r['summary']}" for r in new_risks)
            sent_count = ctx._notify_management_whatsapp(
                tenant, f"⚠️ Business dependency risk{'s' if len(new_risks) > 1 else ''} found:\n{lines}"
            )
            for r in new_risks:
                svc.audit_log.record(
                    tenant_id=tenant.tenant_id, actor_employee_id=None, action="dependency_risk_flagged",
                    target_type="dependency_risk", target_id=r["target_id"], metadata={"summary": r["summary"]},
                )
            notified.append(tenant.tenant_id)
            if sent_count == 0:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "no WhatsApp recipient reachable, flagged anyway"})
        return {"notified": notified, "skipped": skipped}

    @app.post("/api/v1/admin/inventory-alert/run")
    def admin_run_inventory_alert(authorization: str | None = Header(default=None)) -> dict:
        """Restaurant Foundation (Phase 17)'s proactive half: notifies the
        owner/manager roster about any ingredient below its configured
        par level. Dedup follows the exact same AuditLogStore-backed
        pattern as the dependency-risk scan above, just with a shorter
        renotify window (INVENTORY_ALERT_RENOTIFY_HOURS = 24h, not 7
        days) — a low-stock ingredient is a same-day problem, so a still-
        low ingredient should be re-flagged daily, not go quiet for a
        week while the kitchen keeps running short."""
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        renotify_cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - INVENTORY_ALERT_RENOTIFY_HOURS * 3600)
        )
        notified: list[str] = []
        skipped: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                continue
            low_stock = svc.inventory_store.list_low_stock(tenant.tenant_id)
            if not low_stock:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "nothing below par level"})
                continue

            already_flagged = {
                e.target_id for e in svc.audit_log.list_for_tenant(tenant.tenant_id, action="inventory_low_stock_flagged")
                if e.created_at >= renotify_cutoff
            }
            new_items = [i for i in low_stock if f"inventory:{i.ingredient_key}" not in already_flagged]
            if not new_items:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "already flagged recently"})
                continue

            lines = "\n".join(f"• {i.ingredient_name}: {i.quantity_on_hand:g}{i.unit} (par {i.par_level:g}{i.unit})" for i in new_items)
            sent_count = ctx._notify_management_whatsapp(tenant, f"⚠️ Low stock:\n{lines}")
            for i in new_items:
                svc.audit_log.record(
                    tenant_id=tenant.tenant_id, actor_employee_id=None, action="inventory_low_stock_flagged",
                    target_type="inventory_item", target_id=f"inventory:{i.ingredient_key}",
                    metadata={"ingredient_name": i.ingredient_name, "quantity_on_hand": i.quantity_on_hand, "par_level": i.par_level},
                )
            notified.append(tenant.tenant_id)
            if sent_count == 0:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "no WhatsApp recipient reachable, flagged anyway"})
        return {"notified": notified, "skipped": skipped}

    @app.post("/api/v1/admin/review-sync/run")
    def admin_run_review_sync(authorization: str | None = Header(default=None)) -> dict:
        """Phase 25 — pulls each active tenant's current Google rating/
        review count and records it as a new snapshot (source=
        "google_places"). Same one-job-per-cron-endpoint convention as
        every other periodic job here — no in-process scheduler. Skips
        gracefully, per-tenant, whenever the platform-level
        GOOGLE_PLACES_API_KEY isn't configured or a tenant hasn't set
        their own google_place_id — never fails the whole run over one
        tenant's missing/invalid configuration."""
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        if not svc.settings.google_places_api_key:
            return {"synced": [], "skipped": [{"tenant_id": "*", "reason": "GOOGLE_PLACES_API_KEY not configured on this platform"}]}

        client = GooglePlacesReviewClient(api_key=svc.settings.google_places_api_key)
        synced: list[str] = []
        skipped: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                continue
            if not tenant.google_place_id:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "no google_place_id configured"})
                continue
            try:
                rating, review_count = client.fetch_rating(tenant.google_place_id)
            except GooglePlacesError as exc:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": str(exc)})
                continue

            previous = svc.review_store.latest_by_platform(tenant.tenant_id).get("google")
            svc.review_store.record(
                tenant_id=tenant.tenant_id, platform="google", rating=rating, review_count=review_count,
                source="google_places",
            )
            synced.append(tenant.tenant_id)

            if previous is not None and (previous.rating - rating) >= RATING_DROP_ALERT_THRESHOLD:
                _send_rating_drop_alert(
                    tenant=tenant, platform="google", previous_rating=previous.rating,
                    new_rating=rating, review_count=review_count,
                )
        return {"synced": synced, "skipped": skipped}

