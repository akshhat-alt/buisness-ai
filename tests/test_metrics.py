"""Tests for the Financial Truth Layer (Phase 12) — BusinessMetricStore's
pure logic, exercised against a real temp SQLite instance.
"""

from __future__ import annotations

import pytest

from business_ai.metrics import BusinessMetricStore

TENANT = "salon-a"


@pytest.fixture()
def store(tmp_path):
    return BusinessMetricStore(tmp_path / "metrics.db")


def test_record_rejects_unknown_metric_type(store):
    with pytest.raises(ValueError):
        store.record(tenant_id=TENANT, metric_type="refund", amount_inr=100)


def test_record_rejects_non_positive_amount(store):
    with pytest.raises(ValueError):
        store.record(tenant_id=TENANT, metric_type="sale", amount_inr=0)


def test_record_and_list_roundtrip(store):
    m = store.record(tenant_id=TENANT, metric_type="sale", amount_inr=1500, note="haircut", reported_by_employee_id="emp1")
    entries = store.list_for_tenant(TENANT)
    assert len(entries) == 1
    assert entries[0].metric_id == m.metric_id
    assert entries[0].source == "manual"


def test_summary_aggregates_by_type(store):
    store.record(tenant_id=TENANT, metric_type="sale", amount_inr=1000)
    store.record(tenant_id=TENANT, metric_type="sale", amount_inr=500)
    store.record(tenant_id=TENANT, metric_type="expense", amount_inr=300)
    summary = {s.metric_type: s for s in store.summary_for_tenant(TENANT)}
    assert summary["sale"].total_inr == 1500
    assert summary["sale"].entry_count == 2
    assert summary["expense"].total_inr == 300


def test_summary_is_tenant_isolated(store):
    store.record(tenant_id=TENANT, metric_type="sale", amount_inr=1000)
    store.record(tenant_id="other-tenant", metric_type="sale", amount_inr=9999)
    summary = store.summary_for_tenant(TENANT)
    assert len(summary) == 1
    assert summary[0].total_inr == 1000


def test_delete_for_tenant_removes_only_that_tenant(store):
    store.record(tenant_id=TENANT, metric_type="sale", amount_inr=1000)
    store.record(tenant_id="other-tenant", metric_type="sale", amount_inr=500)
    assert store.delete_for_tenant(TENANT) == 1
    assert store.list_for_tenant(TENANT) == []
    assert len(store.list_for_tenant("other-tenant")) == 1
