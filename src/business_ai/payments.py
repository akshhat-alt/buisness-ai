"""Payment links via Razorpay's Payment Links API, for two distinct uses:

1. A TENANT's own deposit links (bring-your-own-credential — key_id/
   secret set per tenant from their dashboard; money goes straight into
   THEIR Razorpay account). No webhook, no payment-status reconciliation
   — see README.md's known-limitations section for that explicitly-
   stated scope boundary, which still applies to this path.
2. Business AI's OWN platform subscription billing (Phase 8 —
   PLATFORM_RAZORPAY_KEY_ID/SECRET, the platform's own account). THIS
   path DOES get real webhook confirmation (verify_razorpay_webhook_signature
   below + app.py's POST /api/webhooks/razorpay) — a platform-level
   business decision Business AI can make about its own money, distinct
   from the deliberate choice not to build payment-status reconciliation
   for a tenant's own customer-facing deposits.

Every payment link created by either path carries a `reference_id` (the
tenant_id) so a webhook event can be correlated back to the right tenant
without a second lookup call to Razorpay.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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
        reference_id: str | None = None,
    ) -> str:
        """Returns the short, customer-shareable payment URL.

        `reference_id` is Razorpay's own merchant-reference field, echoed
        back verbatim in every webhook event for this link — the
        correlation key a webhook handler uses to find the right tenant
        without an extra API call. Optional because a tenant's own
        deposit links (no webhook consumer) have no need for it."""
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
        if reference_id:
            payload["reference_id"] = reference_id
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


def verify_razorpay_webhook_signature(*, raw_body: bytes, signature_header: str | None, webhook_secret: str) -> bool:
    """Verifies Razorpay's `X-Razorpay-Signature` header — HMAC-SHA256 of
    the exact raw request bytes, keyed by the webhook secret configured
    in the Razorpay dashboard. Same shape as whatsapp.verify_webhook_signature:
    must run against the RAW body, before any JSON parsing/re-serialization
    could change a single byte and silently break the comparison."""
    if not signature_header or not webhook_secret:
        return False
    expected = hmac.new(webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
