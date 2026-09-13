"""Tests for the two instant, event-triggered emails: the real-time
dissatisfaction alert (owner-facing) and the review-request (customer-
facing, owner-triggered)."""

from __future__ import annotations

import dataclasses

import pytest

from business_ai.app import Services, create_app
from business_ai.retrieval import HashEmbeddingProvider
from tests.conftest import FakeEmailSender, FakeGenerator


def _signup(client, business_name="Priya Salon", email="owner@example.com"):
    r = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "secret123", "name": "Priya", "business_name": business_name},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["tenant_id"]


def _activate(client, headers, tenant_id, admin_password):
    """Ingest a trivial knowledge source (required before activation) and
    activate — every ask()/leads call in this file needs an ACTIVE tenant."""
    r = client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    assert r.status_code == 200, r.text
    admin_login = client.post("/api/auth/login", json={"email": "admin", "password": admin_password})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
    assert r.status_code == 200, r.text
    return admin_headers


@pytest.fixture()
def services_dissatisfied(tmp_path, settings):
    """Email configured, and the generator reports dissatisfaction for a
    specific query — for testing the real-time alert without a real
    OpenAI call."""
    settings_with_email = dataclasses.replace(
        settings, resend_api_key="re_test_key", digest_from_email="digest@business-ai.example",
        public_base_url="https://app.example.com",
    )
    svc = Services(settings_with_email, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator(dissatisfied_queries=frozenset({"this is unacceptable, no callback"}))
    fake_sender = FakeEmailSender()
    svc.email_sender = lambda: fake_sender
    svc.fake_email_sender = fake_sender
    yield svc
    svc.vector_store.close()


@pytest.fixture()
def client_dissatisfied(services_dissatisfied):
    from fastapi.testclient import TestClient

    return TestClient(create_app(services_dissatisfied))


def test_dissatisfaction_triggers_instant_owner_alert(client_dissatisfied, services_dissatisfied):
    headers, tenant_id = _signup(client_dissatisfied)
    _activate(client_dissatisfied, headers, tenant_id, services_dissatisfied.settings.admin_secret)
    services_dissatisfied.fake_email_sender.sent.clear()  # discard the activation email — not what this test checks

    # example.com's boilerplate text shares no real vocabulary with this
    # complaint, so it still abstains via the pre-LLM gate rather than
    # ever calling generate() — dissatisfaction must still be detected.
    r = client_dissatisfied.post(
        f"/api/ask?tenant_id={tenant_id}", json={"query": "this is unacceptable, no callback"}, headers=headers
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "insufficient_evidence"
    assert data["shows_dissatisfaction"] is True

    sent = services_dissatisfied.fake_email_sender.sent
    assert len(sent) == 1
    assert sent[0]["to"] == "owner@example.com"
    assert "unhappy" in sent[0]["subject"].lower()
    assert "this is unacceptable, no callback" in sent[0]["html_body"]


def test_non_dissatisfied_query_sends_no_alert(client_dissatisfied, services_dissatisfied):
    headers, tenant_id = _signup(client_dissatisfied, email="owner2@example.com")
    _activate(client_dissatisfied, headers, tenant_id, services_dissatisfied.settings.admin_secret)
    services_dissatisfied.fake_email_sender.sent.clear()  # discard the activation email — not what this test checks

    r = client_dissatisfied.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "what are your hours"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["shows_dissatisfaction"] is False
    assert services_dissatisfied.fake_email_sender.sent == []


def test_dissatisfaction_alert_skipped_when_email_not_configured(client, owner_session, settings, activate_tenant):
    """Uses the plain `client`/`owner_session` fixtures (no email config)
    to prove a missing RESEND_API_KEY doesn't break the customer's actual
    response — it's a best-effort side channel, not a hard dependency."""
    headers, tenant_id = owner_session
    r = client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    assert r.status_code == 200, r.text
    activate_tenant(tenant_id)

    r = client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "anything"}, headers=headers)
    assert r.status_code == 200, r.text  # would fail loudly if the alert path raised instead of no-opping


# ------------------------------------------------------------------ review request


def test_request_review_sends_to_lead_email(client_with_email, services_with_email):
    headers, tenant_id = _signup(client_with_email)
    _activate(client_with_email, headers, tenant_id, services_with_email.settings.admin_secret)
    services_with_email.fake_email_sender.sent.clear()  # discard the activation email — not what this test checks

    r = client_with_email.put(
        f"/api/tenant?tenant_id={tenant_id}", json={"review_link": "https://g.page/r/fake-review-link"}, headers=headers,
    )
    assert r.status_code == 200, r.text

    r = client_with_email.post(
        f"/api/leads?tenant_id={tenant_id}", json={"session_id": "s1", "email": "customer@example.com", "name": "Bob"},
    )
    assert r.status_code == 200, r.text
    lead_id = r.json()["lead_id"]

    r = client_with_email.post(f"/api/leads/{lead_id}/request-review?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["sent_to"] == "customer@example.com"

    sent = services_with_email.fake_email_sender.sent
    assert len(sent) == 1
    assert sent[0]["to"] == "customer@example.com"
    assert "g.page/r/fake-review-link" in sent[0]["html_body"]


def test_request_review_requires_lead_email(client_with_email, services_with_email):
    headers, tenant_id = _signup(client_with_email)
    _activate(client_with_email, headers, tenant_id, services_with_email.settings.admin_secret)
    client_with_email.put(f"/api/tenant?tenant_id={tenant_id}", json={"review_link": "https://example.com/r"}, headers=headers)

    r = client_with_email.post(f"/api/leads?tenant_id={tenant_id}", json={"session_id": "s1", "phone": "9876543210"})
    assert r.status_code == 200, r.text
    lead_id = r.json()["lead_id"]

    r = client_with_email.post(f"/api/leads/{lead_id}/request-review?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert "email address" in r.json()["detail"]


def test_request_review_requires_review_link_configured(client_with_email, services_with_email):
    headers, tenant_id = _signup(client_with_email)
    _activate(client_with_email, headers, tenant_id, services_with_email.settings.admin_secret)

    r = client_with_email.post(f"/api/leads?tenant_id={tenant_id}", json={"session_id": "s1", "email": "customer@example.com"})
    assert r.status_code == 200, r.text
    lead_id = r.json()["lead_id"]

    r = client_with_email.post(f"/api/leads/{lead_id}/request-review?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert "review link" in r.json()["detail"].lower()


def test_request_review_is_tenant_isolated(client_with_email, services_with_email):
    headers_a, tenant_a = _signup(client_with_email, business_name="Salon A", email="a@example.com")
    headers_b, tenant_b = _signup(client_with_email, business_name="Salon B", email="b@example.com")
    _activate(client_with_email, headers_a, tenant_a, services_with_email.settings.admin_secret)
    _activate(client_with_email, headers_b, tenant_b, services_with_email.settings.admin_secret)
    client_with_email.put(f"/api/tenant?tenant_id={tenant_a}", json={"review_link": "https://example.com/r"}, headers=headers_a)

    r = client_with_email.post(f"/api/leads?tenant_id={tenant_a}", json={"session_id": "s1", "email": "customer@example.com"})
    assert r.status_code == 200, r.text
    lead_id = r.json()["lead_id"]

    r = client_with_email.post(f"/api/leads/{lead_id}/request-review?tenant_id={tenant_b}", headers=headers_b)
    assert r.status_code == 404


# ------------------------------------------------------------------ onboarding notifications


def test_admin_notified_on_new_signup(client_with_email, services_with_email):
    services_with_email.settings = dataclasses.replace(services_with_email.settings, platform_admin_email="admin@business-ai.example")

    _signup(client_with_email, business_name="Fresh Salon", email="fresh@example.com")

    sent = services_with_email.fake_email_sender.sent
    assert len(sent) == 1
    assert sent[0]["to"] == "admin@business-ai.example"
    assert "Fresh Salon" in sent[0]["subject"]
    assert "fresh@example.com" in sent[0]["html_body"]


def test_admin_not_notified_when_platform_admin_email_unset(client_with_email, services_with_email):
    # services_with_email never sets platform_admin_email — signup must
    # not crash or send anything unexpected.
    _signup(client_with_email, business_name="Quiet Salon", email="quiet@example.com")
    assert services_with_email.fake_email_sender.sent == []


def test_owner_notified_on_activation(client_with_email, services_with_email):
    headers, tenant_id = _signup(client_with_email, business_name="Glow Salon", email="glow@example.com")
    client_with_email.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    admin_login = client_with_email.post("/api/auth/login", json={"email": "admin", "password": services_with_email.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

    r = client_with_email.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
    assert r.status_code == 200, r.text

    sent = services_with_email.fake_email_sender.sent
    assert len(sent) == 1
    assert sent[0]["to"] == "glow@example.com"
    assert "live" in sent[0]["subject"].lower()


def test_activation_notification_skipped_gracefully_when_email_not_configured(client, owner_session, activate_tenant):
    """Uses the plain `client`/`owner_session` fixtures (no email config)
    to prove activation still succeeds without crashing when there's no
    email provider configured — best-effort, not a hard dependency."""
    headers, tenant_id = owner_session
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    activate_tenant(tenant_id)  # would raise/fail loudly if the notification path broke activation
