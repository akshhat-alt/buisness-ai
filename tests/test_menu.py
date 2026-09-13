"""Tests for MenuStore (Phase 17 — Restaurant Foundation): menu items
and their recipes, real temp SQLite, no mocks.
"""

from __future__ import annotations

import pytest

from business_ai.menu import MenuStore, normalize_ingredient_name

TENANT = "salon-a"


@pytest.fixture()
def store(tmp_path):
    return MenuStore(tmp_path / "menu.db")


def test_normalize_ingredient_name_collapses_case_and_whitespace():
    assert normalize_ingredient_name("  Chicken   Breast ") == "chicken breast"


def test_create_item_rejects_blank_name(store):
    with pytest.raises(ValueError):
        store.create_item(tenant_id=TENANT, name="  ", price_inr=100)


def test_create_item_rejects_non_positive_price(store):
    with pytest.raises(ValueError):
        store.create_item(tenant_id=TENANT, name="Butter Chicken", price_inr=0)


def test_create_and_get_item_roundtrip(store):
    item = store.create_item(tenant_id=TENANT, name="Butter Chicken", price_inr=350, category="Mains")
    fetched = store.get_item(TENANT, item.menu_item_id)
    assert fetched == item
    assert fetched.active is True


def test_find_by_name_is_case_and_whitespace_insensitive(store):
    store.create_item(tenant_id=TENANT, name="Butter Chicken", price_inr=350)
    found = store.find_by_name(TENANT, "  butter   chicken ")
    assert found is not None
    assert found.name == "Butter Chicken"


def test_find_by_name_ignores_inactive_items(store):
    item = store.create_item(tenant_id=TENANT, name="Discontinued Dish", price_inr=100)
    store.update_item(TENANT, item.menu_item_id, active=False)
    assert store.find_by_name(TENANT, "Discontinued Dish") is None


def test_list_for_tenant_is_isolated_and_sorted(store):
    store.create_item(tenant_id=TENANT, name="Zebra Roll", price_inr=100, category="Rolls")
    store.create_item(tenant_id=TENANT, name="Apple Pie", price_inr=100, category="Desserts")
    store.create_item(tenant_id="other-tenant", name="Should Not Appear", price_inr=100)
    items = store.list_for_tenant(TENANT)
    assert [i.name for i in items] == ["Apple Pie", "Zebra Roll"]  # ordered by category then name


def test_update_item_rejects_unknown_field(store):
    item = store.create_item(tenant_id=TENANT, name="Dish", price_inr=100)
    with pytest.raises(ValueError):
        store.update_item(TENANT, item.menu_item_id, unknown_field="x")


def test_update_item_changes_price(store):
    item = store.create_item(tenant_id=TENANT, name="Dish", price_inr=100)
    updated = store.update_item(TENANT, item.menu_item_id, price_inr=150)
    assert updated.price_inr == 150


def test_set_recipe_replaces_prior_lines(store):
    item = store.create_item(tenant_id=TENANT, name="Butter Chicken", price_inr=350)
    store.set_recipe(TENANT, item.menu_item_id, [
        {"ingredient_name": "Chicken", "quantity": 200, "unit": "g"},
        {"ingredient_name": "Butter", "quantity": 30, "unit": "g"},
    ])
    store.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "Chicken", "quantity": 250, "unit": "g"}])
    lines = store.get_recipe(TENANT, item.menu_item_id)
    assert len(lines) == 1
    assert lines[0].quantity == 250
    assert lines[0].ingredient_key == "chicken"


def test_set_recipe_rejects_non_positive_quantity(store):
    item = store.create_item(tenant_id=TENANT, name="Dish", price_inr=100)
    with pytest.raises(ValueError):
        store.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "Salt", "quantity": 0, "unit": "g"}])


def test_delete_for_tenant_removes_items_and_recipes(store):
    item = store.create_item(tenant_id=TENANT, name="Dish", price_inr=100)
    store.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "Salt", "quantity": 1, "unit": "g"}])
    store.create_item(tenant_id="other-tenant", name="Other Dish", price_inr=100)

    deleted = store.delete_for_tenant(TENANT)
    assert deleted == 2  # 1 menu item + 1 recipe line
    assert store.list_for_tenant(TENANT) == []
    assert len(store.list_for_tenant("other-tenant")) == 1
