"""HTTP-level tests for Phase 23's shift-scheduling routes: RBAC
(owner+manager can manage, every roster member can view — same pairing
as ASSIGN_TASK/VIEW_TASKS), tenant isolation.
"""

from __future__ import annotations

from business_ai.auth import Principal, create_access_token


def _add_employee(client, headers, tenant_id, **overrides):
    body = {"name": "Ravi", "whatsapp_number": "919876500001", "role": "staff"}
    body.update(overrides)
    r = client.post(f"/api/employees?tenant_id={tenant_id}", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_owner_can_create_list_and_delete_a_shift(client, owner_session):
    headers, tenant_id = owner_session
    ravi = _add_employee(client, headers, tenant_id)

    r = client.post(
        f"/api/shifts?tenant_id={tenant_id}",
        json={"employee_id": ravi["employee_id"], "shift_date": "2026-09-20", "start_time": "09:00", "end_time": "17:00"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    shift = r.json()

    r2 = client.get(f"/api/shifts?tenant_id={tenant_id}", headers=headers)
    assert r2.status_code == 200, r2.text
    assert len(r2.json()["shifts"]) == 1

    r3 = client.delete(f"/api/shifts/{shift['shift_id']}?tenant_id={tenant_id}", headers=headers)
    assert r3.status_code == 200, r3.text
    assert client.get(f"/api/shifts?tenant_id={tenant_id}", headers=headers).json()["shifts"] == []


def test_manager_can_create_shifts_but_staff_cannot(client, owner_session, services):
    headers, tenant_id = owner_session
    ravi = _add_employee(client, headers, tenant_id)
    manager_token = create_access_token(Principal.manager("mgr_1", tenant_id), services.settings)
    manager_headers = {"Authorization": f"Bearer {manager_token}"}

    r = client.post(
        f"/api/shifts?tenant_id={tenant_id}",
        json={"employee_id": ravi["employee_id"], "shift_date": "2026-09-20", "start_time": "09:00", "end_time": "17:00"},
        headers=manager_headers,
    )
    assert r.status_code == 200, r.text

    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    staff_headers = {"Authorization": f"Bearer {staff_token}"}
    r2 = client.post(
        f"/api/shifts?tenant_id={tenant_id}",
        json={"employee_id": ravi["employee_id"], "shift_date": "2026-09-21", "start_time": "09:00", "end_time": "17:00"},
        headers=staff_headers,
    )
    assert r2.status_code == 403


def test_staff_can_view_shifts(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = client.get(f"/api/shifts?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 200, r.text


def test_create_shift_rejects_unknown_employee(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(
        f"/api/shifts?tenant_id={tenant_id}",
        json={"employee_id": "does-not-exist", "shift_date": "2026-09-20", "start_time": "09:00", "end_time": "17:00"},
        headers=headers,
    )
    assert r.status_code == 404


def test_list_shifts_filters_by_date(client, owner_session):
    headers, tenant_id = owner_session
    ravi = _add_employee(client, headers, tenant_id)
    client.post(
        f"/api/shifts?tenant_id={tenant_id}",
        json={"employee_id": ravi["employee_id"], "shift_date": "2026-09-20", "start_time": "09:00", "end_time": "17:00"},
        headers=headers,
    )
    client.post(
        f"/api/shifts?tenant_id={tenant_id}",
        json={"employee_id": ravi["employee_id"], "shift_date": "2026-09-21", "start_time": "09:00", "end_time": "17:00"},
        headers=headers,
    )
    r = client.get(f"/api/shifts?tenant_id={tenant_id}&shift_date=2026-09-20", headers=headers)
    assert len(r.json()["shifts"]) == 1


def test_delete_unknown_shift_returns_404(client, owner_session):
    headers, tenant_id = owner_session
    r = client.delete(f"/api/shifts/does-not-exist?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 404


def test_shifts_are_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "shifts-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r = client.get(f"/api/shifts?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403
