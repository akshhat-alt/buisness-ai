"""Tests for new-lead email alerts to owners (Phase: New-Lead Alert to Owner)."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from business_ai.app import Services, create_app
from business_ai.email_sender import EmailSendError
from business_ai.rate_limiting import FixedWindowRateLimiter
from business_ai.retrieval import HashEmbeddingProvider
from tests.conftest import FakeEmailSender, FakeGenerator

APP_SECRET = "test-meta-app-secret-32-chars-ok"
VERIFY_TOKEN = "test-verify-token"


class FakeWhatsAppClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_text(self, *, phone_number_id: str, access_token: str, to: str, body: str) -> dict:
        self.sent.append({"phone_number_id": phone_number_id, "access_token": access_token, "to": to, "body": body})
        return {"messages": [{"id": f"wamid.{len(self.sent)}"}]}

    def mark_read(self, **kwargs) -> None:
        pass


@pytest.fixture()
def services_lead_alert(tmp_path, settings):
    settings_with_email = dataclasses.replace(
        settings,
        resend_api_key="re_test_key",
        digest_from_email="digest@business-ai.example",
        public_base_url="https://app.example.com",
        whatsapp_app_secret=APP_SECRET,
        whatsapp_verify_token=VERIFY_TOKEN,
    )
    svc = Services(settings_with_email, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    fake_sender = FakeEmailSender()
    svc.email_sender = lambda: fake_sender
    svc.fake_email_sender = fake_sender
    fake_wa = FakeWhatsAppClient()
    svc.whatsapp_client = lambda: fake_wa
    svc.fake_whatsapp_client = fake_wa
    yield svc
    svc.vector_store.close()


@pytest.fixture()
def client_lead_alert(services_lead_alert):
    return TestClient(create_app(services_lead_alert))


def _signup(client, business_name="Priya Salon", email="owner@example.com"):
    r = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "secret123", "name": "Priya", "business_name": business_name},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["tenant_id"]


def _activate_tenant(client, headers, tenant_id, admin_password, *, phone_number_id="PNID_LEAD"):
    r = client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    assert r.status_code == 200, r.text
    r_txt = client.post(
        f"/api/knowledge/text?tenant_id={tenant_id}",
        json={"title": "Services", "text": "Our signature drink is Indore Masala Chai brewed fresh with cardamom, ginger, and saffron."},
        headers=headers,
    )
    assert r_txt.status_code == 200, r_txt.text
    r = client.put(
        f"/api/tenant?tenant_id={tenant_id}",
        json={"whatsapp_phone_number_id": phone_number_id, "whatsapp_access_token": "fake-token"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    admin_login = client.post("/api/auth/login", json={"email": "admin", "password": admin_password})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
    assert r.status_code == 200, r.text
    return admin_headers


def _wa_payload(*, phone_number_id: str, wa_id: str, message_id: str, text: str, contact_name: str = "Asha") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_TEST",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "15550001111", "phone_number_id": phone_number_id},
                            "contacts": [{"profile": {"name": contact_name}, "wa_id": wa_id}],
                            "messages": [
                                {"from": wa_id, "id": message_id, "timestamp": "1700000000", "type": "text", "text": {"body": text}}
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _signed_post(client, payload: dict, *, app_secret: str = APP_SECRET):
    raw = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/api/whatsapp/webhook", content=raw, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"}
    )


# ------------------------------------------------------------------ Tests


def test_widget_lead_sends_alert_with_escaped_fields(client_lead_alert, services_lead_alert):
    headers, tenant_id = _signup(client_lead_alert, email="owner1@example.com")
    _activate_tenant(client_lead_alert, headers, tenant_id, services_lead_alert.settings.admin_secret)
    services_lead_alert.fake_email_sender.sent.clear()

    res = client_lead_alert.post(
        f"/api/leads?tenant_id={tenant_id}",
        json={
            "session_id": "sess_widget_1",
            "name": "Bob Smith",
            "phone": "9876543210",
            "email": "bob@example.com",
            "message": "Do you offer bridal makeup packages?",
        },
    )
    assert res.status_code == 200, res.text
    assert "lead_id" in res.json()

    sent = services_lead_alert.fake_email_sender.sent
    assert len(sent) == 1
    email = sent[0]
    assert email["to"] == "owner1@example.com"
    assert email["subject"] == "New customer lead — Bob Smith"
    assert "Bob Smith" in email["html_body"]
    assert "9876543210" in email["html_body"]
    assert "bob@example.com" in email["html_body"]
    assert "Website chat" in email["html_body"]
    assert "Do you offer bridal makeup packages?" in email["html_body"]
    assert "https://app.example.com/dashboard#leads" in email["html_body"]


def test_repeat_widget_lead_same_session_no_second_alert(client_lead_alert, services_lead_alert):
    headers, tenant_id = _signup(client_lead_alert, email="owner2@example.com")
    _activate_tenant(client_lead_alert, headers, tenant_id, services_lead_alert.settings.admin_secret)
    services_lead_alert.fake_email_sender.sent.clear()

    # First submit from session
    r1 = client_lead_alert.post(
        f"/api/leads?tenant_id={tenant_id}",
        json={"session_id": "sess_repeat", "phone": "9876543210", "name": "First Submit"},
    )
    assert r1.status_code == 200
    assert len(services_lead_alert.fake_email_sender.sent) == 1

    # Second submit from same session
    r2 = client_lead_alert.post(
        f"/api/leads?tenant_id={tenant_id}",
        json={"session_id": "sess_repeat", "phone": "9876543210", "name": "Second Submit"},
    )
    assert r2.status_code == 200
    # Alert count must remain exactly 1
    assert len(services_lead_alert.fake_email_sender.sent) == 1


def test_whatsapp_first_contact_sends_alert(client_lead_alert, services_lead_alert):
    headers, tenant_id = _signup(client_lead_alert, email="owner3@example.com")
    _activate_tenant(client_lead_alert, headers, tenant_id, services_lead_alert.settings.admin_secret, phone_number_id="PNID_WA1")
    services_lead_alert.fake_email_sender.sent.clear()
    services_lead_alert.fake_whatsapp_client.sent.clear()

    payload = _wa_payload(
        phone_number_id="PNID_WA1",
        wa_id="919876500001",
        message_id="wamid.test.1",
        text="Hi, what time do you open?",
        contact_name="Asha Sharma",
    )
    res = _signed_post(client_lead_alert, payload)
    assert res.status_code == 200

    # WhatsApp reply sent to customer
    assert len(services_lead_alert.fake_whatsapp_client.sent) == 1
    assert services_lead_alert.fake_whatsapp_client.sent[0]["to"] == "919876500001"

    # Exactly 1 lead alert email sent to owner
    sent = services_lead_alert.fake_email_sender.sent
    assert len(sent) == 1
    assert sent[0]["to"] == "owner3@example.com"
    assert sent[0]["subject"] == "New customer lead — Asha Sharma"
    assert "Asha Sharma" in sent[0]["html_body"]
    assert "919876500001" in sent[0]["html_body"]
    assert "WhatsApp" in sent[0]["html_body"]


def test_whatsapp_buying_intent_flag(client_lead_alert, services_lead_alert):
    """Amendment 3: exercises real _process_question with FakeGenerator(shows_buying_intent=True)
    and verifies presence, then verifies absence when shows_buying_intent=False."""
    headers, tenant_id = _signup(client_lead_alert, email="owner4@example.com")
    _activate_tenant(client_lead_alert, headers, tenant_id, services_lead_alert.settings.admin_secret, phone_number_id="PNID_WA2")
    services_lead_alert.fake_email_sender.sent.clear()

    # Generator returns buying intent True
    services_lead_alert.generator = lambda: FakeGenerator(shows_buying_intent=True)

    payload = _wa_payload(
        phone_number_id="PNID_WA2",
        wa_id="919876500002",
        message_id="wamid.intent.1",
        text="Indore Masala Chai brewed fresh",
        contact_name="Rani",
    )
    res = _signed_post(client_lead_alert, payload)
    assert res.status_code == 200

    sent = services_lead_alert.fake_email_sender.sent
    assert len(sent) == 1
    assert "Showing buying interest" in sent[0]["html_body"]

    # Now test another contact where generator returns shows_buying_intent=False
    services_lead_alert.generator = lambda: FakeGenerator(shows_buying_intent=False)
    services_lead_alert.fake_email_sender.sent.clear()

    payload2 = _wa_payload(
        phone_number_id="PNID_WA2",
        wa_id="919876500003",
        message_id="wamid.intent.2",
        text="Where is your shop located?",
        contact_name="Meena",
    )
    res2 = _signed_post(client_lead_alert, payload2)
    assert res2.status_code == 200

    sent2 = services_lead_alert.fake_email_sender.sent
    assert len(sent2) == 1
    assert "Showing buying interest" not in sent2[0]["html_body"]


def test_whatsapp_subsequent_message_same_number_no_alert(client_lead_alert, services_lead_alert):
    headers, tenant_id = _signup(client_lead_alert, email="owner5@example.com")
    _activate_tenant(client_lead_alert, headers, tenant_id, services_lead_alert.settings.admin_secret, phone_number_id="PNID_WA3")
    services_lead_alert.fake_email_sender.sent.clear()

    # 1st message
    payload1 = _wa_payload(
        phone_number_id="PNID_WA3",
        wa_id="919876500004",
        message_id="wamid.subseq.1",
        text="Hello",
        contact_name="Kavita",
    )
    _signed_post(client_lead_alert, payload1)
    assert len(services_lead_alert.fake_email_sender.sent) == 1

    services_lead_alert.fake_email_sender.sent.clear()

    # 2nd message from same number
    payload2 = _wa_payload(
        phone_number_id="PNID_WA3",
        wa_id="919876500004",
        message_id="wamid.subseq.2",
        text="Are you open on Sundays?",
        contact_name="Kavita",
    )
    _signed_post(client_lead_alert, payload2)
    # Must NOT send another email
    assert len(services_lead_alert.fake_email_sender.sent) == 0


def test_owner_reservation_does_not_alert(client_lead_alert, services_lead_alert):
    headers, tenant_id = _signup(client_lead_alert, email="owner6@example.com")
    _activate_tenant(client_lead_alert, headers, tenant_id, services_lead_alert.settings.admin_secret, phone_number_id="PNID_WA4")

    # Add owner employee roster record
    r_emp = client_lead_alert.post(
        f"/api/employees?tenant_id={tenant_id}",
        json={"name": "Owner Priya", "whatsapp_number": "919000000001", "role": "owner"},
        headers=headers,
    )
    assert r_emp.status_code == 200
    services_lead_alert.fake_email_sender.sent.clear()

    # Owner uses WhatsApp admin command to create a reservation
    payload = _wa_payload(
        phone_number_id="PNID_WA4",
        wa_id="919000000001",
        message_id="wamid.res.1",
        text="reserve Rahul 9876543210 for 2 on 2026-09-25 19:00",
        contact_name="Owner Priya",
    )
    res = _signed_post(client_lead_alert, payload)
    assert res.status_code == 200

    # Reservation lead was created in LeadStore
    leads = services_lead_alert.lead_store.list_for_tenant(tenant_id)
    assert any(l.source == "reservation" for l in leads)

    # But zero lead alert emails were sent!
    assert len(services_lead_alert.fake_email_sender.sent) == 0


def test_notify_new_leads_disabled(client_lead_alert, services_lead_alert):
    headers, tenant_id = _signup(client_lead_alert, email="owner7@example.com")
    _activate_tenant(client_lead_alert, headers, tenant_id, services_lead_alert.settings.admin_secret)

    # Disable lead notifications in tenant settings
    r_put = client_lead_alert.put(
        f"/api/tenant?tenant_id={tenant_id}",
        json={"notify_new_leads": False},
        headers=headers,
    )
    assert r_put.status_code == 200
    assert r_put.json()["notify_new_leads"] is False

    services_lead_alert.fake_email_sender.sent.clear()

    # Submit lead
    r_lead = client_lead_alert.post(
        f"/api/leads?tenant_id={tenant_id}",
        json={"session_id": "sess_disabled", "phone": "9876543210", "name": "Quiet Customer"},
    )
    assert r_lead.status_code == 200
    # Lead is saved
    assert services_lead_alert.lead_store.exists_for_session(tenant_id, "sess_disabled")
    # But no email was sent
    assert len(services_lead_alert.fake_email_sender.sent) == 0


def test_rate_limit_unit_check_lead_alert_action():
    """Amendment 4: unit test on FixedWindowRateLimiter.check_lead_alert_action with injected now."""
    limiter = FixedWindowRateLimiter(limit=10, window_seconds=3600.0)
    t0 = 1000.0

    # 10 individual
    for i in range(1, 11):
        assert limiter.check_lead_alert_action("tenant_1", now=t0) == "individual"

    # 11th triggers summary
    assert limiter.check_lead_alert_action("tenant_1", now=t0) == "summary"

    # 12th and subsequent suppressed
    assert limiter.check_lead_alert_action("tenant_1", now=t0) == "suppress"
    assert limiter.check_lead_alert_action("tenant_1", now=t0 + 100) == "suppress"

    # Rollover after 3600 seconds
    assert limiter.check_lead_alert_action("tenant_1", now=t0 + 3601.0) == "individual"


def test_rate_limit_tenth_and_eleventh_lead_summary(client_lead_alert, services_lead_alert):
    headers, tenant_id = _signup(client_lead_alert, email="owner8@example.com")
    _activate_tenant(client_lead_alert, headers, tenant_id, services_lead_alert.settings.admin_secret)
    services_lead_alert.fake_email_sender.sent.clear()

    # Send 10 leads -> 10 individual emails
    for i in range(10):
        r = client_lead_alert.post(
            f"/api/leads?tenant_id={tenant_id}",
            json={"session_id": f"sess_rate_{i}", "phone": f"987654321{i}", "name": f"Lead {i}"},
        )
        assert r.status_code == 200

    assert len(services_lead_alert.fake_email_sender.sent) == 10
    for i in range(10):
        assert services_lead_alert.fake_email_sender.sent[i]["subject"] == f"New customer lead — Lead {i}"

    # 11th lead -> exactly 1 summary email
    r11 = client_lead_alert.post(
        f"/api/leads?tenant_id={tenant_id}",
        json={"session_id": "sess_rate_11", "phone": "9876543299", "name": "Lead 11"},
    )
    assert r11.status_code == 200
    assert len(services_lead_alert.fake_email_sender.sent) == 11
    assert services_lead_alert.fake_email_sender.sent[10]["subject"] == "10+ new customer leads this hour — open your dashboard"

    # 12th lead -> suppressed, no new email
    r12 = client_lead_alert.post(
        f"/api/leads?tenant_id={tenant_id}",
        json={"session_id": "sess_rate_12", "phone": "9876543288", "name": "Lead 12"},
    )
    assert r12.status_code == 200
    assert len(services_lead_alert.fake_email_sender.sent) == 11

    # All 12 leads are saved in LeadStore
    assert services_lead_alert.lead_store.count_for_tenant(tenant_id) == 12


def test_email_failure_isolated(client_lead_alert, services_lead_alert):
    headers, tenant_id = _signup(client_lead_alert, email="owner9@example.com")
    _activate_tenant(client_lead_alert, headers, tenant_id, services_lead_alert.settings.admin_secret)

    class FailingSender:
        def send(self, **kwargs):
            raise EmailSendError("Simulated email provider network failure")

    services_lead_alert.email_sender = lambda: FailingSender()

    res = client_lead_alert.post(
        f"/api/leads?tenant_id={tenant_id}",
        json={"session_id": "sess_fail", "phone": "9876543210", "name": "Customer 1"},
    )
    # HTTP status code must still be 200
    assert res.status_code == 200
    # Lead must still be saved
    assert services_lead_alert.lead_store.exists_for_session(tenant_id, "sess_fail")


def test_missing_resend_api_key_no_crash(tmp_path, settings):
    settings_no_email = dataclasses.replace(
        settings,
        resend_api_key=None,
        digest_from_email=None,
    )
    svc = Services(settings_no_email, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    client = TestClient(create_app(svc))

    headers, tenant_id = _signup(client, email="owner10@example.com")
    _activate_tenant(client, headers, tenant_id, svc.settings.admin_secret)

    res = client.post(
        f"/api/leads?tenant_id={tenant_id}",
        json={"session_id": "sess_no_email", "phone": "9876543210", "name": "Customer"},
    )
    assert res.status_code == 200
    assert svc.lead_store.exists_for_session(tenant_id, "sess_no_email")
    svc.vector_store.close()


def test_provisioning_inactive_tenant_404(client_lead_alert, services_lead_alert):
    headers, tenant_id = _signup(client_lead_alert, email="owner11@example.com")
    # Not activated!
    res = client_lead_alert.post(
        f"/api/leads?tenant_id={tenant_id}",
        json={"session_id": "sess_unauth", "phone": "9876543210"},
    )
    assert res.status_code == 404


def test_html_injection_escaped(client_lead_alert, services_lead_alert):
    headers, tenant_id = _signup(client_lead_alert, email="owner12@example.com")
    _activate_tenant(client_lead_alert, headers, tenant_id, services_lead_alert.settings.admin_secret)
    services_lead_alert.fake_email_sender.sent.clear()

    payload = {
        "session_id": "sess_xss",
        "name": "<script>alert('xss')</script>",
        "phone": "+91 98765 43210",
        "email": "hacker@example.com",
        "message": "<img src=x onerror=alert(1)> Injected message",
    }
    res = client_lead_alert.post(f"/api/leads?tenant_id={tenant_id}", json=payload)
    assert res.status_code == 200

    sent = services_lead_alert.fake_email_sender.sent
    assert len(sent) == 1
    html = sent[0]["html_body"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&#x27;xss&#x27;" in html
    assert "<img src" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_tenant_isolation(client_lead_alert, services_lead_alert):
    headers_a, tenant_a = _signup(client_lead_alert, business_name="Salon A", email="owner_a@example.com")
    _activate_tenant(client_lead_alert, headers_a, tenant_a, services_lead_alert.settings.admin_secret)

    headers_b, tenant_b = _signup(client_lead_alert, business_name="Salon B", email="owner_b@example.com")
    _activate_tenant(client_lead_alert, headers_b, tenant_b, services_lead_alert.settings.admin_secret)

    services_lead_alert.fake_email_sender.sent.clear()

    # Lead for Tenant A
    client_lead_alert.post(
        f"/api/leads?tenant_id={tenant_a}",
        json={"session_id": "sess_tenant_a", "phone": "9876543210", "name": "Customer A"},
    )

    sent = services_lead_alert.fake_email_sender.sent
    assert len(sent) == 1
    assert sent[0]["to"] == "owner_a@example.com"
    assert "Salon A" in sent[0]["html_body"]
    assert "owner_b@example.com" not in [e["to"] for e in sent]
