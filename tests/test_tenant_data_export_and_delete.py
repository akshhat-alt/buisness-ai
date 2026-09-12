"""Tests for Phase 9's tenant data export/deletion — the compliance
baseline: GET /api/tenant/export and POST /api/tenant/delete."""

from __future__ import annotations

from business_ai.tenant import TenantNotFoundError
import pytest


def _add_some_data(services, client, headers, tenant_id):
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    services.lead_store.create(tenant_id=tenant_id, session_id="sess_1", phone="919876543210", name="Priya")
    services.employee_store.add(tenant_id=tenant_id, whatsapp_number="919999999999", name="Ravi", role="staff")


def test_export_includes_data_across_multiple_stores(client, owner_session, services):
    headers, tenant_id = owner_session
    _add_some_data(services, client, headers, tenant_id)

    r = client.get(f"/api/tenant/export?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["tenant"]["tenant_id"] == tenant_id
    assert len(data["leads"]) == 1
    assert len(data["employees"]) == 1
    assert len(data["knowledge_sources"]) == 1
    assert "analytics_summary" in data


def test_export_redacts_connection_secrets(client, owner_session, services):
    headers, tenant_id = owner_session
    client.put(
        f"/api/tenant?tenant_id={tenant_id}",
        json={"whatsapp_phone_number_id": "PNID_1", "whatsapp_access_token": "super-secret-token"},
        headers=headers,
    )
    r = client.get(f"/api/tenant/export?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    tenant_data = r.json()["tenant"]
    assert "whatsapp_access_token" not in tenant_data
    assert "razorpay_key_secret" not in tenant_data
    assert tenant_data["whatsapp_phone_number_id"] == "PNID_1"  # non-secret fields still present


def test_export_requires_owner_and_is_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "other-export@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}

    r = client.get(f"/api/tenant/export?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_delete_requires_exact_business_name_confirmation(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(f"/api/tenant/delete?tenant_id={tenant_id}", json={"confirm_business_name": "Wrong Name"}, headers=headers)
    assert r.status_code == 400
    assert "confirm_business_name" in r.json()["detail"]


def test_delete_removes_data_across_every_store(client, owner_session, services):
    headers, tenant_id = owner_session
    _add_some_data(services, client, headers, tenant_id)
    assert services.vector_store.count_for_tenant(tenant_id) >= 1

    r = client.post(
        f"/api/tenant/delete?tenant_id={tenant_id}", json={"confirm_business_name": "Priya Salon"}, headers=headers,
    )
    assert r.status_code == 200, r.text
    report = r.json()["rows_removed"]
    assert report["leads"] == 1
    assert report["employees"] == 1
    assert report["knowledge_sources"] == 1
    assert report["tenant_config"] == 1

    assert services.lead_store.list_for_tenant(tenant_id) == []
    assert services.employee_store.list_for_tenant(tenant_id, active_only=False) == []
    assert services.vector_store.count_for_tenant(tenant_id) == 0
    with pytest.raises(TenantNotFoundError):
        services.tenant_registry.get_config(tenant_id)


def test_delete_is_owner_gated_and_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "other-delete@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz 2"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}

    r = client.post(
        f"/api/tenant/delete?tenant_id={tenant_a}", json={"confirm_business_name": "Priya Salon"}, headers=headers_b,
    )
    assert r.status_code == 403
    # tenant_a must survive an attempted cross-tenant delete
    assert client.get(f"/api/tenant?tenant_id={tenant_a}", headers=headers_a).status_code == 200
