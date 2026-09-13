"""HTTP-level tests for Phase 22's Restaurant Profitability Intelligence
routes: GET /api/menu-engineering and GET /api/reorder-suggestions.
RBAC (owner+manager via VIEW_INVENTORY, same gate as every other menu/
inventory read), tenant isolation.
"""

from __future__ import annotations

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
