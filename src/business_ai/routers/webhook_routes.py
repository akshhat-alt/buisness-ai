"""Public inbound webhooks other than WhatsApp's (which lives in
routers/admin_bot.py): Razorpay's platform-subscription payment
confirmation (Phase 9 extraction from app.py), plus Phase 18's per-
tenant deposit-payment reconciliation.

Scope, stated honestly: both webhooks here understand the full
`payment_link.*` event family (`paid`, `expired`, `cancelled`,
`partially_paid`) — every event Razorpay fires against a payment link,
all sharing the exact same `payload.payment_link.entity` shape this
codebase already correlates by `reference_id`. `payment.*`/`refund.*`/
`payment.dispute.*` events are deliberately NOT handled: those carry a
different, order-centric payload shape this app has no way to
confidently correlate back to a tenant/lead without a live Razorpay
account to verify the real shape against — guessing that mapping would
risk silently mis-filing a real payment event, which is worse than not
handling it. See README's Known Limitations for this stated boundary.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import FastAPI, HTTPException, Request

from business_ai.payments import verify_razorpay_webhook_signature
from business_ai.tenant import TenantNotFoundError

logger = logging.getLogger(__name__)

# The full payment_link.* lifecycle — every event Razorpay fires against
# a payment link, all sharing payload.payment_link.entity's shape.
_PAYMENT_LINK_EVENTS = frozenset({
    "payment_link.paid", "payment_link.expired", "payment_link.cancelled", "payment_link.partially_paid",
})

# Phase 2 — the full subscription.* lifecycle Razorpay documents (verified
# against their current webhook docs), all sharing payload.subscription.entity's
# shape. Correlated the same way as payment links: a `reference_id` set at
# creation time, here inside `notes` (Subscriptions has no top-level
# reference_id field) — see RazorpayClient.create_subscription.
_SUBSCRIPTION_EVENTS = frozenset({
    "subscription.authenticated", "subscription.activated", "subscription.charged", "subscription.completed",
    "subscription.updated", "subscription.pending", "subscription.halted", "subscription.cancelled",
    "subscription.paused", "subscription.resumed",
})

# subscription.* statuses that mean "the tenant currently has working,
# paid access" — everything else (pending/halted/cancelled/paused) does
# not, and should be treated as a billing problem to alert the owner
# about, not a silent downgrade.
_SUBSCRIPTION_ACTIVE_STATUSES = frozenset({"authenticated", "active", "completed"})


def _handle_subscription_event(svc, event: str, payload: dict) -> dict:
    """Phase 2 — real recurring billing. Correlates via `notes.reference_id`
    (Subscriptions has no top-level reference_id field the way Payment
    Links does — see RazorpayClient.create_subscription). Always records
    Razorpay's own subscription_id/status/current_end on the tenant, so
    that state is visible even before any product decision is made about
    it. Only ever SETS billing_status to "paid" (on an active-status
    event) — deliberately does NOT auto-revoke it on halted/cancelled/
    paused here; per the Master Plan, that's a grace-period decision
    that needs its own design, not a side effect of wiring the webhook.
    Every non-active-status event is still recorded via the audit log,
    so nothing is silently lost."""
    sub_entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    target_tenant_id = (sub_entity.get("notes") or {}).get("reference_id")
    if not target_tenant_id:
        return {"status": "ignored", "reason": "no_matching_tenant"}

    try:
        svc.tenant_registry.get_config(target_tenant_id)
    except TenantNotFoundError:
        logger.warning("Razorpay subscription webhook referenced unknown tenant_id %s", target_tenant_id)
        return {"status": "ignored", "reason": "unknown_tenant"}

    status = sub_entity.get("status")
    current_end = sub_entity.get("current_end")
    fields: dict = {
        "platform_subscription_id": sub_entity.get("id"),
        "platform_subscription_status": status,
    }
    if current_end:
        fields["platform_subscription_current_period_end"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(current_end)
        )
    if status in _SUBSCRIPTION_ACTIVE_STATUSES:
        fields["billing_status"] = "paid"
        fields["billing_paid_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    updated = svc.tenant_registry.update_config(target_tenant_id, **fields)
    svc.audit_log.record(
        tenant_id=target_tenant_id, actor_employee_id=None, action="platform_subscription_event",
        target_type="tenant", target_id=target_tenant_id, metadata={"event": event, "status": status},
    )
    logger.info("Razorpay subscription event %s for tenant %s -> status %s", event, target_tenant_id, status)
    return {"status": "ok", "subscription_status": updated.platform_subscription_status}


def register_webhooks(app: FastAPI, svc, ctx) -> None:

    @app.post("/api/webhooks/razorpay")
    async def receive_razorpay_webhook(req: Request) -> dict:
        """Auto-confirms a platform subscription payment the moment
        Razorpay reports it paid — HMAC-signature-verified, same
        fail-closed pattern as the WhatsApp webhook above. Always returns
        200 for a validly-signed request even when a specific event can't
        be matched to a tenant (an unrelated event type, or a payment
        link this app didn't create) — Razorpay interprets non-200 as
        "retry this", and retry-storming ourselves over a benign mismatch
        would only make things worse. An invalid signature is the one
        case rejected outright. Idempotent: redelivery of an
        already-applied event is a safe no-op."""
        raw_body = await req.body()
        signature = req.headers.get("x-razorpay-signature")
        if not svc.settings.platform_razorpay_webhook_secret or not verify_razorpay_webhook_signature(
            raw_body=raw_body, signature_header=signature, webhook_secret=svc.settings.platform_razorpay_webhook_secret
        ):
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")


        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"status": "ignored", "reason": "invalid_json"}

        event = payload.get("event")

        if event in _SUBSCRIPTION_EVENTS:
            return _handle_subscription_event(svc, event, payload)

        if event not in _PAYMENT_LINK_EVENTS:
            return {"status": "ignored", "reason": "irrelevant_event"}

        link_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
        target_tenant_id = link_entity.get("reference_id")
        if not target_tenant_id:
            return {"status": "ignored", "reason": "no_matching_tenant"}

        try:
            svc.tenant_registry.get_config(target_tenant_id)
        except TenantNotFoundError:
            logger.warning("Razorpay webhook referenced unknown tenant_id %s", target_tenant_id)
            return {"status": "ignored", "reason": "unknown_tenant"}

        if event != "payment_link.paid":
            # expired/cancelled/partially_paid: nothing to reconcile for
            # platform billing (no separate state beyond "unbilled/
            # invoiced/paid" exists to move to) — audit-logged for
            # visibility, same "acknowledge, don't invent a new state
            # machine" discipline as the per-tenant webhook below.
            svc.audit_log.record(
                tenant_id=target_tenant_id, actor_employee_id=None, action="platform_payment_link_event",
                target_type="tenant", target_id=target_tenant_id, metadata={"event": event},
            )
            return {"status": "ok", "reason": "acknowledged_no_state_change"}

        tenant = svc.tenant_registry.get_config(target_tenant_id)
        if link_entity.get("status") != "paid":
            return {"status": "ignored", "reason": "not_actually_paid"}
        if tenant.billing_status == "paid":
            return {"status": "ok", "reason": "already_paid"}

        amount_paise = link_entity.get("amount_paid") or link_entity.get("amount") or 0
        updated = svc.tenant_registry.update_config(
            target_tenant_id, billing_status="paid", billing_paid_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        svc.audit_log.record(
            tenant_id=target_tenant_id, actor_employee_id=None, action="platform_payment_confirmed",
            target_type="tenant", target_id=target_tenant_id, metadata={"amount_inr": amount_paise // 100},
        )
        logger.info("Platform subscription payment auto-confirmed for tenant %s", target_tenant_id)
        return {"status": "ok", "billing_status": updated.billing_status}

    @app.post("/api/webhooks/razorpay/{tenant_id}")
    async def receive_tenant_razorpay_webhook(tenant_id: str, req: Request) -> dict:
        """Phase 18: a tenant's OWN Razorpay account webhook — set up
        once in THEIR Razorpay dashboard, verified against THEIR OWN
        `razorpay_webhook_secret` (never the platform's), closing the
        gap the original README named explicitly ("no payment-status
        webhook... the owner checks their own Razorpay dashboard for
        now"). Correlates via `reference_id` = the lead_id set when the
        deposit link was created (see leads_routes.py's
        send_deposit_link) — auto-confirms the SAME
        `LeadStore.mark_deposit_paid` an owner would otherwise have to
        call by hand via `POST /api/leads/{id}/deposit-paid`."""
        try:
            tenant = svc.tenant_registry.get_config(tenant_id)
        except TenantNotFoundError:
            raise HTTPException(status_code=404, detail="Unknown tenant.")

        raw_body = await req.body()
        signature = req.headers.get("x-razorpay-signature")
        if not tenant.razorpay_webhook_secret or not verify_razorpay_webhook_signature(
            raw_body=raw_body, signature_header=signature, webhook_secret=tenant.razorpay_webhook_secret
        ):
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"status": "ignored", "reason": "invalid_json"}

        event = payload.get("event")
        if event not in _PAYMENT_LINK_EVENTS:
            return {"status": "ignored", "reason": "irrelevant_event"}

        link_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
        lead_id = link_entity.get("reference_id")
        if not lead_id:
            return {"status": "ignored", "reason": "no_matching_lead"}
        lead = svc.lead_store.get(tenant_id, lead_id)
        if lead is None:
            logger.warning("Tenant Razorpay webhook for %s referenced unknown lead_id %s", tenant_id, lead_id)
            return {"status": "ignored", "reason": "unknown_lead"}

        if event != "payment_link.paid":
            svc.audit_log.record(
                tenant_id=tenant_id, actor_employee_id=None, action="deposit_payment_link_event",
                target_type="lead", target_id=lead_id, metadata={"event": event},
            )
            return {"status": "ok", "reason": "acknowledged_no_state_change"}

        if link_entity.get("status") != "paid":
            return {"status": "ignored", "reason": "not_actually_paid"}
        if lead.deposit_paid_at:
            return {"status": "ok", "reason": "already_paid"}

        amount_paise = link_entity.get("amount_paid") or link_entity.get("amount") or 0
        svc.lead_store.mark_deposit_paid(tenant_id, lead_id, amount_paise // 100)
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="deposit_payment_confirmed",
            target_type="lead", target_id=lead_id, metadata={"amount_inr": amount_paise // 100},
        )
        logger.info("Deposit payment auto-confirmed for tenant %s lead %s", tenant_id, lead_id)
        return {"status": "ok", "reason": "deposit_confirmed"}

