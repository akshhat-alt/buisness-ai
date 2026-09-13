"""Lead capture, review requests, appointments, and deposit-link/
payment-outcome tracking (Phase 9 extraction from app.py).
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Header, HTTPException

from business_ai.customer_intelligence import build_repeat_customer_report
from business_ai.email_sender import EmailSendError
from business_ai.formatting import _format_appointment_ist, _parse_appointment_to_utc
from business_ai.leads import Lead, lead_stage
from business_ai.whatsapp import REENGAGEMENT_WINDOW_CLOSED_CODE, WhatsAppSendError
from business_ai.alerts import render_review_request
from business_ai.payments import PaymentLinkError
from business_ai.schemas import (
    AppointmentOutcomeRequest,
    AppointmentRequest,
    DepositPaidRequest,
    LeadRequest,
)
from business_ai.tenant import TenantAction, TenantNotActiveError, TenantNotFoundError, UnauthorizedError, authorize


def register_leads(app: FastAPI, svc, ctx) -> None:

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
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_LEADS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"leads": [{**l.model_dump(), "stage": lead_stage(l)} for l in svc.lead_store.list_for_tenant(tenant_id)]}

    @app.post("/api/leads/{lead_id}/request-review")
    def request_review(lead_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
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

    @app.get("/api/leads/upcoming-appointments")
    def list_upcoming_appointments(
        tenant_id: str, authorization: str | None = Header(default=None), within_hours: float = 24,
    ) -> dict:
        """Phase 23's reservations-book view — any lead with a future
        appointment inside the window that hasn't been confirmed as any
        outcome yet. Gated the same as viewing leads themselves."""
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_LEADS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        leads = svc.lead_store.list_upcoming_appointments(tenant_id, within_hours=within_hours)
        return {"leads": [lead.model_dump() for lead in leads]}

    @app.put("/api/leads/{lead_id}/appointment")
    def set_lead_appointment(
        lead_id: str, request: AppointmentRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        """Manual for now — there's no live calendar/slot booking yet
        (a deliberately deferred, much larger feature). This is the
        foundation both reminders and win-back read from."""
        principal = ctx._resolve(authorization)
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

        updated = svc.lead_store.set_appointment(tenant_id, lead_id, appointment_utc, party_size=request.party_size)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"No lead '{lead_id}' for this business.")
        return updated.model_dump()

    @app.post("/api/leads/{lead_id}/deposit-link")
    def send_deposit_link(lead_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
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
                # Phase 18: correlates this payment link back to the exact
                # lead for POST /api/webhooks/razorpay/{tenant_id} — a
                # tenant with no webhook secret configured simply never
                # receives this event; nothing about the manual
                # request-and-confirm flow changes for them.
                reference_id=lead_id,
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

    @app.post("/api/leads/{lead_id}/nudge")
    def nudge_lead(lead_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Phase 18 — a one-tap Revenue Radar recovery action: manually
        re-engage a lead who showed buying intent but never booked,
        on demand, using the EXACT same message the automated
        reengagement cron sends (see admin_routes.py's
        admin_run_reengagement) so a manual nudge and an automatic one
        are indistinguishable to the customer. WhatsApp-only, like the
        automated version — a lead with no WhatsApp number/connection
        gets a clear error, not a silent no-op."""
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.VIEW_LEADS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        lead = svc.lead_store.get(tenant_id, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail=f"No lead '{lead_id}' for this business.")
        if not lead.phone:
            raise HTTPException(status_code=400, detail="This lead has no WhatsApp number to nudge.")
        if not (tenant.whatsapp_phone_number_id and tenant.whatsapp_access_token):
            raise HTTPException(status_code=400, detail="Connect a WhatsApp number first.")

        body = (
            f"Hi! This is {tenant.assistant_name} from {tenant.business_name}. Just checking in on your "
            "recent message — happy to help you book, or answer anything else!"
        )
        try:
            svc.whatsapp_client().send_text(
                phone_number_id=tenant.whatsapp_phone_number_id, access_token=tenant.whatsapp_access_token,
                to=lead.phone, body=body,
            )
        except WhatsAppSendError as exc:
            raise HTTPException(status_code=502, detail=f"Could not send the nudge: {exc}") from exc
        svc.lead_store.mark_reengaged(tenant_id, lead_id)
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="lead_nudged_manually",
            target_type="lead", target_id=lead_id, metadata={},
        )
        return {"sent_to": lead.phone, "channel": "whatsapp"}

    @app.post("/api/leads/{lead_id}/deposit-paid")
    def mark_lead_deposit_paid(
        lead_id: str, request: DepositPaidRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        """Owner-confirmed only — the exact same request-and-confirm
        shape as platform billing's mark-paid (app.py's admin_mark_paid),
        just at the tenant-customer level. A deposit LINK being sent
        never implies payment; this is the one action that does."""
        principal = ctx._resolve(authorization)
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
        principal = ctx._resolve(authorization)
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

    @app.get("/api/customer-behavior")
    def get_customer_behavior(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Phase 23: repeat-customer intelligence, pure computation over
        leads that already exist — gated the same as viewing leads
        themselves, since this is just a different lens on the same
        data a caller can already see row by row."""
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_LEADS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        report = build_repeat_customer_report(tenant_id, lead_store=svc.lead_store)
        return report.model_dump()

