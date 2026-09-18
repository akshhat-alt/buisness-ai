"""Tests for Phase 1's owner/admin usage-and-plan HTTP surface:
GET /api/tenant/usage (owner), and the platform_admin-only
POST /api/v1/admin/tenants/{id}/set-plan and
GET /api/v1/admin/tenants/{id}/usage."""

from __future__ import annotations

from business_ai.auth import Principal, create_access_token


def test_owner_can_view_own_usage_and_plan(client, owner_session, services):
    headers, tenant_id = owner_session
    services.usage_meter_store.increment(tenant_id=tenant_id, metric="ai_messages", by=3)

    r = client.get(f"/api/tenant/usage?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["plan"] == "starter"
    assert data["usage"]["ai_messages"] == 3
    assert "period" in data


def test_staff_can_view_own_usage(client, owner_session, services):
    # VIEW_ANALYTICS is available to staff too (same tier as GET /api/tenant).
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = client.get(f"/api/tenant/usage?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 200, r.text


def test_usage_route_is_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "usage-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r = client.get(f"/api/tenant/usage?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_admin_can_set_plan(client, owner_session, admin_headers, services):
    headers, tenant_id = owner_session
    assert services.tenant_registry.get_config(tenant_id).plan == "starter"

    r = client.post(f"/api/v1/admin/tenants/{tenant_id}/set-plan", json={"plan": "growth"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "growth"
    assert services.tenant_registry.get_config(tenant_id).plan == "growth"


def test_set_plan_rejects_invalid_plan_value(client, owner_session, admin_headers):
    headers, tenant_id = owner_session
    r = client.post(f"/api/v1/admin/tenants/{tenant_id}/set-plan", json={"plan": "bogus"}, headers=admin_headers)
    assert r.status_code == 422


def test_set_plan_requires_platform_admin(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(f"/api/v1/admin/tenants/{tenant_id}/set-plan", json={"plan": "growth"}, headers=headers)
    assert r.status_code == 403


def test_owner_cannot_self_upgrade_via_tenant_config_update(client, owner_session):
    """PUT /api/tenant (TenantConfigUpdate, owner self-service) must have
    no way to change plan — SetPlanRequest is a deliberately separate,
    admin-only schema/route precisely so an owner can't grant themselves
    a paid plan's features for free."""
    headers, tenant_id = owner_session
    r = client.put(f"/api/tenant?tenant_id={tenant_id}", json={"plan": "scale"}, headers=headers)
    assert r.status_code == 200, r.text  # unknown/extra field is just ignored, not an error
    assert r.json()["plan"] == "starter"  # unchanged


def test_admin_can_view_any_tenants_usage(client, owner_session, admin_headers, services):
    headers, tenant_id = owner_session
    services.usage_meter_store.increment(tenant_id=tenant_id, metric="whatsapp_messages", by=2)

    r = client.get(f"/api/v1/admin/tenants/{tenant_id}/usage", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["plan"] == "starter"
    assert data["usage"]["whatsapp_messages"] == 2


def test_admin_usage_route_requires_platform_admin(client, owner_session):
    headers, tenant_id = owner_session
    r = client.get(f"/api/v1/admin/tenants/{tenant_id}/usage", headers=headers)
    assert r.status_code == 403


def test_admin_set_plan_404s_for_unknown_tenant(client, admin_headers):
    r = client.post("/api/v1/admin/tenants/does-not-exist/set-plan", json={"plan": "growth"}, headers=admin_headers)
    assert r.status_code == 404
