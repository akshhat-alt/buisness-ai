"""HTTP-level tests for Phase 22's Restaurant Profitability Intelligence
routes (GET /api/menu-engineering, GET /api/reorder-suggestions) and
Phase 24's Restaurant Autopilot routes (GET /api/menu-recommendations,
POST /api/menu-engineering/simulate, GET /api/digital-gm-briefing). RBAC
(owner+manager via VIEW_INVENTORY, same gate as every other menu/
inventory read), tenant isolation.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _growth_plan(services, owner_session):
    # Restaurant intelligence (VIEW_INVENTORY/MANAGE_MENU) is Growth-tier
    # under Phase 1's plan-gating.
    _, tenant_id = owner_session
    services.tenant_registry.update_config(tenant_id, plan="growth")

from business_ai.auth import Principal, create_access_token


def test_menu_engineering_route_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    services.menu_store.create_item(tenant_id=tenant_id, name="Lassi", price_inr=80)

    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = client.get(f"/api/menu-engineering?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403

    r2 = client.get(f"/api/menu-engineering?tenant_id={tenant_id}", headers=headers)
    assert r2.status_code == 200, r2.text
    assert len(r2.json()["dishes"]) == 1
    assert r2.json()["dishes"][0]["name"] == "Lassi"


def test_menu_engineering_route_allows_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    manager_token = create_access_token(Principal.manager("mgr_1", tenant_id), services.settings)
    r = client.get(f"/api/menu-engineering?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {manager_token}"})
    assert r.status_code == 200, r.text


def test_menu_engineering_is_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "menu-eng-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r = client.get(f"/api/menu-engineering?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_reorder_suggestions_route_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = client.get(f"/api/reorder-suggestions?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403

    r2 = client.get(f"/api/reorder-suggestions?tenant_id={tenant_id}", headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["suggestions"] == []


def test_reorder_suggestions_route_reflects_real_automation_history(client, owner_session, services):
    headers, tenant_id = owner_session
    services.inventory_store.adjust_quantity(tenant_id, "Paneer", delta=50, unit="g")
    services.inventory_store.set_par_level(tenant_id, "Paneer", par_level=1000, unit="g")
    from business_ai.automation import RunStatus

    for _ in range(2):
        services.automation_run_store.record(
            tenant_id=tenant_id, rule_id="r1", trigger_type="low_stock", target_type="inventory_item",
            target_id="inventory:paneer", action_type="notify_owner", status=RunStatus.SUCCESS,
        )
    r = client.get(f"/api/reorder-suggestions?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    suggestions = r.json()["suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["ingredient_name"] == "Paneer"


def test_reorder_suggestions_is_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "reorder-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r = client.get(f"/api/reorder-suggestions?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


# ------------------------------------------------------------------ menu recommendations / simulate / GM briefing (Phase 24)


def test_menu_recommendations_route_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = client.get(f"/api/menu-recommendations?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403

    r2 = client.get(f"/api/menu-recommendations?tenant_id={tenant_id}", headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["recommendations"] == []


def test_menu_recommendations_route_reflects_a_high_food_cost_dish(client, owner_session, services):
    headers, tenant_id = owner_session
    services.purchase_store.record(tenant_id=tenant_id, ingredient_name="X", quantity=1000, unit="g", amount_inr=1000)
    item = services.menu_store.create_item(tenant_id=tenant_id, name="Overpriced Recipe", price_inr=100)
    services.menu_store.set_recipe(tenant_id, item.menu_item_id, [{"ingredient_name": "X", "quantity": 40, "unit": "g"}])
    services.metric_store.record(tenant_id=tenant_id, metric_type="sale", amount_inr=100, menu_item_id=item.menu_item_id, quantity=1)

    r = client.get(f"/api/menu-recommendations?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    recs = r.json()["recommendations"]
    assert len(recs) == 1
    assert "caution line" in recs[0]["recommendation"]


def test_simulate_menu_price_route_recomputes_food_cost_and_margin(client, owner_session, services):
    headers, tenant_id = owner_session
    item = services.menu_store.create_item(tenant_id=tenant_id, name="Butter Chicken", price_inr=350)
    services.menu_store.set_recipe(tenant_id, item.menu_item_id, [{"ingredient_name": "Chicken", "quantity": 200, "unit": "g"}])
    services.purchase_store.record(tenant_id=tenant_id, ingredient_name="Chicken", quantity=1000, unit="g", amount_inr=500)

    r = client.post(
        f"/api/menu-engineering/simulate?tenant_id={tenant_id}", headers=headers,
        json={"menu_item_id": item.menu_item_id, "hypothetical_price_inr": 400},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe_cost_inr"] == 100.0
    assert body["current_price_inr"] == 350
    assert body["hypothetical_price_inr"] == 400


def test_simulate_menu_price_route_404s_for_a_missing_menu_item(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(
        f"/api/menu-engineering/simulate?tenant_id={tenant_id}", headers=headers,
        json={"menu_item_id": "does-not-exist", "hypothetical_price_inr": 100},
    )
    assert r.status_code == 404


def test_simulate_menu_price_route_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    item = services.menu_store.create_item(tenant_id=tenant_id, name="Lassi", price_inr=80)
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = client.post(
        f"/api/menu-engineering/simulate?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"},
        json={"menu_item_id": item.menu_item_id, "hypothetical_price_inr": 100},
    )
    assert r.status_code == 403


def test_digital_gm_briefing_route_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = client.get(f"/api/digital-gm-briefing?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403

    r2 = client.get(f"/api/digital-gm-briefing?tenant_id={tenant_id}", headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["lines"] == ["✅ All clear — nothing urgent right now."]
