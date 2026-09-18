"""HTTP-level tests for Phase 17's restaurant setup routes: menu items,
recipes, suppliers, inventory par levels — RBAC (owner/manager, not
staff) and tenant isolation.
"""

from __future__ import annotations

import pytest

from business_ai.auth import Principal, create_access_token


@pytest.fixture(autouse=True)
def _growth_plan(services, owner_session):
    # Restaurant setup (MANAGE_MENU) is Growth-tier under Phase 1's plan-gating.
    _, tenant_id = owner_session
    services.tenant_registry.update_config(tenant_id, plan="growth")


def test_menu_item_crud_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    manager_token = create_access_token(Principal.manager("mgr_1", tenant_id), services.settings)
    staff_headers = {"Authorization": f"Bearer {staff_token}"}
    manager_headers = {"Authorization": f"Bearer {manager_token}"}

    r = client.post(f"/api/menu/items?tenant_id={tenant_id}", json={"name": "Dish", "price_inr": 100}, headers=staff_headers)
    assert r.status_code == 403

    r = client.post(f"/api/menu/items?tenant_id={tenant_id}", json={"name": "Dish", "price_inr": 100}, headers=manager_headers)
    assert r.status_code == 200, r.text

    r = client.get(f"/api/menu/items?tenant_id={tenant_id}", headers=staff_headers)
    assert r.status_code == 403  # VIEW_INVENTORY is also owner/manager only, like VIEW_FINANCIALS

    r = client.get(f"/api/menu/items?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 1


def test_menu_item_routes_are_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "menu-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz Menu"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r = client.get(f"/api/menu/items?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_create_menu_item_rejects_invalid_price(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(f"/api/menu/items?tenant_id={tenant_id}", json={"name": "Dish", "price_inr": 0}, headers=headers)
    assert r.status_code == 400


def test_update_menu_item_price(client, owner_session):
    headers, tenant_id = owner_session
    created = client.post(f"/api/menu/items?tenant_id={tenant_id}", json={"name": "Dish", "price_inr": 100}, headers=headers).json()
    r = client.patch(f"/api/menu/items/{created['menu_item_id']}?tenant_id={tenant_id}", json={"price_inr": 150}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["price_inr"] == 150


def test_set_and_get_recipe(client, owner_session, services):
    headers, tenant_id = owner_session
    created = client.post(
        f"/api/menu/items?tenant_id={tenant_id}", json={"name": "Butter Chicken", "price_inr": 350}, headers=headers,
    ).json()
    r = client.post(
        f"/api/menu/items/{created['menu_item_id']}/recipe?tenant_id={tenant_id}",
        json={"lines": [{"ingredient_name": "Chicken", "quantity": 200, "unit": "g"}]}, headers=headers,
    )
    assert r.status_code == 200, r.text
    r = client.get(f"/api/menu/items/{created['menu_item_id']}/recipe?tenant_id={tenant_id}", headers=headers)
    assert len(r.json()["lines"]) == 1
    assert r.json()["lines"][0]["ingredient_name"] == "Chicken"


def test_set_recipe_for_unknown_menu_item_is_404(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(
        f"/api/menu/items/does_not_exist/recipe?tenant_id={tenant_id}",
        json={"lines": [{"ingredient_name": "Chicken", "quantity": 1, "unit": "g"}]}, headers=headers,
    )
    assert r.status_code == 404


def test_supplier_create_and_list(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(f"/api/suppliers?tenant_id={tenant_id}", json={"name": "Ramesh Vegetables", "phone": "9876500001"}, headers=headers)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/suppliers?tenant_id={tenant_id}", headers=headers)
    assert len(r.json()["suppliers"]) == 1


def test_inventory_par_level_and_list_low_stock_flag(client, owner_session, services):
    headers, tenant_id = owner_session
    r = client.post(
        f"/api/inventory/par-level?tenant_id={tenant_id}", json={"ingredient_name": "Rice", "par_level": 10, "unit": "kg"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    services.inventory_store.adjust_quantity(tenant_id, "Rice", delta=2, unit="kg")

    r = client.get(f"/api/inventory?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["low_stock"] is True
