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
RAZORPAY_PLANS_URL = "https://api.razorpay.com/v1/plans"
RAZORPAY_SUBSCRIPTIONS_URL = "https://api.razorpay.com/v1/subscriptions"


class PaymentLinkError(Exception):
    """Raised when a Razorpay payment link could not be created."""


class SubscriptionError(Exception):
    """Raised when a Razorpay Plan or Subscription could not be created.

    Phase 2 — real recurring billing, additive to (not a replacement
    for) the one-time Payment Links path above, which stays exactly as
    it is for tenant deposit links. Platform subscription billing
    migrates from one-time payment links to this path once Razorpay
    Plan IDs actually exist for each tier (see config.py's
    platform_razorpay_plan_id_* settings) — until then, every caller
    here gets a clear SubscriptionError, never a guess."""


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

    def _post(self, url: str, *, key_id: str, key_secret: str, payload: dict) -> dict:
        credentials = base64.b64encode(f"{key_id}:{key_secret}".encode("utf-8")).decode("ascii")
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise SubscriptionError(f"Razorpay API error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise SubscriptionError(f"Could not reach Razorpay: {exc}") from exc

    def create_plan(
        self, *, key_id: str, key_secret: str, plan_tier: str, amount_inr: int, period: str = "monthly", interval: int = 1
    ) -> str:
        """One-time setup per pricing tier — creates a Razorpay Plan and
        returns its `plan_id`, meant to be saved (e.g. as a
        PLATFORM_RAZORPAY_PLAN_ID_<TIER> env var) and reused for every
        subscription on that tier, not called per-customer. `plan_tier`
        is just a label baked into the plan's item name for readability
        in the Razorpay dashboard — it carries no logic here."""
        if not key_id or not key_secret:
            raise SubscriptionError("Platform Razorpay credentials are not configured.")
        if amount_inr <= 0:
            raise SubscriptionError("Plan amount must be a positive number of rupees.")
        payload = {
            "period": period,
            "interval": interval,
            "item": {
                "name": f"Bizistic {plan_tier.capitalize()}",
                "amount": amount_inr * 100,
                "currency": "INR",
            },
        }
        body = self._post(RAZORPAY_PLANS_URL, key_id=key_id, key_secret=key_secret, payload=payload)
        plan_id = body.get("id")
        if not plan_id:
            raise SubscriptionError("Razorpay did not return a plan id.")
        return plan_id

    def create_subscription(
        self, *, key_id: str, key_secret: str, plan_id: str, total_count: int, reference_id: str | None = None
    ) -> dict:
        """Creates a real recurring Subscription against an existing
        Plan and returns Razorpay's full response — callers need both
        `short_url` (send this to the customer to authorize the
        mandate) and `id` (the subscription_id to store on the tenant
        for future upgrade/downgrade/cancel calls).

        `total_count` is the number of billing cycles Razorpay will run
        before the subscription naturally completes (Razorpay requires
        a finite count, not literally "forever") — e.g. 120 monthly
        cycles is 10 years, a practical stand-in for "until cancelled"
        without inventing a magic "unlimited" value Razorpay doesn't
        accept. `reference_id` is the tenant_id, same correlation-key
        convention as create_payment_link."""
        if not key_id or not key_secret:
            raise SubscriptionError("Platform Razorpay credentials are not configured.")
        if not plan_id:
            raise SubscriptionError("No Razorpay plan_id configured for this tier.")
        payload: dict = {"plan_id": plan_id, "total_count": total_count, "customer_notify": 1}
        if reference_id:
            payload["notes"] = {"reference_id": reference_id}
        body = self._post(RAZORPAY_SUBSCRIPTIONS_URL, key_id=key_id, key_secret=key_secret, payload=payload)
        if not body.get("id") or not body.get("short_url"):
            raise SubscriptionError("Razorpay did not return a subscription id/url.")
        return body


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
