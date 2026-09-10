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
    return svc


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
