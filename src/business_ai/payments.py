"""Deposit / booking-payment links via Razorpay's Payment Links API.

Same "bring your own credential" shape as whatsapp.py: each tenant
connects their OWN Razorpay account (key_id + key_secret), so a deposit
a customer pays goes straight into the business's own account — Business
AI never holds, moves, or has custody of a tenant's money. One REST call
with HTTP Basic Auth; no SDK, no webhook, no payment-status reconciliation
in v1 — see README.md for that explicitly-stated scope boundary.
"""

from __future__ import annotations

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

RAZORPAY_API_URL = "https://api.razorpay.com/v1/payment_links"


class PaymentLinkError(Exception):
    """Raised when a Razorpay payment link could not be created."""


class RazorpayClient:
    def create_payment_link(
        self,
        *,
        key_id: str,
        key_secret: str,
        amount_inr: int,
        description: str,
        customer_name: str | None = None,
        customer_phone: str | None = None,
    ) -> str:
        """Returns the short, customer-shareable payment URL."""
        if not key_id or not key_secret:
            raise PaymentLinkError("This business has not connected a Razorpay account yet.")
        if amount_inr <= 0:
            raise PaymentLinkError("Deposit amount must be a positive number of rupees.")

        payload: dict = {
            "amount": amount_inr * 100,  # Razorpay wants paise, the smallest currency unit
            "currency": "INR",
            "accept_partial": False,
            "description": description,
            "notify": {"sms": False, "email": False},  # we deliver the link ourselves, over WhatsApp/email
            "reminder_enable": False,
        }
        customer: dict = {}
        if customer_name:
            customer["name"] = customer_name
        if customer_phone:
            customer["contact"] = f"+{customer_phone}"
        if customer:
            payload["customer"] = customer

        credentials = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("ascii")
        request = Request(
            RAZORPAY_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise PaymentLinkError(f"Razorpay API error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise PaymentLinkError(f"Could not reach Razorpay: {exc}") from exc

        short_url = body.get("short_url")
        if not short_url:
            raise PaymentLinkError("Razorpay did not return a payment link URL.")
        return short_url
