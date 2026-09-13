"""Tests for InventoryStore (Phase 17 — Restaurant Foundation): stock
tracking keyed by normalized ingredient name, real temp SQLite.
"""

from __future__ import annotations

import pytest

from business_ai.inventory import InventoryStore, UnitMismatchError

TENANT = "salon-a"


@pytest.fixture()
def store(tmp_path):
    return InventoryStore(tmp_path / "inventory.db")


def test_get_returns_none_for_unknown_ingredient(store):
    assert store.get(TENANT, "Chicken") is None


def test_adjust_quantity_auto_creates_row_at_zero_par(store):
    item = store.adjust_quantity(TENANT, "Chicken", delta=5, unit="kg")
    assert item.quantity_on_hand == 5
    assert item.par_level == 0
    assert item.unit == "kg"


def test_adjust_quantity_is_keyed_by_normalized_name(store):
    store.adjust_quantity(TENANT, "Chicken Breast", delta=10, unit="kg")
    store.adjust_quantity(TENANT, "  chicken   breast ", delta=-3, unit="kg")
    item = store.get(TENANT, "CHICKEN BREAST")
    assert item.quantity_on_hand == 7


def test_adjust_quantity_rejects_a_mismatched_unit_instead_of_corrupting_the_number(store):
    """Regression test for a real bug caught during Phase 17's own live
    verification: a 5kg purchase followed by a 200g depletion for the
    SAME ingredient must never silently compute a wrong quantity."""
    store.adjust_quantity(TENANT, "Chicken", delta=5, unit="kg")
    with pytest.raises(UnitMismatchError):
        store.adjust_quantity(TENANT, "Chicken", delta=-200, unit="g")
    # The rejected call must not have partially applied.
    assert store.get(TENANT, "Chicken").quantity_on_hand == 5
    assert store.get(TENANT, "Chicken").unit == "kg"


def test_adjust_quantity_allows_consistent_repeated_unit(store):
    store.adjust_quantity(TENANT, "Chicken", delta=5, unit="kg")
    item = store.adjust_quantity(TENANT, "Chicken", delta=-2, unit="kg")
    assert item.quantity_on_hand == 3
    assert item.unit == "kg"


def test_adjust_quantity_allows_omitted_unit_after_the_first_call(store):
    store.adjust_quantity(TENANT, "Chicken", delta=5, unit="kg")
    item = store.adjust_quantity(TENANT, "Chicken", delta=-1, unit=None)
    assert item.quantity_on_hand == 4
    assert item.unit == "kg"


def test_adjust_quantity_can_go_negative_honestly(store):
    """A depletion without a prior purchase is a real signal (used
    without being bought), not an error to hide."""
    item = store.adjust_quantity(TENANT, "Paneer", delta=-2, unit="kg")
    assert item.quantity_on_hand == -2


def test_set_par_level_creates_row_at_zero_stock(store):
    item = store.set_par_level(TENANT, "Rice", par_level=10, unit="kg")
    assert item.quantity_on_hand == 0
    assert item.par_level == 10


def test_set_par_level_updates_existing_row_without_resetting_stock(store):
    store.adjust_quantity(TENANT, "Rice", delta=20, unit="kg")
    item = store.set_par_level(TENANT, "Rice", par_level=5, unit="kg")
    assert item.quantity_on_hand == 20
    assert item.par_level == 5


def test_list_low_stock_only_flags_items_with_a_configured_par_level(store):
    store.adjust_quantity(TENANT, "Chicken", delta=1, unit="kg")  # no par level set — never "low"
    store.set_par_level(TENANT, "Rice", par_level=10, unit="kg")
    store.adjust_quantity(TENANT, "Rice", delta=2, unit="kg")  # below par
    low = store.list_low_stock(TENANT)
    assert [i.ingredient_name for i in low] == ["Rice"]


def test_list_low_stock_excludes_items_at_or_above_par(store):
    store.set_par_level(TENANT, "Rice", par_level=10, unit="kg")
    store.adjust_quantity(TENANT, "Rice", delta=10, unit="kg")
    assert store.list_low_stock(TENANT) == []


def test_list_for_tenant_is_isolated(store):
    store.adjust_quantity(TENANT, "Chicken", delta=1, unit="kg")
    store.adjust_quantity("other-tenant", "Chicken", delta=99, unit="kg")
    items = store.list_for_tenant(TENANT)
    assert len(items) == 1
    assert items[0].quantity_on_hand == 1


def test_delete_for_tenant_removes_only_that_tenant(store):
    store.adjust_quantity(TENANT, "Chicken", delta=1, unit="kg")
    store.adjust_quantity("other-tenant", "Chicken", delta=1, unit="kg")
    assert store.delete_for_tenant(TENANT) == 1
    assert store.list_for_tenant(TENANT) == []
    assert len(store.list_for_tenant("other-tenant")) == 1
