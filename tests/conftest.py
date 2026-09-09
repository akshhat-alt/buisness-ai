from __future__ import annotations

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
