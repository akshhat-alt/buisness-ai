"""Tenant self-service settings: public/authenticated tenant info,
config updates, WhatsApp Embedded Signup, self-serve activation, and
self-serve platform-subscription billing checkout (Phase 9 extraction
from app.py).
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Header, HTTPException

from business_ai.leads import lead_stage
from business_ai.payments import PaymentLinkError
from business_ai.schemas import EmbeddedSignupRequest, PlanUpgradeRequest, TenantConfigUpdate, TenantDeleteRequest
from business_ai.tenant import TenantAction, TenantNotFoundError, TenantStatus, UnauthorizedError, authorize
from business_ai.tenant_data import delete_tenant_data, export_tenant_data
from business_ai.usage_meter import current_period
from business_ai.whatsapp import MetaEmbeddedSignupError

logger = logging.getLogger(__name__)


def register_tenant_settings(app: FastAPI, svc, ctx) -> None:

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
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.VIEW_ANALYTICS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return tenant.model_dump()

    @app.put("/api/tenant")
    def update_tenant(request: TenantConfigUpdate, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_ASSISTANT, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        fields = {k: v for k, v in request.model_dump().items() if v is not None}
        updated = svc.tenant_registry.update_config(tenant_id, **fields)
        return updated.model_dump()

    @app.get("/api/tenant/usage")
    def get_tenant_usage(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Phase 1 monetization infrastructure: the owner's own current
        plan and this month's usage counters — same authorization tier
        as viewing the tenant itself (VIEW_ANALYTICS), since knowing
        your own usage isn't a sensitive lever the way changing it is."""
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.VIEW_ANALYTICS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "plan": tenant.plan,
            "period": current_period(),
            "usage": svc.usage_meter_store.get_usage(tenant_id=tenant_id),
        }

    @app.get("/api/tenant/whatsapp/embedded-signup-status")
    def whatsapp_embedded_signup_status() -> dict:
        """Unauthenticated, read-only: whether the "Connect WhatsApp"
        one-click flow can even be offered on this deployment yet. The
        dashboard uses this to decide whether to show that button at
        all, rather than showing it and failing on click — this is a
        platform-wide capability flag, not tenant data. app_id/config_id
        are both public, non-secret identifiers Meta's own JS SDK needs
        client-side (never the app SECRET, which never leaves the
        server) — see MetaEmbeddedSignupClient's docstring."""
        available = bool(svc.settings.whatsapp_app_id and svc.settings.whatsapp_app_secret and svc.settings.whatsapp_config_id)
        return {
            "available": available,
            "app_id": svc.settings.whatsapp_app_id if available else None,
            "config_id": svc.settings.whatsapp_config_id if available else None,
            "api_version": svc.settings.whatsapp_api_version,
        }

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
        principal = ctx._resolve(authorization)
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

    @app.post("/api/tenant/activate")
    def self_activate_tenant(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Lets the OWNER activate their own tenant once they've cleared
        the same bar an admin would check — knowledge ingested, and paid
        if a subscription price was set. Removes the founder from the
        loop as the default path; admin activation (above) still works
        unchanged for anyone who wants to do it by hand, and `suspend`
        remains the escape hatch if a self-activated tenant needs to be
        pulled back."""
        principal = ctx._resolve(authorization)
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

        blocker = ctx._activation_blocker(tenant_id)
        if blocker:
            raise HTTPException(status_code=400, detail=blocker)

        updated = svc.tenant_registry.update_status(tenant_id, TenantStatus.ACTIVE)
        ctx._notify_admin_of_self_activation(updated)
        return updated.model_dump()

    @app.get("/api/platform/plan")
    def get_platform_plan() -> dict:
        """Public, read-only capability flag — the self-serve subscription
        price for this deployment, if the operator has configured one.
        Same shape as GET /api/tenant/whatsapp/embedded-signup-status: the
        onboarding wizard uses this to decide whether to show a payment
        step at all, rather than showing one and failing on click. None
        means this deployment doesn't charge for onboarding yet — every
        tenant activates for free, exactly like before this endpoint
        existed."""
        return {"price_inr": svc.settings.platform_subscription_price_inr}

    @app.post("/api/tenant/plan/upgrade-request")
    def request_plan_upgrade(
        request: PlanUpgradeRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        """Owner-facing request to upgrade to Growth or Scale plan.
        Notifies platform admin via email; does not change plan directly or touch billing."""
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.MANAGE_ASSISTANT, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        target = request.target_plan.lower().strip()
        if target not in {"growth", "scale"}:
            raise HTTPException(status_code=400, detail="Invalid target plan. Must be 'growth' or 'scale'.")
        if tenant.plan == target:
            raise HTTPException(status_code=400, detail=f"Already on the {target} plan.")
        if tenant.plan == "scale" and target == "growth":
            raise HTTPException(status_code=400, detail="Cannot request downgrade via upgrade request.")

        if hasattr(ctx, "_notify_admin_of_upgrade_request"):
            ctx._notify_admin_of_upgrade_request(tenant, target, request.note)

        logger.info("Plan upgrade requested for tenant %s to %s by %s", tenant_id, target, principal.principal_id)
        return {
            "status": "received",
            "tenant_id": tenant_id,
            "current_plan": tenant.plan,
            "target_plan": target,
            "message": f"Upgrade request for {target.title()} plan received. Our team has been notified and will reach out shortly.",
        }


    @app.post("/api/tenant/billing/checkout")
    def self_serve_billing_checkout(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Lets the OWNER generate their own platform-subscription payment
        link — the self-serve equivalent of an admin's billing-link
        action, at the operator-configured price ONLY (never a caller-
        supplied amount, so an owner can never set their own price).
        Confirmation comes from the Razorpay webhook below when
        PLATFORM_RAZORPAY_WEBHOOK_SECRET is configured; otherwise an
        admin still confirms manually via mark-paid, unaffected by this
        endpoint's existence."""
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.MANAGE_ASSISTANT, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        price = svc.settings.platform_subscription_price_inr
        if not price:
            raise HTTPException(status_code=400, detail="This deployment doesn't require payment to activate.")
        if tenant.billing_status == "paid":
            return {"payment_url": None, "billing_status": "paid"}
        if not (svc.settings.platform_razorpay_key_id and svc.settings.platform_razorpay_key_secret):
            raise HTTPException(status_code=400, detail="Payment isn't configured on this deployment yet — contact support.")

        try:
            payment_url = svc.razorpay_client().create_payment_link(
                key_id=svc.settings.platform_razorpay_key_id, key_secret=svc.settings.platform_razorpay_key_secret,
                amount_inr=price, description=f"Bizistic subscription — {tenant.business_name}",
                customer_name=tenant.business_name, reference_id=tenant_id,
            )
        except PaymentLinkError as exc:
            raise HTTPException(status_code=502, detail=f"Could not create payment link: {exc}") from exc

        updated = svc.tenant_registry.update_config(
            tenant_id, subscription_price_inr=price, billing_status="invoiced",
            billing_link_sent_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        return {"payment_url": payment_url, "billing_status": updated.billing_status}

    @app.get("/api/tenant/export")
    def export_tenant(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Phase 9: every tenant-scoped record this app holds, as one
        JSON document — the data-portability half of the compliance
        baseline. Connection secrets (WhatsApp access token, Razorpay key
        secret) are deliberately excluded: they're operational
        credentials, not the owner's business data, and exporting them
        would be a real security exposure of their own."""
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_ASSISTANT, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return export_tenant_data(svc, tenant_id)

    @app.post("/api/tenant/delete")
    def delete_tenant(
        request: TenantDeleteRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        """Phase 9: permanent, irreversible deletion of every record this
        app holds for this tenant — the erasure half of the compliance
        baseline. Gated the same as every other owner-only policy action
        PLUS a real confirmation: the caller must type the business's
        CURRENT name exactly, the same "type the name to confirm" bar
        every serious platform holds a destructive action to. There is
        no undo — `suspend` (admin-only) remains the reversible escape
        hatch for "stop this account" short of actually deleting it."""
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.MANAGE_ASSISTANT, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if request.confirm_business_name != tenant.business_name:
            raise HTTPException(
                status_code=400,
                detail="confirm_business_name must exactly match this business's current name to proceed.",
            )
        deleted = delete_tenant_data(svc, tenant_id)
        logger.warning("Tenant %s permanently deleted by %s, rows removed: %s", tenant_id, principal.principal_id, deleted)
        return {"deleted": True, "rows_removed": deleted}
