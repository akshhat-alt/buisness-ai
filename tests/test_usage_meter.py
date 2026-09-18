"""Tests for Phase 1's UsageMeterStore: durable, per-tenant, per-period
usage counters — distinct from usage_limiter.py's decrementing
per-session quota (see usage_meter.py's own module docstring)."""

from __future__ import annotations

from pathlib import Path

from business_ai.usage_meter import UsageMeterStore, current_period


def test_increment_and_get_usage(tmp_path: Path):
    store = UsageMeterStore(tmp_path / "usage.db")
    store.increment(tenant_id="t1", metric="ai_messages")
    store.increment(tenant_id="t1", metric="ai_messages")
    store.increment(tenant_id="t1", metric="whatsapp_messages", by=3)

    usage = store.get_usage(tenant_id="t1")
    assert usage == {"ai_messages": 2, "whatsapp_messages": 3}


def test_usage_is_tenant_scoped(tmp_path: Path):
    store = UsageMeterStore(tmp_path / "usage.db")
    store.increment(tenant_id="t1", metric="ai_messages")
    store.increment(tenant_id="t2", metric="ai_messages", by=5)

    assert store.get_usage(tenant_id="t1") == {"ai_messages": 1}
    assert store.get_usage(tenant_id="t2") == {"ai_messages": 5}


def test_usage_is_period_scoped(tmp_path: Path):
    store = UsageMeterStore(tmp_path / "usage.db")
    store.increment(tenant_id="t1", metric="ai_messages", period="2026-01")
    store.increment(tenant_id="t1", metric="ai_messages", period="2026-02", by=4)

    assert store.get_usage(tenant_id="t1", period="2026-01") == {"ai_messages": 1}
    assert store.get_usage(tenant_id="t1", period="2026-02") == {"ai_messages": 4}


def test_get_usage_for_unknown_tenant_or_period_is_empty(tmp_path: Path):
    store = UsageMeterStore(tmp_path / "usage.db")
    assert store.get_usage(tenant_id="nobody") == {}


def test_current_period_is_current_calendar_month():
    import time

    assert current_period() == time.strftime("%Y-%m", time.gmtime())


def test_increment_defaults_to_current_period(tmp_path: Path):
    store = UsageMeterStore(tmp_path / "usage.db")
    store.increment(tenant_id="t1", metric="ai_messages")
    assert store.get_usage(tenant_id="t1", period=current_period()) == {"ai_messages": 1}
