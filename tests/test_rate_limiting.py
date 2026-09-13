"""Tests for Phase 9's IP/tenant rate-limiting middleware — a broad
abuse guard layered in front of (never replacing) the existing
per-(tenant, session) question quota in usage_limiter.py."""

from __future__ import annotations

import dataclasses

import pytest

from business_ai.app import Services, create_app
from business_ai.rate_limiting import FixedWindowRateLimiter, client_ip


# ------------------------------------------------------------------ FixedWindowRateLimiter unit tests


def test_allows_requests_under_the_limit():
    limiter = FixedWindowRateLimiter(limit_per_minute=3)
    assert limiter.check_and_increment("key-a") is True
    assert limiter.check_and_increment("key-a") is True
    assert limiter.check_and_increment("key-a") is True


def test_blocks_requests_over_the_limit():
    limiter = FixedWindowRateLimiter(limit_per_minute=2)
    assert limiter.check_and_increment("key-a") is True
    assert limiter.check_and_increment("key-a") is True
    assert limiter.check_and_increment("key-a") is False


def test_different_keys_have_independent_windows():
    limiter = FixedWindowRateLimiter(limit_per_minute=1)
    assert limiter.check_and_increment("key-a") is True
    assert limiter.check_and_increment("key-b") is True  # unaffected by key-a's count


def test_window_resets_after_60_seconds():
    limiter = FixedWindowRateLimiter(limit_per_minute=1)
    assert limiter.check_and_increment("key-a", now=1000.0) is True
    assert limiter.check_and_increment("key-a", now=1010.0) is False  # still in window
    assert limiter.check_and_increment("key-a", now=1061.0) is True  # new window


def test_zero_limit_disables_the_dimension():
    limiter = FixedWindowRateLimiter(limit_per_minute=0)
    for _ in range(1000):
        assert limiter.check_and_increment("key-a") is True


def test_sweep_stale_removes_old_windows():
    limiter = FixedWindowRateLimiter(limit_per_minute=5)
    limiter.check_and_increment("old-key", now=1000.0)
    limiter.check_and_increment("fresh-key", now=2000.0)
    removed = limiter.sweep_stale(older_than_seconds=500.0, now=2000.0)
    assert removed == 1
    assert "old-key" not in limiter._windows
    assert "fresh-key" in limiter._windows


# ------------------------------------------------------------------ middleware integration tests


@pytest.fixture()
def tight_client(tmp_path, settings):
    tight_settings = dataclasses.replace(settings, ip_requests_per_minute=3, tenant_requests_per_minute=2)
    svc = Services(tight_settings, data_root=tmp_path)
    from business_ai.retrieval import HashEmbeddingProvider
    from tests.conftest import FakeGenerator

    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    from fastapi.testclient import TestClient

    yield TestClient(create_app(svc))
    svc.vector_store.close()


def test_ip_rate_limit_returns_429_when_exceeded(tight_client):
    for _ in range(3):
        r = tight_client.get("/healthz")
        assert r.status_code == 200
    r = tight_client.get("/healthz")
    assert r.status_code == 429
    assert "too many requests" in r.json()["detail"].lower()


def test_tenant_rate_limit_returns_429_when_exceeded(tight_client):
    # tenant limit (2) is tighter than ip limit (3) here, and only counts
    # requests that name a tenant_id.
    r1 = tight_client.get("/api/tenant/public?tenant_id=some-tenant")
    r2 = tight_client.get("/api/tenant/public?tenant_id=some-tenant")
    r3 = tight_client.get("/api/tenant/public?tenant_id=some-tenant")
    assert r1.status_code == 404  # unknown tenant, but request was allowed through
    assert r2.status_code == 404
    assert r3.status_code == 429


def test_requests_without_tenant_id_are_unaffected_by_tenant_limit(tight_client):
    # /healthz never carries tenant_id, so only the (looser) IP limit applies.
    for _ in range(3):
        assert tight_client.get("/healthz").status_code == 200


def test_rate_limit_response_still_carries_a_request_id(tight_client):
    for _ in range(3):
        tight_client.get("/healthz")
    r = tight_client.get("/healthz")
    assert r.status_code == 429
    assert "X-Request-ID" in r.headers


def test_generous_defaults_do_not_throttle_normal_dashboard_load(client):
    """Regression guard: the default limits must stay generous enough
    that a real dashboard page load (a dozen-plus calls) never trips
    them — this is an abuse guard, not a tight per-user quota."""
    for _ in range(20):
        assert client.get("/healthz").status_code == 200


def test_client_ip_prefers_x_forwarded_for(client):
    # Exercised indirectly via a real request; this directly unit-tests
    # the header-parsing precedence client_ip() documents.
    class _FakeClient:
        host = "10.0.0.1"

    class _FakeRequest:
        headers = {"x-forwarded-for": "203.0.113.5, 10.0.0.1"}
        client = _FakeClient()

    assert client_ip(_FakeRequest()) == "203.0.113.5"


def test_client_ip_falls_back_to_direct_connection():
    class _FakeClient:
        host = "127.0.0.1"

    class _FakeRequest:
        headers = {}
        client = _FakeClient()

    assert client_ip(_FakeRequest()) == "127.0.0.1"
