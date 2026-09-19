"""Tests for POST /api/tenant/whatsapp/help-request — concierge WhatsApp setup assistance."""

import dataclasses
import pytest
from fastapi.testclient import TestClient

from business_ai.auth import Principal, create_access_token
from business_ai.tenant import TenantConfig, TenantStatus
from tests.conftest import FakeEmailSender


@pytest.fixture
def whatsapp_help_setup(services):
    tenant = TenantConfig(
        tenant_id="indore-sweets",
        business_name="Indore Sweets",
        owner_email="owner@indoresweets.example",
        status=TenantStatus.ACTIVE,
    )
    services.tenant_registry.register(tenant)

    other = TenantConfig(
        tenant_id="other-cafe",
        business_name="Other Cafe",
        owner_email="other@cafe.example",
        status=TenantStatus.ACTIVE,
    )
    services.tenant_registry.register(other)

    email_sender = FakeEmailSender()
    services.email_sender = lambda: email_sender
    services.settings = dataclasses.replace(
        services.settings,
        platform_admin_email="admin@bizistic.example",
        resend_api_key="test-resend-key",
        digest_from_email="noreply@send.bizistic.com",
    )

    owner_token = create_access_token(
        Principal.owner("owner@indoresweets.example", tenant.tenant_id),
        services.settings,
    )
    staff_token = create_access_token(
        Principal.staff("staff@indoresweets.example", tenant.tenant_id),
        services.settings,
    )
    other_token = create_access_token(
        Principal.owner("other@cafe.example", other.tenant_id),
        services.settings,
    )

    return {
        "tenant_id": tenant.tenant_id,
        "owner_token": owner_token,
        "staff_token": staff_token,
        "other_token": other_token,
        "email_sender": email_sender,
    }


def test_whatsapp_help_request_success(client, whatsapp_help_setup):
    s = whatsapp_help_setup
    res = client.post(
        f"/api/tenant/whatsapp/help-request?tenant_id={s['tenant_id']}",
        headers={"Authorization": f"Bearer {s['owner_token']}"},
        json={"phone_number": "+91 98260 12345", "note": "Please set this up on my existing WhatsApp Business number."},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "received"
    assert "24 hours" in data["message"]

    # Verify email was dispatched
    assert len(s["email_sender"].sent) == 1
    sent = s["email_sender"].sent[0]
    assert sent["to"] == "admin@bizistic.example"
    assert "Indore Sweets" in sent["subject"]
    assert s["tenant_id"] in sent["html_body"]
    assert "+91 98260 12345" in sent["html_body"]
    assert "existing WhatsApp Business number" in sent["html_body"]


def test_whatsapp_help_request_invalid_phone(client, whatsapp_help_setup):
    s = whatsapp_help_setup
    res = client.post(
        f"/api/tenant/whatsapp/help-request?tenant_id={s['tenant_id']}",
        headers={"Authorization": f"Bearer {s['owner_token']}"},
        json={"phone_number": "123", "note": ""},
    )
    assert res.status_code == 400
    assert "valid phone number" in res.json()["detail"]


def test_whatsapp_help_request_staff_forbidden(client, whatsapp_help_setup):
    s = whatsapp_help_setup
    res = client.post(
        f"/api/tenant/whatsapp/help-request?tenant_id={s['tenant_id']}",
        headers={"Authorization": f"Bearer {s['staff_token']}"},
        json={"phone_number": "+91 98260 12345"},
    )
    assert res.status_code == 403


def test_whatsapp_help_request_cross_tenant_forbidden(client, whatsapp_help_setup):
    s = whatsapp_help_setup
    res = client.post(
        f"/api/tenant/whatsapp/help-request?tenant_id={s['tenant_id']}",
        headers={"Authorization": f"Bearer {s['other_token']}"},
        json={"phone_number": "+91 98260 12345"},
    )
    assert res.status_code == 403


def test_whatsapp_help_request_rate_limit(client, whatsapp_help_setup):
    s = whatsapp_help_setup
    # 3 requests succeed
    for i in range(3):
        res = client.post(
            f"/api/tenant/whatsapp/help-request?tenant_id={s['tenant_id']}",
            headers={"Authorization": f"Bearer {s['owner_token']}"},
            json={"phone_number": f"+91 98260 1234{i}"},
        )
        assert res.status_code == 200

    # 4th request must fail with 429
    res4 = client.post(
        f"/api/tenant/whatsapp/help-request?tenant_id={s['tenant_id']}",
        headers={"Authorization": f"Bearer {s['owner_token']}"},
        json={"phone_number": "+91 98260 12349"},
    )
    assert res4.status_code == 429
    assert "already submitted assistance requests" in res4.json()["detail"]
