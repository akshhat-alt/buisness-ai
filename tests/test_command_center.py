"""Tests for the Owner Command Center (Phase 7): GET /api/command-center,
a pure reorganization of business-health signals plus the automation
engine's own execution history — no new store, no LLM call on this path.
"""

from __future__ import annotations

import time

import pytest

from tests.test_admin_bot import OWNER_WA, _setup_tenant_with_owner_and_staff
from tests.test_automation import (
    _admin_headers,
    _backdate_task_due,
    _create_rule,
    _iso_hours_ago,
    _run_cron,
)
from tests.test_whatsapp import client_wa, services_wa

__all__ = ["client_wa", "services_wa"]


def test_command_center_requires_management_view_access(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    from business_ai.auth import Principal, create_access_token

    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services_wa.settings)
    r = client_wa.get(f"/api/command-center?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403, r.text

    r = client_wa.get(f"/api/command-center?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text


def test_command_center_surfaces_overdue_tasks_and_recommendation(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="Close register", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_id, task.task_id, _iso_hours_ago(5))

    r = client_wa.get(f"/api/command-center?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["needs_attention"]["overdue_tasks"] == 1
    assert any("Close register" in line for line in body["needs_attention"]["overdue_task_lines"])
    assert body["operational_problems"]["tasks_overdue"] == 1
    assert any("overdue" in action for action in body["recommended_actions"])
    assert "trends" in body
    assert body["automated_actions_taken"] == []


def test_command_center_surfaces_automated_actions_taken(client_wa, services_wa):
    """This is the direct Phase 6 -> Phase 7 link: an automation rule
    firing shows up in the command center as something already handled,
    without the owner having to separately check execution history."""
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="Restock shelf 3", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_id, task.task_id, _iso_hours_ago(5))
    rule = _create_rule(client_wa, headers, tenant_id, name="Notify on overdue", trigger_params={"hours": 2})
    _run_cron(client_wa, admin_headers)

    r = client_wa.get(f"/api/command-center?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["automated_actions_taken"]) == 1
    assert "Notify on overdue" in body["automated_actions_taken"][0]


def test_command_center_revenue_opportunities_reflect_confirmed_revenue_only(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(tenant_id=tenant_id, session_id="wa_919000055555", phone="919000055555", name="Kabir")
    services_wa.lead_store.mark_deposit_paid(tenant_id, lead.lead_id, amount_inr=1200)

    r = client_wa.get(f"/api/command-center?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revenue_opportunities"]["confirmed_revenue_inr"] == 1200


def test_command_center_is_tenant_isolated(client_wa, services_wa):
    headers_a, tenant_a, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    task = services_wa.task_store.create(
        tenant_id=tenant_a, title="Only tenant A's task", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_a, task.task_id, _iso_hours_ago(5))

    from tests.test_whatsapp import _signup

    headers_b, tenant_b = _signup(client_wa, business_name="Other Biz", email="cc-owner@example.com")
    r = client_wa.get(f"/api/command-center?tenant_id={tenant_b}", headers=headers_b)
    assert r.status_code == 200, r.text
    assert r.json()["needs_attention"]["overdue_tasks"] == 0

    r = client_wa.get(f"/api/command-center?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403, r.text
