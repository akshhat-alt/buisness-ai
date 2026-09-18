"""Tests for endpoint-specific in-memory rate limiting:
- IP-level rate limiting on POST /api/auth/signup (e.g. 10/hr/IP)
- Tenant-level rate limiting on customer-facing AI questions in _process_question (e.g. 30/min/tenant)
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from business_ai.app import Services, create_app
from business_ai.retrieval import HashEmbeddingProvider
from business_ai.tenant import TenantConfig, TenantStatus
from tests.conftest import FakeGenerator


@pytest.fixture()
def services_rate_limited(tmp_path: Path, settings):
    custom_settings = dataclasses.replace(
        settings,
        signup_requests_per_hour=3,
        ai_questions_per_minute=3,
        ip_requests_per_minute=100,  # Ensure global IP limiter does not interfere
        tenant_requests_per_minute=100,  # Ensure global tenant limiter does not interfere
    )
    svc = Services(custom_settings, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    yield svc
    svc.vector_store.close()


@pytest.fixture()
def client_rate_limited(services_rate_limited):
    app = create_app(services_rate_limited)
    return TestClient(app)


def test_signup_ip_rate_limiting(client_rate_limited):
    ip_headers = {"X-Forwarded-For": "198.51.100.1"}

    # 1. First 3 signups from 198.51.100.1 should succeed
    for i in range(3):
        r = client_rate_limited.post(
            "/api/auth/signup",
            json={
                "email": f"owner{i}@example.com",
                "password": "pass123456",
                "name": f"Owner {i}",
                "business_name": f"Business {i}",
            },
            headers=ip_headers,
        )
        assert r.status_code == 200, f"Expected 200 on attempt {i+1}, got {r.status_code}: {r.text}"

    # 2. 4th signup from same IP trips the limit and returns 429
    r_blocked = client_rate_limited.post(
        "/api/auth/signup",
        json={
            "email": "owner_blocked@example.com",
            "password": "pass123456",
            "name": "Blocked Owner",
            "business_name": "Blocked Business",
        },
        headers=ip_headers,
    )
    assert r_blocked.status_code == 429, r_blocked.text
    assert "Too many signup attempts" in r_blocked.json()["detail"]

    # 3. A signup from a different IP is unaffected
    other_ip_headers = {"X-Forwarded-For": "198.51.100.2"}
    r_other = client_rate_limited.post(
        "/api/auth/signup",
        json={
            "email": "owner_other@example.com",
            "password": "pass123456",
            "name": "Other Owner",
            "business_name": "Other Business",
        },
        headers=other_ip_headers,
    )
    assert r_other.status_code == 200, r_other.text


def test_ai_question_tenant_rate_limiting(client_rate_limited, services_rate_limited):
    # Set up two active tenants
    services_rate_limited.tenant_registry.register(
        TenantConfig(
            tenant_id="tenant-a",
            business_name="Tenant A",
            owner_email="a@example.com",
            status=TenantStatus.ACTIVE,
        )
    )
    services_rate_limited.tenant_registry.register(
        TenantConfig(
            tenant_id="tenant-b",
            business_name="Tenant B",
            owner_email="b@example.com",
            status=TenantStatus.ACTIVE,
        )
    )

    # 1. First 3 questions to tenant-a succeed (different sessions to isolate from UsageLimiter per-session limits)
    for i in range(3):
        r = client_rate_limited.post(
            "/api/ask?tenant_id=tenant-a",
            json={"query": f"What is question {i}?", "session_id": f"sess_a_{i}"},
        )
        assert r.status_code == 200, f"Expected 200 on question {i+1}, got {r.status_code}: {r.text}"

    # 2. 4th question to tenant-a trips the tenant rate limit and returns 429
    r_blocked = client_rate_limited.post(
        "/api/ask?tenant_id=tenant-a",
        json={"query": "What is question 4?", "session_id": "sess_a_blocked"},
    )
    assert r_blocked.status_code == 429, r_blocked.text
    detail = r_blocked.json()["detail"]
    assert "RATE_LIMIT_EXCEEDED" in detail["error"]
    assert "Maximum 3 questions per minute allowed" in detail["error"]

    # 3. Questions to tenant-b remain unaffected
    r_other = client_rate_limited.post(
        "/api/ask?tenant_id=tenant-b",
        json={"query": "What is question 1 for B?", "session_id": "sess_b_1"},
    )
    assert r_other.status_code == 200, r_other.text
