"""Tests for the WhatsApp Business Cloud API channel: webhook
verification, signature enforcement, tenant routing/isolation, the
shared RAG/lead/dissatisfaction pipeline reused via _process_question,
redelivery idempotency, and the WhatsApp follow-up (review request) path.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json

import pytest

from business_ai.app import Services, create_app
from business_ai.retrieval import HashEmbeddingProvider
from tests.conftest import FakeGenerator

APP_SECRET = "test-whatsapp-app-secret"
VERIFY_TOKEN = "test-verify-token"


class FakeWhatsAppClient:
    """Captures every send instead of hitting the real Graph API —
    mirrors FakeEmailSender's role in tests/conftest.py."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.raise_reengagement = False

    def send_text(self, *, phone_number_id: str, access_token: str, to: str, body: str) -> dict:
        if self.raise_reengagement:
            from business_ai.whatsapp import REENGAGEMENT_WINDOW_CLOSED_CODE, WhatsAppSendError

            raise WhatsAppSendError("window closed", error_code=REENGAGEMENT_WINDOW_CLOSED_CODE)
        self.sent.append({"phone_number_id": phone_number_id, "access_token": access_token, "to": to, "body": body})
        return {"messages": [{"id": "wamid.fake"}]}

    def mark_read(self, **kwargs) -> None:
        pass


@pytest.fixture()
def services_wa(tmp_path, settings):
    settings_wa = dataclasses.replace(settings, whatsapp_app_secret=APP_SECRET, whatsapp_verify_token=VERIFY_TOKEN)
    svc = Services(settings_wa, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    fake_wa = FakeWhatsAppClient()
    svc.whatsapp_client = lambda: fake_wa
    svc.fake_whatsapp_client = fake_wa
    return svc


@pytest.fixture()
def client_wa(services_wa):
    from fastapi.testclient import TestClient

    return TestClient(create_app(services_wa))


def _signup(client, business_name="Priya Salon", email="owner@example.com"):
    r = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "secret123", "name": "Priya", "business_name": business_name},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["tenant_id"]


def _activate_with_whatsapp(client, headers, tenant_id, admin_password, *, phone_number_id="PNID_1"):
    r = client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    assert r.status_code == 200, r.text
    r = client.put(
        f"/api/tenant?tenant_id={tenant_id}",
        json={"whatsapp_phone_number_id": phone_number_id, "whatsapp_access_token": "fake-permanent-token"},
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
    signature = "sha256=" + hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/api/whatsapp/webhook", content=raw, headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"}
    )


# ------------------------------------------------------------------ webhook verification


def test_webhook_verification_succeeds_with_correct_token(client_wa):
    r = client_wa.get(
        "/api/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "challenge123"},
    )
    assert r.status_code == 200
    assert r.text == "challenge123"


def test_webhook_verification_fails_with_wrong_token(client_wa):
    r = client_wa.get(
        "/api/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "challenge123"},
    )
    assert r.status_code == 403


# ------------------------------------------------------------------ signature enforcement


def test_webhook_rejects_missing_signature(client_wa):
    r = client_wa.post("/api/whatsapp/webhook", content=b"{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 401


def test_webhook_rejects_invalid_signature(client_wa):
    payload = _wa_payload(phone_number_id="PNID_1", wa_id="919876543210", message_id="wamid.1", text="hi")
    r = _signed_post(client_wa, payload, app_secret="wrong-secret")
    assert r.status_code == 401


# ------------------------------------------------------------------ end-to-end reuse of RAG/leads/analytics


def test_webhook_answers_captures_lead_and_replies_via_whatsapp(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")

    payload = _wa_payload(phone_number_id="PNID_1", wa_id="919876543210", message_id="wamid.1", text="what are your hours?")
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200, r.text

    # RAG pipeline reused: a reply was sent back over WhatsApp.
    sent = services_wa.fake_whatsapp_client.sent
    assert len(sent) == 1
    assert sent[0]["to"] == "919876543210"
    assert sent[0]["phone_number_id"] == "PNID_1"
    assert sent[0]["access_token"] == "fake-permanent-token"

    # Lead & intent detection reused: a real lead was captured from the
    # customer's own Meta-verified phone number, with no manual form.
    leads_res = client_wa.get(f"/api/leads?tenant_id={tenant_id}", headers=headers)
    leads = leads_res.json()["leads"]
    assert len(leads) == 1
    assert leads[0]["phone"] == "919876543210"
    assert leads[0]["source"] == "whatsapp"
    assert leads[0]["name"] == "Asha"

    # Owner action engine reused for free: the turn is visible in analytics.
    analytics = client_wa.get(f"/api/analytics?tenant_id={tenant_id}", headers=headers).json()
    assert analytics["total_questions"] == 1


def test_webhook_ignores_unknown_phone_number_id(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")

    payload = _wa_payload(phone_number_id="PNID_NOBODY_OWNS_THIS", wa_id="919876543210", message_id="wamid.1", text="hi")
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200  # never leaks tenant existence to an unmatched sender

    assert services_wa.fake_whatsapp_client.sent == []
    leads = client_wa.get(f"/api/leads?tenant_id={tenant_id}", headers=headers).json()["leads"]
    assert leads == []


def test_webhook_does_not_answer_non_active_tenant(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    # Configure WhatsApp but never activate — tenant stays PROVISIONING.
    client_wa.put(
        f"/api/tenant?tenant_id={tenant_id}",
        json={"whatsapp_phone_number_id": "PNID_1", "whatsapp_access_token": "tok"},
        headers=headers,
    )

    payload = _wa_payload(phone_number_id="PNID_1", wa_id="919876543210", message_id="wamid.1", text="hi")
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200
    assert services_wa.fake_whatsapp_client.sent == []


def test_webhook_deduplicates_redelivered_message(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")

    payload = _wa_payload(phone_number_id="PNID_1", wa_id="919876543210", message_id="wamid.dupe", text="hi there")
    r1 = _signed_post(client_wa, payload)
    r2 = _signed_post(client_wa, payload)  # Meta redelivery of the exact same message id
    assert r1.status_code == 200 and r2.status_code == 200

    assert len(services_wa.fake_whatsapp_client.sent) == 1
    leads = client_wa.get(f"/api/leads?tenant_id={tenant_id}", headers=headers).json()["leads"]
    assert len(leads) == 1


def test_webhook_is_tenant_isolated_by_phone_number_id(client_wa, services_wa):
    headers_a, tenant_a = _signup(client_wa, business_name="Salon A", email="a@example.com")
    headers_b, tenant_b = _signup(client_wa, business_name="Salon B", email="b@example.com")
    _activate_with_whatsapp(client_wa, headers_a, tenant_a, services_wa.settings.admin_secret, phone_number_id="PNID_A")
    _activate_with_whatsapp(client_wa, headers_b, tenant_b, services_wa.settings.admin_secret, phone_number_id="PNID_B")

    payload = _wa_payload(phone_number_id="PNID_A", wa_id="919876543210", message_id="wamid.a1", text="hi")
    _signed_post(client_wa, payload)

    leads_a = client_wa.get(f"/api/leads?tenant_id={tenant_a}", headers=headers_a).json()["leads"]
    leads_b = client_wa.get(f"/api/leads?tenant_id={tenant_b}", headers=headers_b).json()["leads"]
    assert len(leads_a) == 1
    assert leads_b == []  # Salon B's number never received this message


# ------------------------------------------------------------------ dissatisfaction alert reused cross-channel


def test_dissatisfaction_alert_fires_from_whatsapp_channel(client_wa, services_wa, monkeypatch):
    from tests.conftest import FakeEmailSender

    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")

    services_wa.settings = dataclasses.replace(
        services_wa.settings, resend_api_key="re_test", digest_from_email="digest@example.com",
    )
    services_wa.generator = lambda: FakeGenerator(dissatisfied_queries=frozenset({"this is unacceptable, no callback"}))
    fake_sender = FakeEmailSender()
    services_wa.email_sender = lambda: fake_sender

    payload = _wa_payload(
        phone_number_id="PNID_1", wa_id="919876543210", message_id="wamid.angry", text="this is unacceptable, no callback",
    )
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200, r.text

    assert len(fake_sender.sent) == 1
    assert fake_sender.sent[0]["to"] == "owner@example.com"


# ------------------------------------------------------------------ follow-ups reused: WhatsApp review request


def test_request_review_delivers_via_whatsapp_for_whatsapp_lead(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    client_wa.put(f"/api/tenant?tenant_id={tenant_id}", json={"review_link": "https://g.page/r/fake"}, headers=headers)

    payload = _wa_payload(phone_number_id="PNID_1", wa_id="919876543210", message_id="wamid.1", text="thanks, great service")
    _signed_post(client_wa, payload)
    lead_id = client_wa.get(f"/api/leads?tenant_id={tenant_id}", headers=headers).json()["leads"][0]["lead_id"]

    services_wa.fake_whatsapp_client.sent.clear()
    r = client_wa.post(f"/api/leads/{lead_id}/request-review?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"sent_to": "919876543210", "channel": "whatsapp"}
    assert len(services_wa.fake_whatsapp_client.sent) == 1
    assert "g.page/r/fake" in services_wa.fake_whatsapp_client.sent[0]["body"]


def test_request_review_reports_closed_reengagement_window(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    client_wa.put(f"/api/tenant?tenant_id={tenant_id}", json={"review_link": "https://g.page/r/fake"}, headers=headers)

    payload = _wa_payload(phone_number_id="PNID_1", wa_id="919876543210", message_id="wamid.1", text="thanks")
    _signed_post(client_wa, payload)
    lead_id = client_wa.get(f"/api/leads?tenant_id={tenant_id}", headers=headers).json()["leads"][0]["lead_id"]

    services_wa.fake_whatsapp_client.raise_reengagement = True
    r = client_wa.post(f"/api/leads/{lead_id}/request-review?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert "24-hour" in r.json()["detail"]


# ------------------------------------------------------------------ owner "what needs my attention today?" command


def test_owner_message_gets_status_pull_not_customer_rag(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    client_wa.put(f"/api/tenant?tenant_id={tenant_id}", json={"owner_whatsapp_number": "919999888877"}, headers=headers)

    payload = _wa_payload(phone_number_id="PNID_1", wa_id="919999888877", message_id="wamid.owner1", text="hey, what's up")
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200, r.text

    sent = services_wa.fake_whatsapp_client.sent
    assert len(sent) == 1
    assert sent[0]["to"] == "919999888877"
    assert "📊" in sent[0]["body"]
    assert "questions asked" in sent[0]["body"].lower()

    # Not treated as a customer: no lead captured for the owner's own number.
    leads = client_wa.get(f"/api/leads?tenant_id={tenant_id}", headers=headers).json()["leads"]
    assert leads == []


def test_owner_command_ignores_message_content(client_wa, services_wa):
    """No keyword parsing — any message from the owner's number triggers
    the same status pull, since that number is for the owner, not customers."""
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    client_wa.put(f"/api/tenant?tenant_id={tenant_id}", json={"owner_whatsapp_number": "919999888877"}, headers=headers)

    payload = _wa_payload(phone_number_id="PNID_1", wa_id="919999888877", message_id="wamid.owner2", text="asdf random text")
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200, r.text
    assert len(services_wa.fake_whatsapp_client.sent) == 1
    assert "📊" in services_wa.fake_whatsapp_client.sent[0]["body"]


def test_regular_customer_unaffected_when_owner_number_configured(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    client_wa.put(f"/api/tenant?tenant_id={tenant_id}", json={"owner_whatsapp_number": "919999888877"}, headers=headers)

    payload = _wa_payload(phone_number_id="PNID_1", wa_id="919876543210", message_id="wamid.cust1", text="what are your hours?")
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200, r.text

    sent = services_wa.fake_whatsapp_client.sent
    assert len(sent) == 1
    assert sent[0]["to"] == "919876543210"
    assert "📊" not in sent[0]["body"]  # a normal grounded reply, not the owner status pull

    leads = client_wa.get(f"/api/leads?tenant_id={tenant_id}", headers=headers).json()["leads"]
    assert len(leads) == 1
    assert leads[0]["phone"] == "919876543210"


def test_owner_number_matches_regardless_of_plus_or_spacing(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    client_wa.put(f"/api/tenant?tenant_id={tenant_id}", json={"owner_whatsapp_number": "+91 99998 88877"}, headers=headers)

    payload = _wa_payload(phone_number_id="PNID_1", wa_id="919999888877", message_id="wamid.owner3", text="status?")
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200, r.text
    assert len(services_wa.fake_whatsapp_client.sent) == 1
    assert "📊" in services_wa.fake_whatsapp_client.sent[0]["body"]


# ------------------------------------------------------------------ WhatsApp Embedded Signup (one-click onboarding)


class FakeMetaEmbeddedSignupClient:
    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.raise_error = raise_error

    def exchange_code_for_token(self, *, app_id: str, app_secret: str, code: str) -> str:
        if self.raise_error:
            raise self.raise_error
        self.calls.append({"app_id": app_id, "app_secret": app_secret, "code": code})
        return "fake-long-lived-token"


def test_embedded_signup_status_unavailable_without_app_id(client_wa, services_wa):
    r = client_wa.get("/api/tenant/whatsapp/embedded-signup-status")
    assert r.status_code == 200, r.text
    assert r.json() == {"available": False}  # services_wa never sets whatsapp_app_id


def test_embedded_signup_status_available_when_configured(client_wa, services_wa):
    services_wa.settings = dataclasses.replace(services_wa.settings, whatsapp_app_id="fake-app-id")
    r = client_wa.get("/api/tenant/whatsapp/embedded-signup-status")
    assert r.json() == {"available": True}


def test_embedded_signup_rejected_when_not_configured(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    r = client_wa.post(
        f"/api/tenant/whatsapp/embedded-signup?tenant_id={tenant_id}",
        json={"code": "authcode123", "phone_number_id": "PNID_NEW"}, headers=headers,
    )
    assert r.status_code == 400
    assert "manual connection" in r.json()["detail"].lower()


def test_embedded_signup_connects_tenant_on_success(client_wa, services_wa):
    services_wa.settings = dataclasses.replace(services_wa.settings, whatsapp_app_id="fake-app-id")
    fake_signup = FakeMetaEmbeddedSignupClient()
    services_wa.meta_signup_client = lambda: fake_signup

    headers, tenant_id = _signup(client_wa)
    r = client_wa.post(
        f"/api/tenant/whatsapp/embedded-signup?tenant_id={tenant_id}",
        json={"code": "authcode123", "phone_number_id": "PNID_NEW"}, headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"whatsapp_phone_number_id": "PNID_NEW", "connected": True}
    assert len(fake_signup.calls) == 1
    assert fake_signup.calls[0]["code"] == "authcode123"

    tenant = client_wa.get(f"/api/tenant?tenant_id={tenant_id}", headers=headers).json()
    assert tenant["whatsapp_phone_number_id"] == "PNID_NEW"
    assert tenant["whatsapp_access_token"] == "fake-long-lived-token"


def test_embedded_signup_surfaces_meta_error(client_wa, services_wa):
    from business_ai.whatsapp import MetaEmbeddedSignupError

    services_wa.settings = dataclasses.replace(services_wa.settings, whatsapp_app_id="fake-app-id")
    services_wa.meta_signup_client = lambda: FakeMetaEmbeddedSignupClient(
        raise_error=MetaEmbeddedSignupError("Meta token exchange failed (400): invalid code")
    )

    headers, tenant_id = _signup(client_wa)
    r = client_wa.post(
        f"/api/tenant/whatsapp/embedded-signup?tenant_id={tenant_id}",
        json={"code": "bad-code", "phone_number_id": "PNID_NEW"}, headers=headers,
    )
    assert r.status_code == 502
    assert "invalid code" in r.json()["detail"]


def test_embedded_signup_requires_authentication(client_wa, services_wa):
    """Matches this codebase's consistent convention (see authorize()):
    a missing/absent token on a tenant-scoped action is a 403, not a 401
    — the same as every other MANAGE_ASSISTANT-gated endpoint."""
    services_wa.settings = dataclasses.replace(services_wa.settings, whatsapp_app_id="fake-app-id")
    headers, tenant_id = _signup(client_wa)
    r = client_wa.post(
        f"/api/tenant/whatsapp/embedded-signup?tenant_id={tenant_id}",
        json={"code": "authcode123", "phone_number_id": "PNID_NEW"},
    )
    assert r.status_code == 403


def test_embedded_signup_is_tenant_isolated(client_wa, services_wa):
    services_wa.settings = dataclasses.replace(services_wa.settings, whatsapp_app_id="fake-app-id")
    services_wa.meta_signup_client = lambda: FakeMetaEmbeddedSignupClient()

    headers_a, tenant_a = _signup(client_wa, business_name="Salon A", email="a@example.com")
    headers_b, tenant_b = _signup(client_wa, business_name="Salon B", email="b@example.com")

    r = client_wa.post(
        f"/api/tenant/whatsapp/embedded-signup?tenant_id={tenant_a}",
        json={"code": "authcode123", "phone_number_id": "PNID_NEW"}, headers=headers_b,
    )
    assert r.status_code == 403
