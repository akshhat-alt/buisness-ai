"""Tests for Phase 17's proactive cron: POST /api/v1/admin/inventory-alert/run.
"""

from __future__ import annotations


def test_inventory_alert_requires_platform_admin(client, owner_session):
    headers, _ = owner_session
    r = client.post("/api/v1/admin/inventory-alert/run", headers=headers)
    assert r.status_code == 403


def test_inventory_alert_skips_tenant_with_nothing_low(client, owner_session, activate_tenant, admin_headers):
    headers, tenant_id = owner_session
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    activate_tenant(tenant_id)

    r = client.post("/api/v1/admin/inventory-alert/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert any(s["tenant_id"] == tenant_id and s["reason"] == "nothing below par level" for s in r.json()["skipped"])


def test_inventory_alert_notifies_and_dedups(client, owner_session, activate_tenant, services, admin_headers):
    headers, tenant_id = owner_session
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    activate_tenant(tenant_id)
    services.inventory_store.set_par_level(tenant_id, "Rice", par_level=10, unit="kg")
    services.inventory_store.adjust_quantity(tenant_id, "Rice", delta=2, unit="kg")

    r = client.post("/api/v1/admin/inventory-alert/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert tenant_id in r.json()["notified"]

    entries = services.audit_log.list_for_tenant(tenant_id, action="inventory_low_stock_flagged")
    assert len(entries) == 1
    assert "Rice" in entries[0].metadata["ingredient_name"]

    r2 = client.post("/api/v1/admin/inventory-alert/run", headers=admin_headers)
    assert tenant_id not in r2.json()["notified"]
    assert any(s["tenant_id"] == tenant_id and s["reason"] == "already flagged recently" for s in r2.json()["skipped"])


def test_inventory_alert_renotifies_a_genuinely_new_low_ingredient(client, owner_session, activate_tenant, services, admin_headers):
    headers, tenant_id = owner_session
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    activate_tenant(tenant_id)
    services.inventory_store.set_par_level(tenant_id, "Rice", par_level=10, unit="kg")
    services.inventory_store.adjust_quantity(tenant_id, "Rice", delta=2, unit="kg")
    client.post("/api/v1/admin/inventory-alert/run", headers=admin_headers)

    services.inventory_store.set_par_level(tenant_id, "Chicken", par_level=5, unit="kg")
    services.inventory_store.adjust_quantity(tenant_id, "Chicken", delta=1, unit="kg")

    r2 = client.post("/api/v1/admin/inventory-alert/run", headers=admin_headers)
    assert tenant_id in r2.json()["notified"]
    entries = services.audit_log.list_for_tenant(tenant_id, action="inventory_low_stock_flagged")
    assert len(entries) == 2  # Rice (from before) + Chicken (new)
