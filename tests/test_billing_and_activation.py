"""Tests for Business AI's own subscription billing and owner self-serve
activation — the two onboarding gaps identified in the product/onboarding
review: no revenue-collection path existed, and activation required a
platform admin to click a button for every single new business.

Billing reuses the exact same RazorpayClient/payment-link pattern as
tenant deposit links, just with PLATFORM credentials instead of the
tenant's own. Self-activation reuses the exact same knowledge-ingested
check the admin path already enforced, plus (new) a payment gate shared
by both paths via app.py's _activation_blocker.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json

import pytest

from business_ai.app import Services, create_app
from business_ai.payments import PaymentLinkError
from business_ai.retrieval import HashEmbeddingProvider
from tests.conftest import FakeEmailSender, FakeGenerator


class FakeRazorpayClient:
    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.raise_error = raise_error

    def create_payment_link(self, **kwargs) -> str:
        if self.raise_error:
            raise self.raise_error
        self.calls.append(kwargs)
        return "https://rzp.io/i/fake-subscription-link"


@pytest.fixture()
def services_billing(tmp_path, settings):
    settings_billing = dataclasses.replace(
        settings,
        resend_api_key="re_test", digest_from_email="digest@example.com",
        platform_razorpay_key_id="rzp_platform_key", platform_razorpay_key_secret="rzp_platform_secret",
        platform_admin_email="admin@business-ai.example",
    )
    svc = Services(settings_billing, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    fake_rzp = FakeRazorpayClient()
    svc.razorpay_client = lambda: fake_rzp
    svc.fake_razorpay_client = fake_rzp
    fake_email = FakeEmailSender()
    svc.email_sender = lambda: fake_email
    svc.fake_email_sender = fake_email
    yield svc
    svc.vector_store.close()


@pytest.fixture()
def client_billing(services_billing):
    from fastapi.testclient import TestClient

    return TestClient(create_app(services_billing))


def _signup(client, business_name="Priya Salon", email="owner@example.com"):
    r = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "secret123", "name": "Priya", "business_name": business_name},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["tenant_id"]


def _admin_headers(client, settings):
    r = client.post("/api/auth/login", json={"email": "admin", "password": settings.admin_secret})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _add_knowledge(client, headers, tenant_id):
    r = client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    assert r.status_code == 200, r.text


# ============================================================== billing link


def test_admin_sends_billing_link_and_updates_tenant(client_billing, services_billing):
    headers, tenant_id = _signup(client_billing)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    services_billing.fake_email_sender.sent.clear()  # discard the new-signup admin notification

    r = client_billing.post(
        f"/api/v1/admin/tenants/{tenant_id}/billing-link", json={"amount_inr": 999}, headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["sent_to"] == "owner@example.com"
    assert data["payment_url"] == "https://rzp.io/i/fake-subscription-link"
    assert data["billing_status"] == "invoiced"

    assert len(services_billing.fake_razorpay_client.calls) == 1
    assert services_billing.fake_razorpay_client.calls[0]["amount_inr"] == 999
    assert len(services_billing.fake_email_sender.sent) == 1
    assert services_billing.fake_email_sender.sent[0]["to"] == "owner@example.com"
    assert "999" in services_billing.fake_email_sender.sent[0]["html_body"]

    tenant = client_billing.get(f"/api/tenant?tenant_id={tenant_id}", headers=headers).json()
    assert tenant["subscription_price_inr"] == 999
    assert tenant["billing_status"] == "invoiced"
    assert tenant["billing_link_sent_at"] is not None


def test_billing_link_requires_platform_admin_role(client_billing, services_billing):
    headers, tenant_id = _signup(client_billing)
    r = client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/billing-link", json={"amount_inr": 999}, headers=headers)
    assert r.status_code == 403


def test_billing_link_rejected_when_platform_razorpay_not_configured(client_billing, services_billing):
    services_billing.settings = dataclasses.replace(
        services_billing.settings, platform_razorpay_key_id=None, platform_razorpay_key_secret=None,
    )
    headers, tenant_id = _signup(client_billing)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    r = client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/billing-link", json={"amount_inr": 999}, headers=admin_headers)
    assert r.status_code == 400
    assert "razorpay" in r.json()["detail"].lower()
    assert services_billing.fake_razorpay_client.calls == []


def test_billing_link_surfaces_razorpay_error(client_billing, services_billing):
    services_billing.fake_razorpay_client.raise_error = PaymentLinkError("Razorpay API error 401: invalid key")
    headers, tenant_id = _signup(client_billing)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    services_billing.fake_email_sender.sent.clear()  # discard the new-signup admin notification
    r = client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/billing-link", json={"amount_inr": 999}, headers=admin_headers)
    assert r.status_code == 502
    assert "invalid key" in r.json()["detail"]
    assert services_billing.fake_email_sender.sent == []  # never sent an email for a link that was never created


def test_admin_mark_paid_updates_billing_status(client_billing, services_billing):
    headers, tenant_id = _signup(client_billing)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/billing-link", json={"amount_inr": 999}, headers=admin_headers)

    r = client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/mark-paid", json={}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["billing_status"] == "paid"
    assert r.json()["billing_paid_at"] is not None


def test_mark_paid_can_set_price_directly(client_billing, services_billing):
    """Admin can record an out-of-band payment (e.g. bank transfer)
    without ever sending a billing link through the product."""
    headers, tenant_id = _signup(client_billing)
    admin_headers = _admin_headers(client_billing, services_billing.settings)

    r = client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/mark-paid", json={"amount_inr": 1499}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["subscription_price_inr"] == 1499
    assert r.json()["billing_status"] == "paid"


# ============================================================== activation gated on billing


def test_admin_activation_blocked_when_priced_and_unpaid(client_billing, services_billing):
    headers, tenant_id = _signup(client_billing)
    _add_knowledge(client_billing, headers, tenant_id)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/billing-link", json={"amount_inr": 999}, headers=admin_headers)

    r = client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
    assert r.status_code == 400
    assert "payment" in r.json()["detail"].lower()


def test_admin_activation_succeeds_once_paid(client_billing, services_billing):
    headers, tenant_id = _signup(client_billing)
    _add_knowledge(client_billing, headers, tenant_id)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/billing-link", json={"amount_inr": 999}, headers=admin_headers)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/mark-paid", json={}, headers=admin_headers)

    r = client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


def test_activation_unaffected_when_tenant_was_never_priced(client_billing, services_billing):
    """Backward compatible: a tenant nobody ever billed activates exactly
    like before this feature existed."""
    headers, tenant_id = _signup(client_billing)
    _add_knowledge(client_billing, headers, tenant_id)
    admin_headers = _admin_headers(client_billing, services_billing.settings)

    r = client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
    assert r.status_code == 200, r.text


# ============================================================== owner self-activation


def test_owner_can_self_activate_once_knowledge_and_payment_are_done(client_billing, services_billing):
    headers, tenant_id = _signup(client_billing)
    _add_knowledge(client_billing, headers, tenant_id)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/mark-paid", json={"amount_inr": 999}, headers=admin_headers)

    r = client_billing.post(f"/api/tenant/activate?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    # Admin was notified for visibility, not required to act.
    assert len(services_billing.fake_email_sender.sent) >= 1
    assert any("activated" in s["subject"].lower() for s in services_billing.fake_email_sender.sent)


def test_self_activation_blocked_without_knowledge(client_billing, services_billing):
    headers, tenant_id = _signup(client_billing)
    r = client_billing.post(f"/api/tenant/activate?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert "knowledge" in r.json()["detail"].lower()


def test_self_activation_blocked_when_priced_and_unpaid(client_billing, services_billing):
    headers, tenant_id = _signup(client_billing)
    _add_knowledge(client_billing, headers, tenant_id)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/billing-link", json={"amount_inr": 999}, headers=admin_headers)

    r = client_billing.post(f"/api/tenant/activate?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert "payment" in r.json()["detail"].lower()


def test_self_activation_blocked_by_platform_price_even_without_checkout(client_billing, services_billing):
    """The actual production bug: _activation_blocker used to check ONLY
    tenant.subscription_price_inr, a field that stays None for a
    self-serve signup unless /api/tenant/billing/checkout was called at
    least once. A tenant who skipped straight to Activate without ever
    clicking "Pay Now" activated for free, even with a real platform
    price configured — this is the exploit path the fix closes."""
    services_billing.settings = dataclasses.replace(services_billing.settings, platform_subscription_price_inr=2000)
    headers, tenant_id = _signup(client_billing)
    _add_knowledge(client_billing, headers, tenant_id)
    # Deliberately never calls /api/tenant/billing/checkout.

    r = client_billing.post(f"/api/tenant/activate?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400, r.text
    assert "payment" in r.json()["detail"].lower()


def test_self_activation_succeeds_after_platform_price_checkout_and_webhook_payment(client_billing, services_billing):
    """The legitimate counterpart: once the platform-priced checkout link
    is actually paid (tenant.subscription_price_inr gets set by checkout,
    billing_status flips via mark-paid/webhook), self-activation works."""
    services_billing.settings = dataclasses.replace(services_billing.settings, platform_subscription_price_inr=2000)
    headers, tenant_id = _signup(client_billing)
    _add_knowledge(client_billing, headers, tenant_id)
    client_billing.post(f"/api/tenant/billing/checkout?tenant_id={tenant_id}", headers=headers)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/mark-paid", json={}, headers=admin_headers)

    r = client_billing.post(f"/api/tenant/activate?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


def test_admin_activation_also_blocked_by_platform_price_alone(client_billing, services_billing):
    """Same fix, admin-triggered path — both activation entry points
    share the one _activation_blocker function."""
    services_billing.settings = dataclasses.replace(services_billing.settings, platform_subscription_price_inr=2000)
    headers, tenant_id = _signup(client_billing)
    _add_knowledge(client_billing, headers, tenant_id)
    admin_headers = _admin_headers(client_billing, services_billing.settings)

    r = client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
    assert r.status_code == 400, r.text
    assert "payment" in r.json()["detail"].lower()


def test_self_activation_is_tenant_isolated(client_billing, services_billing):
    headers_a, tenant_a = _signup(client_billing, business_name="Salon A", email="a@example.com")
    headers_b, tenant_b = _signup(client_billing, business_name="Salon B", email="b@example.com")
    _add_knowledge(client_billing, headers_a, tenant_a)

    r = client_billing.post(f"/api/tenant/activate?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_self_activation_blocked_when_suspended(client_billing, services_billing):
    headers, tenant_id = _signup(client_billing)
    _add_knowledge(client_billing, headers, tenant_id)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/suspend", headers=admin_headers)

    r = client_billing.post(f"/api/tenant/activate?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 403
    assert "suspended" in r.json()["detail"].lower()


def test_self_activation_rejects_already_active(client_billing, services_billing):
    headers, tenant_id = _signup(client_billing)
    _add_knowledge(client_billing, headers, tenant_id)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)

    r = client_billing.post(f"/api/tenant/activate?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert "already active" in r.json()["detail"].lower()


def test_self_activation_notice_skipped_gracefully_when_admin_email_unset(client, owner_session):
    """Uses the plain `client`/`owner_session` fixtures (no platform admin
    email configured) to prove self-activation still succeeds without
    crashing when there's nobody configured to notify."""
    headers, tenant_id = owner_session
    r = client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    assert r.status_code == 200, r.text

    r = client.post(f"/api/tenant/activate?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text


# ============================================================== Phase 8: self-serve plan/payment


def test_platform_plan_reports_no_price_by_default(client_billing, services_billing):
    r = client_billing.get("/api/platform/plan")
    assert r.status_code == 200, r.text
    assert r.json()["price_inr"] is None


def test_platform_plan_reports_configured_price(client_billing, services_billing):
    services_billing.settings = dataclasses.replace(services_billing.settings, platform_subscription_price_inr=1499)
    r = client_billing.get("/api/platform/plan")
    assert r.status_code == 200, r.text
    assert r.json()["price_inr"] == 1499


def test_checkout_rejected_when_no_price_configured(client_billing, services_billing):
    """Backward compatible: a deployment that hasn't set a self-serve
    price simply doesn't offer this step — no invented pricing."""
    headers, tenant_id = _signup(client_billing)
    r = client_billing.post(f"/api/tenant/billing/checkout?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert "payment" in r.json()["detail"].lower()
    assert services_billing.fake_razorpay_client.calls == []


def test_checkout_rejected_when_platform_razorpay_not_configured(client_billing, services_billing):
    services_billing.settings = dataclasses.replace(
        services_billing.settings, platform_subscription_price_inr=999,
        platform_razorpay_key_id=None, platform_razorpay_key_secret=None,
    )
    headers, tenant_id = _signup(client_billing)
    r = client_billing.post(f"/api/tenant/billing/checkout?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert "payment" in r.json()["detail"].lower()


def test_checkout_generates_real_link_with_tenant_reference_id(client_billing, services_billing):
    services_billing.settings = dataclasses.replace(services_billing.settings, platform_subscription_price_inr=999)
    headers, tenant_id = _signup(client_billing)

    r = client_billing.post(f"/api/tenant/billing/checkout?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["payment_url"] == "https://rzp.io/i/fake-subscription-link"
    assert data["billing_status"] == "invoiced"

    call = services_billing.fake_razorpay_client.calls[0]
    assert call["amount_inr"] == 999
    assert call["reference_id"] == tenant_id

    tenant = client_billing.get(f"/api/tenant?tenant_id={tenant_id}", headers=headers).json()
    assert tenant["subscription_price_inr"] == 999
    assert tenant["billing_status"] == "invoiced"


def test_checkout_owner_cannot_set_their_own_price(client_billing, services_billing):
    """Even if a caller tries to smuggle an amount in, checkout always
    uses the operator-configured platform price."""
    services_billing.settings = dataclasses.replace(services_billing.settings, platform_subscription_price_inr=999)
    headers, tenant_id = _signup(client_billing)

    r = client_billing.post(
        f"/api/tenant/billing/checkout?tenant_id={tenant_id}", json={"amount_inr": 1}, headers=headers,
    )
    assert r.status_code == 200, r.text
    assert services_billing.fake_razorpay_client.calls[0]["amount_inr"] == 999


def test_checkout_is_owner_gated_and_tenant_isolated(client_billing, services_billing):
    services_billing.settings = dataclasses.replace(services_billing.settings, platform_subscription_price_inr=999)
    headers_a, tenant_a = _signup(client_billing, business_name="Salon A", email="a2@example.com")
    headers_b, tenant_b = _signup(client_billing, business_name="Salon B", email="b2@example.com")

    r = client_billing.post(f"/api/tenant/billing/checkout?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_checkout_short_circuits_when_already_paid(client_billing, services_billing):
    services_billing.settings = dataclasses.replace(services_billing.settings, platform_subscription_price_inr=999)
    headers, tenant_id = _signup(client_billing)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/mark-paid", json={}, headers=admin_headers)

    r = client_billing.post(f"/api/tenant/billing/checkout?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"payment_url": None, "billing_status": "paid"}
    assert services_billing.fake_razorpay_client.calls == []


def test_checkout_surfaces_razorpay_error(client_billing, services_billing):
    services_billing.settings = dataclasses.replace(services_billing.settings, platform_subscription_price_inr=999)
    services_billing.fake_razorpay_client.raise_error = PaymentLinkError("Razorpay API error 401: invalid key")
    headers, tenant_id = _signup(client_billing)

    r = client_billing.post(f"/api/tenant/billing/checkout?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 502
    assert "invalid key" in r.json()["detail"]


# ============================================================== Phase 8: Razorpay webhook auto-confirmation


WEBHOOK_SECRET = "test-razorpay-webhook-secret"


def _razorpay_webhook_payload(*, tenant_id: str, status: str = "paid", amount_paise: int = 99900) -> dict:
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment_link.paid",
        "contains": ["payment_link", "payment"],
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_test123",
                    "reference_id": tenant_id,
                    "amount": amount_paise,
                    "amount_paid": amount_paise,
                    "status": status,
                }
            },
        },
        "created_at": 1234567890,
    }


def _signed_webhook_post(client, payload: dict, *, secret: str = WEBHOOK_SECRET):
    raw_body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/webhooks/razorpay", content=raw_body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )


@pytest.fixture()
def services_webhook(services_billing):
    services_billing.settings = dataclasses.replace(
        services_billing.settings, platform_subscription_price_inr=999,
        platform_razorpay_webhook_secret=WEBHOOK_SECRET,
    )
    return services_billing


@pytest.fixture()
def client_webhook(services_webhook):
    from fastapi.testclient import TestClient

    return TestClient(create_app(services_webhook))


def test_webhook_auto_confirms_payment_and_writes_audit_entry(client_webhook, services_webhook):
    headers, tenant_id = _signup(client_webhook)
    client_webhook.post(f"/api/tenant/billing/checkout?tenant_id={tenant_id}", headers=headers)

    r = _signed_webhook_post(client_webhook, _razorpay_webhook_payload(tenant_id=tenant_id))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"

    tenant = client_webhook.get(f"/api/tenant?tenant_id={tenant_id}", headers=headers).json()
    assert tenant["billing_status"] == "paid"
    assert tenant["billing_paid_at"] is not None

    audit = services_webhook.audit_log.list_for_tenant(tenant_id, action="platform_payment_confirmed")
    assert len(audit) == 1
    assert audit[0].metadata["amount_inr"] == 999


def test_webhook_activation_now_genuinely_one_click(client_webhook, services_webhook):
    """The point of the webhook: knowledge + checkout + a real payment
    event is enough to self-activate, with zero admin involvement."""
    headers, tenant_id = _signup(client_webhook)
    _add_knowledge(client_webhook, headers, tenant_id)
    client_webhook.post(f"/api/tenant/billing/checkout?tenant_id={tenant_id}", headers=headers)
    _signed_webhook_post(client_webhook, _razorpay_webhook_payload(tenant_id=tenant_id))

    r = client_webhook.post(f"/api/tenant/activate?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


def test_webhook_rejects_invalid_signature(client_webhook, services_webhook):
    headers, tenant_id = _signup(client_webhook)
    r = _signed_webhook_post(client_webhook, _razorpay_webhook_payload(tenant_id=tenant_id), secret="wrong-secret")
    assert r.status_code == 401

    tenant = client_webhook.get(f"/api/tenant?tenant_id={tenant_id}", headers=headers).json()
    assert tenant["billing_status"] == "unbilled"


def test_webhook_fails_closed_when_secret_not_configured(client_billing, services_billing):
    """No PLATFORM_RAZORPAY_WEBHOOK_SECRET configured at all — every event
    is rejected outright rather than trusted on a missing check."""
    headers, tenant_id = _signup(client_billing)
    raw_body = json.dumps(_razorpay_webhook_payload(tenant_id=tenant_id)).encode("utf-8")
    signature = hmac.new(b"whatever", raw_body, hashlib.sha256).hexdigest()
    r = client_billing.post(
        "/api/webhooks/razorpay", content=raw_body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )
    assert r.status_code == 401


def test_webhook_ignores_irrelevant_event_type(client_webhook, services_webhook):
    """A genuinely unrelated Razorpay event (not in the payment_link.*
    family this webhook understands — see Phase 18's own scope note)
    stays a safe, unactioned no-op."""
    headers, tenant_id = _signup(client_webhook)
    payload = _razorpay_webhook_payload(tenant_id=tenant_id)
    payload["event"] = "payment.captured"
    r = _signed_webhook_post(client_webhook, payload)
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "irrelevant_event"

    tenant = client_webhook.get(f"/api/tenant?tenant_id={tenant_id}", headers=headers).json()
    assert tenant["billing_status"] == "unbilled"


def test_webhook_acknowledges_a_recognized_non_paid_payment_link_event(client_webhook, services_webhook):
    """Phase 18: payment_link.expired/cancelled/partially_paid are now
    recognized (not "irrelevant") but correctly change no billing
    state — there's nothing to reconcile beyond unbilled/invoiced/paid."""
    headers, tenant_id = _signup(client_webhook)
    payload = _razorpay_webhook_payload(tenant_id=tenant_id)
    payload["event"] = "payment_link.expired"
    r = _signed_webhook_post(client_webhook, payload)
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "acknowledged_no_state_change"

    tenant = client_webhook.get(f"/api/tenant?tenant_id={tenant_id}", headers=headers).json()
    assert tenant["billing_status"] == "unbilled"


def test_webhook_ignores_unknown_tenant_reference(client_webhook, services_webhook):
    r = _signed_webhook_post(client_webhook, _razorpay_webhook_payload(tenant_id="no-such-tenant"))
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "unknown_tenant"


def test_webhook_redelivery_is_idempotent(client_webhook, services_webhook):
    headers, tenant_id = _signup(client_webhook)
    client_webhook.post(f"/api/tenant/billing/checkout?tenant_id={tenant_id}", headers=headers)

    _signed_webhook_post(client_webhook, _razorpay_webhook_payload(tenant_id=tenant_id))
    r2 = _signed_webhook_post(client_webhook, _razorpay_webhook_payload(tenant_id=tenant_id))
    assert r2.status_code == 200, r2.text
    assert r2.json()["reason"] == "already_paid"

    audit = services_webhook.audit_log.list_for_tenant(tenant_id, action="platform_payment_confirmed")
    assert len(audit) == 1  # not duplicated on redelivery


def test_webhook_ignores_unpaid_status(client_webhook, services_webhook):
    """Phase 18: the reason string is now the more precise
    "not_actually_paid" (a real, matched tenant whose link just isn't
    paid yet) rather than the old conflated "no_matching_tenant_or_not_paid"
    — the actual outcome (billing_status untouched) is unchanged."""
    headers, tenant_id = _signup(client_webhook)
    client_webhook.post(f"/api/tenant/billing/checkout?tenant_id={tenant_id}", headers=headers)

    r = _signed_webhook_post(client_webhook, _razorpay_webhook_payload(tenant_id=tenant_id, status="created"))
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "not_actually_paid"

    tenant = client_webhook.get(f"/api/tenant?tenant_id={tenant_id}", headers=headers).json()
    assert tenant["billing_status"] == "invoiced"


def test_admin_billing_link_now_carries_reference_id(client_billing, services_billing):
    """Retrofit check: an admin-sent billing link is now ALSO correlatable
    by the webhook, not just self-serve checkout links."""
    headers, tenant_id = _signup(client_billing)
    admin_headers = _admin_headers(client_billing, services_billing.settings)
    client_billing.post(f"/api/v1/admin/tenants/{tenant_id}/billing-link", json={"amount_inr": 999}, headers=admin_headers)

    call = services_billing.fake_razorpay_client.calls[0]
    assert call["reference_id"] == tenant_id
