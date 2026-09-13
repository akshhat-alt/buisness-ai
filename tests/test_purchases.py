"""Tests for PurchaseStore (Phase 17 — Restaurant Foundation)."""

from __future__ import annotations

import pytest

from business_ai.purchases import PurchaseStore

TENANT = "salon-a"


@pytest.fixture()
def store(tmp_path):
    return PurchaseStore(tmp_path / "purchases.db")


def test_record_rejects_non_positive_quantity(store):
    with pytest.raises(ValueError):
        store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=0, unit="kg", amount_inr=100)


def test_record_rejects_negative_amount(store):
    with pytest.raises(ValueError):
        store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1, unit="kg", amount_inr=-1)


def test_record_and_list_roundtrip(store):
    p = store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=10, unit="kg", amount_inr=4200, supplier_id="sup_1")
    entries = store.list_for_tenant(TENANT)
    assert len(entries) == 1
    assert entries[0] == p


def test_sum_for_ingredient_aggregates_across_purchases(store):
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=10, unit="kg", amount_inr=4000)
    store.record(tenant_id=TENANT, ingredient_name="chicken", quantity=5, unit="kg", amount_inr=2000)  # different casing
    summary = store.sum_for_ingredient(TENANT, "CHICKEN")
    assert summary["total_quantity"] == 15
    assert summary["total_amount_inr"] == 6000
    assert summary["purchase_count"] == 2


def test_sum_for_ingredient_is_isolated_by_ingredient(store):
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=10, unit="kg", amount_inr=4000)
    store.record(tenant_id=TENANT, ingredient_name="Rice", quantity=10, unit="kg", amount_inr=1000)
    summary = store.sum_for_ingredient(TENANT, "Chicken")
    assert summary["total_amount_inr"] == 4000


def test_list_for_tenant_is_isolated(store):
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1, unit="kg", amount_inr=100)
    store.record(tenant_id="other-tenant", ingredient_name="Chicken", quantity=1, unit="kg", amount_inr=999)
    assert len(store.list_for_tenant(TENANT)) == 1


def test_delete_for_tenant_removes_only_that_tenant(store):
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1, unit="kg", amount_inr=100)
    store.record(tenant_id="other-tenant", ingredient_name="Chicken", quantity=1, unit="kg", amount_inr=100)
    assert store.delete_for_tenant(TENANT) == 1
    assert store.list_for_tenant(TENANT) == []
