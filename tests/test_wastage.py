"""Tests for WastageStore (Phase 17 — Restaurant Foundation)."""

from __future__ import annotations

import pytest

from business_ai.wastage import WastageStore

TENANT = "salon-a"


@pytest.fixture()
def store(tmp_path):
    return WastageStore(tmp_path / "wastage.db")


def test_record_rejects_non_positive_quantity(store):
    with pytest.raises(ValueError):
        store.record(tenant_id=TENANT, ingredient_name="Paneer", quantity=0, unit="kg")


def test_record_normalizes_unknown_reason_to_other(store):
    entry = store.record(tenant_id=TENANT, ingredient_name="Paneer", quantity=1, unit="kg", reason="dropped on the floor")
    assert entry.reason == "other"


def test_record_accepts_known_reason(store):
    entry = store.record(tenant_id=TENANT, ingredient_name="Paneer", quantity=1, unit="kg", reason="SPOILED")
    assert entry.reason == "spoiled"


def test_record_clamps_negative_cost_to_zero(store):
    entry = store.record(tenant_id=TENANT, ingredient_name="Paneer", quantity=1, unit="kg", estimated_cost_inr=-50)
    assert entry.estimated_cost_inr == 0


def test_list_for_tenant_is_isolated(store):
    store.record(tenant_id=TENANT, ingredient_name="Paneer", quantity=1, unit="kg")
    store.record(tenant_id="other-tenant", ingredient_name="Paneer", quantity=1, unit="kg")
    assert len(store.list_for_tenant(TENANT)) == 1


def test_total_cost_for_tenant_sums_estimated_costs(store):
    store.record(tenant_id=TENANT, ingredient_name="Paneer", quantity=1, unit="kg", estimated_cost_inr=200)
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1, unit="kg", estimated_cost_inr=300)
    assert store.total_cost_for_tenant(TENANT) == 500


def test_total_cost_for_tenant_is_zero_when_no_entries(store):
    assert store.total_cost_for_tenant(TENANT) == 0


def test_delete_for_tenant_removes_only_that_tenant(store):
    store.record(tenant_id=TENANT, ingredient_name="Paneer", quantity=1, unit="kg")
    store.record(tenant_id="other-tenant", ingredient_name="Paneer", quantity=1, unit="kg")
    assert store.delete_for_tenant(TENANT) == 1
    assert store.list_for_tenant(TENANT) == []
