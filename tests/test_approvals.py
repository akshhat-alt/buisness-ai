"""Tests for Phase 21's dashboard-native task approval REST routes
(POST /api/tasks/{id}/approve and /reject — the WhatsApp "approve <id>"/
"reject <id>" commands' first REST equivalent) and the unified Approval
Inbox (GET /api/approvals) that aggregates them with pending Self-
Evolution proposals, per-item-type RBAC, never a blanket permission.
"""

from __future__ import annotations

from business_ai.auth import Principal, create_access_token
from tests.test_admin_bot import _setup_tenant_with_owner_and_staff
from tests.test_whatsapp import client_wa, services_wa

__all__ = ["client_wa", "services_wa"]


# ------------------------------------------------------------------ POST /api/tasks/{id}/approve|reject


def test_dashboard_can_approve_an_awaiting_approval_task(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_id}",
        json={"title": "Refund the customer", "assigned_to_employee_id": ravi["employee_id"], "approval_required": True},
        headers=headers,
    )
    task_id = r.json()["task_id"]
    services_wa.task_store.update_status(tenant_id, task_id, "awaiting_approval")

    r2 = client_wa.post(f"/api/tasks/{task_id}/approve?tenant_id={tenant_id}", headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "done"
    assert r2.json()["approved_by_employee_id"] is not None

    audit = services_wa.audit_log.list_for_tenant(tenant_id, action="task_approved")
    assert len(audit) == 1


def test_dashboard_can_reject_an_awaiting_approval_task(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_id}",
        json={"title": "Refund the customer", "assigned_to_employee_id": ravi["employee_id"], "approval_required": True},
        headers=headers,
    )
    task_id = r.json()["task_id"]
    services_wa.task_store.update_status(tenant_id, task_id, "awaiting_approval")

    r2 = client_wa.post(
        f"/api/tasks/{task_id}/reject?tenant_id={tenant_id}", json={"reason": "needs more detail"}, headers=headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "in_progress"  # rejected work goes back to in_progress, never cancelled
    assert r2.json()["block_reason"] == "needs more detail"

    audit = services_wa.audit_log.list_for_tenant(tenant_id, action="task_rejected")
    assert len(audit) == 1
    assert audit[0].metadata["reason"] == "needs more detail"


def test_approving_a_customer_facing_task_pings_the_customer(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000055555", phone="919000055555", message="refund please", source="whatsapp",
    )
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_id}",
        json={
            "title": "Process refund", "assigned_to_employee_id": ravi["employee_id"],
            "approval_required": True, "customer_facing_lead_id": lead.lead_id,
        },
        headers=headers,
    )
    task_id = r.json()["task_id"]
    services_wa.task_store.update_status(tenant_id, task_id, "awaiting_approval")

    r2 = client_wa.post(f"/api/tasks/{task_id}/approve?tenant_id={tenant_id}", headers=headers)
    assert r2.status_code == 200, r2.text
    ping = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == "919000055555"]
    assert len(ping) == 1


def test_task_approve_reject_require_owner_or_manager(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_id}",
        json={"title": "Refund", "assigned_to_employee_id": ravi["employee_id"], "approval_required": True},
        headers=headers,
    )
    task_id = r.json()["task_id"]
    services_wa.task_store.update_status(tenant_id, task_id, "awaiting_approval")

    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services_wa.settings)
    staff_headers = {"Authorization": f"Bearer {staff_token}"}
    r2 = client_wa.post(f"/api/tasks/{task_id}/approve?tenant_id={tenant_id}", headers=staff_headers)
    assert r2.status_code == 403
    r3 = client_wa.post(f"/api/tasks/{task_id}/reject?tenant_id={tenant_id}", json={}, headers=staff_headers)
    assert r3.status_code == 403


def test_approve_unknown_task_returns_404(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(f"/api/tasks/does-not-exist/approve?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 404


def test_task_approve_is_tenant_isolated(client_wa, services_wa):
    headers_a, tenant_a, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_a}",
        json={"title": "Refund", "assigned_to_employee_id": ravi["employee_id"], "approval_required": True},
        headers=headers_a,
    )
    task_id = r.json()["task_id"]

    signup_b = client_wa.post(
        "/api/auth/signup",
        json={"email": "approvals-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r2 = client_wa.post(f"/api/tasks/{task_id}/approve?tenant_id={tenant_a}", headers=headers_b)
    assert r2.status_code == 403


# ------------------------------------------------------------------ GET /api/approvals


def test_approval_inbox_lists_awaiting_approval_tasks(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_id}",
        json={"title": "Refund the customer", "assigned_to_employee_id": ravi["employee_id"], "approval_required": True},
        headers=headers,
    )
    task_id = r.json()["task_id"]
    services_wa.task_store.update_status(tenant_id, task_id, "awaiting_approval")

    r2 = client_wa.get(f"/api/approvals?tenant_id={tenant_id}", headers=headers)
    assert r2.status_code == 200, r2.text
    items = r2.json()["items"]
    assert len(items) == 1
    assert items[0]["type"] == "task"
    assert items[0]["id"] == task_id
    assert items[0]["title"] == "Refund the customer"


def test_approval_inbox_lists_pending_evolution_proposals(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    candidate = services_wa.evolution_versions.create(
        tenant_id=tenant_id, config_type="assistant_tone", payload={"tone_instructions": "Be warmer."},
        created_by="self_evolution_engine", rationale="test rationale",
    )
    services_wa.evolution_proposals.create(
        tenant_id=tenant_id, trigger_reason="elevated_dissatisfaction_rate",
        candidate_version_id=candidate.version_id, status="pending_owner_review",
    )

    r = client_wa.get(f"/api/approvals?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["type"] == "evolution_proposal"
    assert "test rationale" in items[0]["detail"]


def test_approval_inbox_aggregates_both_types_sorted_by_created_at(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_id}",
        json={"title": "Refund the customer", "assigned_to_employee_id": ravi["employee_id"], "approval_required": True},
        headers=headers,
    )
    task_id = r.json()["task_id"]
    services_wa.task_store.update_status(tenant_id, task_id, "awaiting_approval")

    candidate = services_wa.evolution_versions.create(
        tenant_id=tenant_id, config_type="assistant_tone", payload={"tone_instructions": "Be warmer."},
        created_by="self_evolution_engine",
    )
    services_wa.evolution_proposals.create(
        tenant_id=tenant_id, trigger_reason="elevated_dissatisfaction_rate",
        candidate_version_id=candidate.version_id, status="pending_owner_review",
    )

    r2 = client_wa.get(f"/api/approvals?tenant_id={tenant_id}", headers=headers)
    assert r2.status_code == 200, r2.text
    items = r2.json()["items"]
    assert {i["type"] for i in items} == {"task", "evolution_proposal"}


def test_approval_inbox_omits_evolution_section_for_a_manager_without_erroring(client_wa, services_wa):
    """MANAGE_EVOLUTION is owner-only — a manager's inbox should still
    succeed and show their pending tasks, just without an evolution
    section, never a blanket 403 for the whole call."""
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_id}",
        json={"title": "Refund the customer", "assigned_to_employee_id": ravi["employee_id"], "approval_required": True},
        headers=headers,
    )
    task_id = r.json()["task_id"]
    services_wa.task_store.update_status(tenant_id, task_id, "awaiting_approval")
    candidate = services_wa.evolution_versions.create(
        tenant_id=tenant_id, config_type="assistant_tone", payload={"tone_instructions": "Be warmer."},
        created_by="self_evolution_engine",
    )
    services_wa.evolution_proposals.create(
        tenant_id=tenant_id, trigger_reason="elevated_dissatisfaction_rate",
        candidate_version_id=candidate.version_id, status="pending_owner_review",
    )

    manager_token = create_access_token(Principal.manager("mgr_1", tenant_id), services_wa.settings)
    manager_headers = {"Authorization": f"Bearer {manager_token}"}
    r2 = client_wa.get(f"/api/approvals?tenant_id={tenant_id}", headers=manager_headers)
    assert r2.status_code == 200, r2.text
    items = r2.json()["items"]
    assert {i["type"] for i in items} == {"task"}


def test_approval_inbox_requires_a_real_tenant(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.get("/api/approvals?tenant_id=does-not-exist", headers=headers)
    assert r.status_code == 404


def test_approval_inbox_is_empty_when_nothing_pending(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.get(f"/api/approvals?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []
