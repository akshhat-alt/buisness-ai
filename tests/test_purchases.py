"""Tests for PurchaseStore (Phase 17 — Restaurant Foundation)."""

from __future__ import annotations

import pytest

from business_ai.purchases import PurchaseStore, PurchaseUnitMismatchError

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


def test_sum_for_ingredient_reports_unit_and_none_when_no_history(store):
    assert store.sum_for_ingredient(TENANT, "Chicken")["unit"] is None
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=10, unit="kg", amount_inr=4000)
    assert store.sum_for_ingredient(TENANT, "Chicken")["unit"] == "kg"


def test_sum_for_ingredient_converts_compatible_mass_units(store):
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=5, unit="kg", amount_inr=1000)
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=500, unit="g", amount_inr=150)
    summary = store.sum_for_ingredient(TENANT, "Chicken")
    assert summary["unit"] == "g"
    assert summary["total_quantity"] == 5500  # 5kg -> 5000g + 500g
    assert summary["total_amount_inr"] == 1150
    assert summary["purchase_count"] == 2


def test_sum_for_ingredient_converts_compatible_volume_units(store):
    store.record(tenant_id=TENANT, ingredient_name="Milk", quantity=2, unit="l", amount_inr=120)
    store.record(tenant_id=TENANT, ingredient_name="Milk", quantity=250, unit="ml", amount_inr=15)
    summary = store.sum_for_ingredient(TENANT, "Milk")
    assert summary["unit"] == "ml"
    assert summary["total_quantity"] == 2250  # 2L -> 2000ml + 250ml
    assert summary["total_amount_inr"] == 135


def test_sum_for_ingredient_raises_on_incompatible_units(store):
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=5, unit="kg", amount_inr=1000)
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=10, unit="pieces", amount_inr=800)
    with pytest.raises(PurchaseUnitMismatchError):
        store.sum_for_ingredient(TENANT, "Chicken")


def test_list_for_tenant_is_isolated(store):
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1, unit="kg", amount_inr=100)
    store.record(tenant_id="other-tenant", ingredient_name="Chicken", quantity=1, unit="kg", amount_inr=999)
    assert len(store.list_for_tenant(TENANT)) == 1


def test_delete_for_tenant_removes_only_that_tenant(store):
    store.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1, unit="kg", amount_inr=100)
    store.record(tenant_id="other-tenant", ingredient_name="Chicken", quantity=1, unit="kg", amount_inr=100)
    assert store.delete_for_tenant(TENANT) == 1
    assert store.list_for_tenant(TENANT) == []
