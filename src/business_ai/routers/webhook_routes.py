"""Public inbound webhooks other than WhatsApp's (which lives in
routers/admin_bot.py) — currently just Razorpay's platform-subscription
payment confirmation (Phase 9 extraction from app.py).
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import FastAPI, HTTPException, Request

from business_ai.payments import verify_razorpay_webhook_signature
from business_ai.tenant import TenantNotFoundError

logger = logging.getLogger(__name__)


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

        if payload.get("event") != "payment_link.paid":
            return {"status": "ignored", "reason": "irrelevant_event"}

        link_entity = payload.get("payload", {}).get("payment_link", {}).get("entity", {})
        target_tenant_id = link_entity.get("reference_id")
        if not target_tenant_id or link_entity.get("status") != "paid":
            return {"status": "ignored", "reason": "no_matching_tenant_or_not_paid"}

        try:
            tenant = svc.tenant_registry.get_config(target_tenant_id)
        except TenantNotFoundError:
            logger.warning("Razorpay webhook referenced unknown tenant_id %s", target_tenant_id)
            return {"status": "ignored", "reason": "unknown_tenant"}

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

