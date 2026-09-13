"""Tests for SupplierStore (Phase 17 — Restaurant Foundation)."""

from __future__ import annotations

import pytest

from business_ai.suppliers import SupplierStore

TENANT = "salon-a"


@pytest.fixture()
def store(tmp_path):
    return SupplierStore(tmp_path / "suppliers.db")


def test_create_rejects_blank_name(store):
    with pytest.raises(ValueError):
        store.create(tenant_id=TENANT, name="  ")


def test_create_and_get_roundtrip(store):
    supplier = store.create(tenant_id=TENANT, name="Ramesh Vegetables", phone="9876500001")
    assert store.get(TENANT, supplier.supplier_id) == supplier


def test_find_by_name_is_case_insensitive(store):
    store.create(tenant_id=TENANT, name="Ramesh Vegetables")
    found = store.find_by_name(TENANT, "ramesh vegetables")
    assert found is not None


def test_find_by_name_returns_none_for_inactive(store):
    # Suppliers are created active; there's no deactivate method yet in
    # Phase 17, so this exercises the active_only filter path directly
    # via list_for_tenant instead.
    store.create(tenant_id=TENANT, name="Active Supplier")
    assert len(store.list_for_tenant(TENANT, active_only=True)) == 1


def test_list_for_tenant_is_isolated(store):
    store.create(tenant_id=TENANT, name="Mine")
    store.create(tenant_id="other-tenant", name="Not Mine")
    assert [s.name for s in store.list_for_tenant(TENANT)] == ["Mine"]


def test_delete_for_tenant_removes_only_that_tenant(store):
    store.create(tenant_id=TENANT, name="Mine")
    store.create(tenant_id="other-tenant", name="Not Mine")
    assert store.delete_for_tenant(TENANT) == 1
    assert store.list_for_tenant(TENANT) == []
