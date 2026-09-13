"""Tests for Phase 18's per-tenant Razorpay webhook
(POST /api/webhooks/razorpay/{tenant_id}) — auto-confirms a tenant's own
deposit-link payments by correlating reference_id back to a real Lead,
verified against THAT TENANT's own razorpay_webhook_secret (never the
platform's).
"""

from __future__ import annotations

import hashlib
import hmac
import json

TENANT_WEBHOOK_SECRET = "tenant-own-webhook-secret"


def _payload(*, lead_id: str, event: str = "payment_link.paid", status: str = "paid", amount_paise: int = 50000) -> dict:
    return {
        "entity": "event", "event": event,
        "payload": {"payment_link": {"entity": {
            "id": "plink_test1", "reference_id": lead_id, "amount": amount_paise, "amount_paid": amount_paise, "status": status,
        }}},
    }


def _signed_post(client, tenant_id, payload, *, secret=TENANT_WEBHOOK_SECRET):
    raw_body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return client.post(
        f"/api/webhooks/razorpay/{tenant_id}", content=raw_body,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )


def _configure_webhook_secret(client, headers, tenant_id):
    r = client.put(f"/api/tenant?tenant_id={tenant_id}", json={"razorpay_webhook_secret": TENANT_WEBHOOK_SECRET}, headers=headers)
    assert r.status_code == 200, r.text


def test_webhook_rejects_missing_signature(client, owner_session):
    headers, tenant_id = owner_session
    _configure_webhook_secret(client, headers, tenant_id)
    r = client.post(f"/api/webhooks/razorpay/{tenant_id}", content=b"{}")
    assert r.status_code == 401


def test_webhook_rejects_wrong_secret(client, owner_session, services):
    headers, tenant_id = owner_session
    _configure_webhook_secret(client, headers, tenant_id)
    lead = services.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210")
    r = _signed_post(client, tenant_id, _payload(lead_id=lead.lead_id), secret="wrong-secret")
    assert r.status_code == 401


def test_webhook_rejects_when_no_secret_configured(client, owner_session):
    headers, tenant_id = owner_session
    r = _signed_post(client, tenant_id, _payload(lead_id="lead_x"))
    assert r.status_code == 401


def test_webhook_unknown_tenant_is_404(client):
    r = _signed_post(client, "no-such-tenant", _payload(lead_id="lead_x"))
    assert r.status_code == 404


def test_webhook_auto_confirms_deposit_paid_for_matching_lead(client, owner_session, services):
    headers, tenant_id = owner_session
    _configure_webhook_secret(client, headers, tenant_id)
    lead = services.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210")
    services.lead_store.mark_deposit_link_sent(tenant_id, lead.lead_id)

    r = _signed_post(client, tenant_id, _payload(lead_id=lead.lead_id, amount_paise=50000))
    assert r.status_code == 200, r.text
    assert r.json()["reason"] == "deposit_confirmed"

    updated_lead = services.lead_store.get(tenant_id, lead.lead_id)
    assert updated_lead.deposit_paid_at is not None
    assert updated_lead.deposit_paid_amount_inr == 500

    audit = services.audit_log.list_for_tenant(tenant_id, action="deposit_payment_confirmed")
    assert len(audit) == 1


def test_webhook_redelivery_is_idempotent(client, owner_session, services):
    headers, tenant_id = owner_session
    _configure_webhook_secret(client, headers, tenant_id)
    lead = services.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210")

    _signed_post(client, tenant_id, _payload(lead_id=lead.lead_id))
    r2 = _signed_post(client, tenant_id, _payload(lead_id=lead.lead_id))
    assert r2.json()["reason"] == "already_paid"
    assert len(services.audit_log.list_for_tenant(tenant_id, action="deposit_payment_confirmed")) == 1


def test_webhook_ignores_unknown_lead(client, owner_session):
    headers, tenant_id = owner_session
    _configure_webhook_secret(client, headers, tenant_id)
    r = _signed_post(client, tenant_id, _payload(lead_id="lead_does_not_exist"))
    assert r.json()["reason"] == "unknown_lead"


def test_webhook_ignores_not_yet_paid_status(client, owner_session, services):
    headers, tenant_id = owner_session
    _configure_webhook_secret(client, headers, tenant_id)
    lead = services.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210")

    r = _signed_post(client, tenant_id, _payload(lead_id=lead.lead_id, status="created"))
    assert r.json()["reason"] == "not_actually_paid"
    assert services.lead_store.get(tenant_id, lead.lead_id).deposit_paid_at is None


def test_webhook_acknowledges_expired_link_without_state_change(client, owner_session, services):
    headers, tenant_id = owner_session
    _configure_webhook_secret(client, headers, tenant_id)
    lead = services.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210")

    r = _signed_post(client, tenant_id, _payload(lead_id=lead.lead_id, event="payment_link.expired"))
    assert r.json()["reason"] == "acknowledged_no_state_change"
    assert services.lead_store.get(tenant_id, lead.lead_id).deposit_paid_at is None
    assert len(services.audit_log.list_for_tenant(tenant_id, action="deposit_payment_link_event")) == 1


def test_webhook_ignores_a_genuinely_unrelated_event(client, owner_session, services):
    headers, tenant_id = owner_session
    _configure_webhook_secret(client, headers, tenant_id)
    lead = services.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210")

    r = _signed_post(client, tenant_id, _payload(lead_id=lead.lead_id, event="payment.captured"))
    assert r.json()["reason"] == "irrelevant_event"


def test_webhook_secret_is_never_returned_in_tenant_export(client, owner_session):
    headers, tenant_id = owner_session
    _configure_webhook_secret(client, headers, tenant_id)
    r = client.get(f"/api/tenant/export?tenant_id={tenant_id}", headers=headers)
    assert "razorpay_webhook_secret" not in r.json()["tenant"]


def test_webhook_secret_is_encrypted_at_rest_when_key_configured(tmp_path, settings):
    """Mirrors the exact style of Phase 9's own secret-encryption tests
    for whatsapp_access_token/razorpay_key_secret, applied to the new
    field."""
    import dataclasses
    from business_ai.app import Services, create_app
    from business_ai.retrieval import HashEmbeddingProvider
    from tests.conftest import FakeGenerator
    from fastapi.testclient import TestClient

    settings_enc = dataclasses.replace(settings, secret_encryption_key="a-real-encryption-key-for-this-test")
    svc = Services(settings_enc, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    client = TestClient(create_app(svc))

    try:
        signup = client.post(
            "/api/auth/signup",
            json={"email": "enc-owner@example.com", "password": "secret123", "name": "Owner", "business_name": "Enc Biz"},
        )
        headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
        tenant_id = signup.json()["tenant_id"]
        client.put(f"/api/tenant?tenant_id={tenant_id}", json={"razorpay_webhook_secret": TENANT_WEBHOOK_SECRET}, headers=headers)

        with svc.tenant_registry._lock, svc.tenant_registry._db() as conn:
            raw_row = conn.execute("SELECT config_json FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
        assert TENANT_WEBHOOK_SECRET not in raw_row["config_json"]
        assert "enc:v1:" in raw_row["config_json"]

        # But an authorized read still gets the real plaintext back.
        r = client.get(f"/api/tenant?tenant_id={tenant_id}", headers=headers)
        assert r.json()["razorpay_webhook_secret"] == TENANT_WEBHOOK_SECRET
    finally:
        svc.vector_store.close()
