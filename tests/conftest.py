from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from business_ai.app import Services, create_app
from business_ai.config import load_settings
from business_ai.generation import LLMResponseDraft
from business_ai.retrieval import HashEmbeddingProvider


class FakeGenerator:
    """Deterministic stand-in for the OpenAI generator: cites whatever
    evidence_passage id appears in the prompt, so tests never spend real
    API credits but still exercise the real validation/citation path."""

    def __init__(self, *, status: str = "answered", answer_text: str = "This is a grounded answer.") -> None:
        self.status = status
        self.answer_text = answer_text

    def generate(self, *, system_prompt: str, user_prompt: str) -> LLMResponseDraft:
        match = re.search(r'evidence_passage id="([^"]+)"', user_prompt)
        seg_id = match.group(1) if match else "seg_unknown"
        return LLMResponseDraft(
            status=self.status,
            answer_text=self.answer_text,
            cited_segment_ids=[seg_id] if self.status == "answered" else [],
        )

    def draft_faq_answer(self, *, business_name: str, assistant_name: str, question: str) -> str:
        return f"[Draft answer for {business_name} — fill in the details for: {question}]"


class FakeEmailSender:
    """Captures every send() call instead of hitting a real network
    endpoint — mirrors FakeGenerator/HashEmbeddingProvider's role."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, to: str, subject: str, html_body: str, text_body: str | None = None) -> None:
        self.sent.append({"to": to, "subject": subject, "html_body": html_body, "text_body": text_body})


@pytest.fixture()
def settings(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-local-dev-only-32bytes")
    monkeypatch.setenv("ADMIN_SECRET", "test-admin-secret-abcdef")
    return load_settings()


@pytest.fixture()
def services(tmp_path: Path, settings) -> Services:
    svc = Services(settings, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    return svc


@pytest.fixture()
def app(services):
    return create_app(services)


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture()
def owner_session(client):
    """Sign up a fresh business and return (headers, tenant_id)."""
    r = client.post(
        "/api/auth/signup",
        json={"email": "owner@example.com", "password": "secret123", "name": "Priya", "business_name": "Priya Salon"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["tenant_id"]


@pytest.fixture()
def admin_headers(client, settings):
    r = client.post("/api/auth/login", json={"email": "admin", "password": settings.admin_secret})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def activate_tenant(client, admin_headers):
    def _activate(tenant_id: str) -> None:
        r = client.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
        assert r.status_code == 200, r.text

    return _activate


@pytest.fixture()
def services_with_email(tmp_path: Path, settings) -> Services:
    """Same as `services`, but with digest-email config present and a
    FakeEmailSender capturing sends — for tests of the digest-run route.
    A separate fixture chain (not a mutation of `services`) because the
    email-not-configured path also needs its own test coverage using the
    plain `services` fixture."""
    settings_with_email = dataclasses.replace(
        settings,
        resend_api_key="re_test_key",
        digest_from_email="digest@business-ai.example",
        public_base_url="https://app.example.com",
    )
    svc = Services(settings_with_email, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    fake_sender = FakeEmailSender()
    svc.email_sender = lambda: fake_sender
    svc.fake_email_sender = fake_sender  # test-only handle to inspect .sent
    return svc


@pytest.fixture()
def app_with_email(services_with_email):
    return create_app(services_with_email)


@pytest.fixture()
def client_with_email(app_with_email):
    from fastapi.testclient import TestClient

    return TestClient(app_with_email)
