"""HTTP-level tests for Phase 10's Business Map API: RBAC, tenant
isolation, and real end-to-end data flow through GET /api/dependency/map
and GET /api/dependency/simulate.
"""

from __future__ import annotations

import pytest

from business_ai.auth import Principal, create_access_token


@pytest.fixture(autouse=True)
def _growth_plan(services, owner_session):
    # Business Map (VIEW_FEEDBACK) is Growth-tier under Phase 1's plan-gating.
    _, tenant_id = owner_session
    services.tenant_registry.update_config(tenant_id, plan="growth")


def test_dependency_map_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    staff_headers = {"Authorization": f"Bearer {staff_token}"}

    r = client.get(f"/api/dependency/map?tenant_id={tenant_id}", headers=staff_headers)
    assert r.status_code == 403

    r = client.get(f"/api/dependency/map?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text

    manager_token = create_access_token(Principal.manager("mgr_1", tenant_id), services.settings)
    manager_headers = {"Authorization": f"Bearer {manager_token}"}
    r = client.get(f"/api/dependency/map?tenant_id={tenant_id}", headers=manager_headers)
    assert r.status_code == 200, r.text


def test_dependency_map_is_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "dep-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}

    r = client.get(f"/api/dependency/map?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_dependency_map_reflects_real_task_history(client, owner_session, services):
    headers, tenant_id = owner_session
    ravi = services.employee_store.add(tenant_id=tenant_id, whatsapp_number="919876500001", name="Ravi")
    for _ in range(3):
        t = services.task_store.create(
            tenant_id=tenant_id, title="Restock shelf 3",
            assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id,
        )
        services.task_store.update_status(tenant_id, t.task_id, "done")

    r = client.get(f"/api/dependency/map?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["processes"]) == 1
    assert data["processes"][0]["bus_factor"] == 1
    assert len(data["single_point_of_failure_processes"]) == 1
    assert any(risk["type"] == "single_point_of_failure_process" for risk in data["risks"])


def test_dependency_simulate_for_real_employee(client, owner_session, services):
    headers, tenant_id = owner_session
    ravi = services.employee_store.add(tenant_id=tenant_id, whatsapp_number="919876500001", name="Ravi")
    services.task_store.create(
        tenant_id=tenant_id, title="Open job", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id,
    )

    r = client.get(f"/api/dependency/simulate?tenant_id={tenant_id}&employee_id={ravi.employee_id}", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["employee_name"] == "Ravi"
    assert len(data["orphaned_open_tasks"]) == 1
    assert data["is_currently_a_risk"] is True


def test_dependency_simulate_unknown_employee_returns_404(client, owner_session):
    headers, tenant_id = owner_session
    r = client.get(f"/api/dependency/simulate?tenant_id={tenant_id}&employee_id=emp_nope", headers=headers)
    assert r.status_code == 404


def test_dependency_simulate_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    ravi = services.employee_store.add(tenant_id=tenant_id, whatsapp_number="919876500001", name="Ravi")
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    staff_headers = {"Authorization": f"Bearer {staff_token}"}

    r = client.get(f"/api/dependency/simulate?tenant_id={tenant_id}&employee_id={ravi.employee_id}", headers=staff_headers)
    assert r.status_code == 403
