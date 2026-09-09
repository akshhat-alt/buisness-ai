"""Unit tests for UsageLimiter: quota enforcement, concurrency limiting,
rate limiting, reservation release/finalize, and the kill switch — the
mechanism that stands between a real tenant and an unbounded OpenAI bill."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from business_ai.config import load_settings
from business_ai.usage_limiter import AccessDecision, UsageLimiter


@pytest.fixture()
def limiter(tmp_path: Path, monkeypatch) -> UsageLimiter:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-local-dev-only-32bytes")
    settings = load_settings()
    return UsageLimiter(tmp_path / "usage.db", settings)


def _with(limiter: UsageLimiter, **overrides) -> UsageLimiter:
    """Settings is frozen (by design — config shouldn't mutate at runtime);
    build a fresh limiter sharing the same db with the overridden settings
    for tests that need a different concurrency/kill-switch value."""
    new_settings = dataclasses.replace(limiter.settings, **overrides)
    return UsageLimiter(limiter.db_path, new_settings)


def test_quota_decrements_and_denies_when_exhausted(limiter):
    # Raise the per-minute rate limit so this test isolates quota behavior
    # from the (separately tested) rate limiter.
    unrated = _with(limiter, requests_per_minute=1000)
    for i in range(3):
        res = unrated.check_and_reserve("tenant_a", "sess_1", f"req_{i}", quota_override=3)
        assert res.decision == AccessDecision.ALLOW
        unrated.finalize_reservation(f"req_{i}")

    res = unrated.check_and_reserve("tenant_a", "sess_1", "req_overflow", quota_override=3)
    assert res.decision == AccessDecision.DENY
    assert res.questions_remaining == 0


def test_released_reservation_restores_quota(limiter):
    res = limiter.check_and_reserve("tenant_a", "sess_1", "req_1", quota_override=1)
    assert res.decision == AccessDecision.ALLOW
    assert res.questions_remaining == 0

    limiter.release_reservation("req_1")

    res2 = limiter.check_and_reserve("tenant_a", "sess_1", "req_2", quota_override=1)
    assert res2.decision == AccessDecision.ALLOW


def test_default_quota_uses_settings_when_no_override(limiter):
    res = limiter.check_and_reserve("tenant_a", "sess_1", "req_1")
    assert res.questions_limit == limiter.settings.active_tenant_quota


def test_tenant_sessions_are_independent(limiter):
    limiter.check_and_reserve("tenant_a", "sess_1", "req_1", quota_override=1)
    res = limiter.check_and_reserve("tenant_b", "sess_1", "req_2", quota_override=1)
    assert res.decision == AccessDecision.ALLOW  # different tenant, same session id


def test_concurrent_request_limit_blocks_second_in_flight_request(limiter):
    limited = _with(limiter, max_concurrent_requests=1)
    res1 = limited.check_and_reserve("tenant_a", "sess_1", "req_1", quota_override=10)
    assert res1.decision == AccessDecision.ALLOW

    res2 = limited.check_and_reserve("tenant_a", "sess_1", "req_2", quota_override=10)
    assert res2.decision == AccessDecision.RATE_LIMITED


def test_kill_switch_denies_every_request(limiter):
    paused = _with(limiter, enabled=False)
    res = paused.check_and_reserve("tenant_a", "sess_1", "req_1", quota_override=10)
    assert res.decision == AccessDecision.DENY
    assert "KILL_SWITCH_ACTIVE" in res.reason
