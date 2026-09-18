"""Tests for Phase 2's Razorpay Subscriptions infrastructure:
RazorpayClient.create_plan/create_subscription (payments.py), and the
platform webhook's new subscription.* event handling (webhook_routes.py)
— additive to the existing payment_link.* path, which stays untouched."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json

import pytest

from business_ai.app import Services, create_app
from business_ai.payments import RazorpayClient, SubscriptionError


# ------------------------------------------------------------------ RazorpayClient


def _fake_response(body: dict):
    class _Resp:
        def read(self):
            return json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _Resp()


def test_create_plan_returns_plan_id(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=15):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["url"] = request.full_url
        return _fake_response({"id": "plan_abc123"})

    monkeypatch.setattr("business_ai.payments.urlopen", fake_urlopen)

    client = RazorpayClient()
    plan_id = client.create_plan(key_id="rzp_test", key_secret="secret", plan_tier="growth", amount_inr=5000)

    assert plan_id == "plan_abc123"
    assert captured["url"] == "https://api.razorpay.com/v1/plans"
    assert captured["payload"]["item"]["amount"] == 500000
    assert captured["payload"]["item"]["currency"] == "INR"


def test_create_plan_rejects_missing_credentials():
    client = RazorpayClient()
    with pytest.raises(SubscriptionError):
        client.create_plan(key_id="", key_secret="", plan_tier="starter", amount_inr=2000)


def test_create_plan_rejects_non_positive_amount():
    client = RazorpayClient()
    with pytest.raises(SubscriptionError):
        client.create_plan(key_id="rzp_test", key_secret="secret", plan_tier="starter", amount_inr=0)


def test_create_subscription_returns_id_and_url(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=15):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _fake_response({"id": "sub_xyz789", "short_url": "https://rzp.io/i/abc123", "status": "created"})

    monkeypatch.setattr("business_ai.payments.urlopen", fake_urlopen)

    client = RazorpayClient()
    result = client.create_subscription(
        key_id="rzp_test", key_secret="secret", plan_id="plan_abc123", total_count=120, reference_id="tenant-1",
    )

    assert result["id"] == "sub_xyz789"
    assert result["short_url"] == "https://rzp.io/i/abc123"
    assert captured["payload"]["notes"]["reference_id"] == "tenant-1"


def test_create_subscription_rejects_missing_plan_id():
    client = RazorpayClient()
    with pytest.raises(SubscriptionError):
        client.create_subscription(key_id="rzp_test", key_secret="secret", plan_id="", total_count=120)


def test_create_plan_surfaces_razorpay_error(monkeypatch):
    from urllib.error import HTTPError

    def fake_urlopen(request, timeout=15):
        raise HTTPError(
            url="https://api.razorpay.com/v1/plans", code=400, msg="Bad Request", hdrs=None,
            fp=__import__("io").BytesIO(b'{"error":{"description":"invalid period"}}'),
        )

    monkeypatch.setattr("business_ai.payments.urlopen", fake_urlopen)

    client = RazorpayClient()
    with pytest.raises(SubscriptionError, match="invalid period"):
        client.create_plan(key_id="rzp_test", key_secret="secret", plan_tier="starter", amount_inr=2000)


# ------------------------------------------------------------------ webhook: subscription.* events


WEBHOOK_SECRET = "test-subscription-webhook-secret"


def _subscription_webhook_payload(*, event: str, tenant_id: str, status: str, current_end: int = 1740000000):
    return {
        "entity": "event",
        "event": event,
        "contains": ["subscription"],
        "payload": {
            "subscription": {
                "entity": {
                    "id": "sub_test123",
                    "status": status,
                    "current_end": current_end,
                    "notes": {"reference_id": tenant_id},
                }
            }
        },
        "created_at": 1234567890,
    }


def _signed_post(client, payload: dict, *, secret: str = WEBHOOK_SECRET):
    raw_body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return client.post(
        "/api/webhooks/razorpay", content=raw_body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )


@pytest.fixture()
def services_sub(services):
    services.settings = dataclasses.replace(services.settings, platform_razorpay_webhook_secret=WEBHOOK_SECRET)
    return services


@pytest.fixture()
def client_sub(services_sub):
    from fastapi.testclient import TestClient

    return TestClient(create_app(services_sub))


def test_subscription_activated_sets_billing_paid(client_sub, services_sub, owner_session):
    headers, tenant_id = owner_session
    assert services_sub.tenant_registry.get_config(tenant_id).billing_status == "unbilled"

    r = _signed_post(client_sub, _subscription_webhook_payload(event="subscription.activated", tenant_id=tenant_id, status="active"))
    assert r.status_code == 200, r.text
    assert r.json()["subscription_status"] == "active"

    tenant = services_sub.tenant_registry.get_config(tenant_id)
    assert tenant.billing_status == "paid"
    assert tenant.platform_subscription_id == "sub_test123"
    assert tenant.platform_subscription_status == "active"
    assert tenant.platform_subscription_current_period_end is not None


def test_subscription_charged_keeps_billing_paid(client_sub, services_sub, owner_session):
    headers, tenant_id = owner_session
    r = _signed_post(client_sub, _subscription_webhook_payload(event="subscription.charged", tenant_id=tenant_id, status="active"))
    assert r.status_code == 200, r.text
    assert services_sub.tenant_registry.get_config(tenant_id).billing_status == "paid"


def test_subscription_halted_does_not_auto_revoke_paid_status(client_sub, services_sub, owner_session):
    """Deliberate: halted/cancelled/paused record the real status but do
    NOT flip billing_status away from "paid" here — that's a grace-period
    product decision not built yet, not something a webhook wiring pass
    should silently decide."""
    headers, tenant_id = owner_session
    _signed_post(client_sub, _subscription_webhook_payload(event="subscription.activated", tenant_id=tenant_id, status="active"))
    assert services_sub.tenant_registry.get_config(tenant_id).billing_status == "paid"

    r = _signed_post(client_sub, _subscription_webhook_payload(event="subscription.halted", tenant_id=tenant_id, status="halted"))
    assert r.status_code == 200, r.text
    tenant = services_sub.tenant_registry.get_config(tenant_id)
    assert tenant.billing_status == "paid"  # unchanged
    assert tenant.platform_subscription_status == "halted"  # but the real status is recorded


def test_subscription_event_writes_audit_log(client_sub, services_sub, owner_session):
    headers, tenant_id = owner_session
    _signed_post(client_sub, _subscription_webhook_payload(event="subscription.activated", tenant_id=tenant_id, status="active"))
    audit = services_sub.audit_log.list_for_tenant(tenant_id, action="platform_subscription_event")
    assert len(audit) == 1
    assert audit[0].metadata["event"] == "subscription.activated"


def test_subscription_webhook_ignored_for_unknown_tenant(client_sub, services_sub):
    r = _signed_post(client_sub, _subscription_webhook_payload(event="subscription.activated", tenant_id="does-not-exist", status="active"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ignored"


def test_subscription_webhook_rejects_invalid_signature(client_sub, owner_session):
    headers, tenant_id = owner_session
    payload = _subscription_webhook_payload(event="subscription.activated", tenant_id=tenant_id, status="active")
    r = _signed_post(client_sub, payload, secret="wrong-secret")
    assert r.status_code == 401


def test_payment_link_events_still_work_unaffected(client_sub, services_sub, owner_session):
    """The pre-existing payment_link.* path must be completely unaffected
    by the new subscription.* branch."""
    headers, tenant_id = owner_session
    services_sub.settings = dataclasses.replace(services_sub.settings, platform_subscription_price_inr=999)
    payload = {
        "entity": "event", "event": "payment_link.paid", "contains": ["payment_link"],
        "payload": {"payment_link": {"entity": {"id": "plink_1", "reference_id": tenant_id, "amount": 99900, "amount_paid": 99900, "status": "paid"}}},
        "created_at": 1234567890,
    }
    r = _signed_post(client_sub, payload)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert services_sub.tenant_registry.get_config(tenant_id).billing_status == "paid"
