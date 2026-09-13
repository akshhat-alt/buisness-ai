"""Tests for the Automation Engine (Phase 6): owner-configured
trigger -> condition -> action rules, the cron entrypoint that evaluates
them, dedup/retry/give-up semantics, the owner kill switch, RBAC, and
tenant isolation.

Reuses test_whatsapp.py's fixtures (services_wa, client_wa) and
test_admin_bot.py's tenant-setup helper exactly like test_admin_bot.py
itself does — this is the same admin-bot roster/task/feedback substrate
the automation engine reads from, not a new fixture universe.
"""

from __future__ import annotations

import time

import pytest

from business_ai.app import Services, create_app
from tests.test_admin_bot import (
    OWNER_WA,
    RAVI_WA,
    _add_employee,
    _setup_tenant_with_owner_and_staff,
)
from tests.test_whatsapp import (
    _activate_with_whatsapp,
    _signup,
    client_wa,
    services_wa,
)

__all__ = ["client_wa", "services_wa"]  # re-exported fixtures, not unused imports


def _iso_hours_ago(hours: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))


def _backdate_task_due(services, tenant_id, task_id, due_at_iso):
    with services.task_store._db() as conn:
        conn.execute(
            "UPDATE tasks SET due_at = ? WHERE tenant_id = ? AND task_id = ?", (due_at_iso, tenant_id, task_id)
        )
        conn.commit()


def _backdate_feedback_created(services, tenant_id, feedback_id, created_at_iso):
    with services.feedback_store._db() as conn:
        conn.execute(
            "UPDATE feedback_items SET created_at = ? WHERE tenant_id = ? AND feedback_id = ?",
            (created_at_iso, tenant_id, feedback_id),
        )
        conn.commit()


def _set_lead_appointment_and_deposit_link(services, tenant_id, lead_id, appointment_at_iso):
    with services.lead_store._db() as conn:
        conn.execute(
            "UPDATE leads SET appointment_at = ?, deposit_link_sent_at = ? WHERE tenant_id = ? AND lead_id = ?",
            (appointment_at_iso, _iso_hours_ago(200), tenant_id, lead_id),
        )
        conn.commit()


def _create_rule(client, headers, tenant_id, **overrides):
    payload = {
        "name": "Notify on overdue tasks",
        "trigger_type": "task_overdue",
        "trigger_params": {"hours": 1},
        "action_type": "notify_owner",
        "action_params": {},
    }
    payload.update(overrides)
    r = client.post(f"/api/automation/rules?tenant_id={tenant_id}", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _run_cron(client, admin_headers):
    r = client.post("/api/v1/admin/automation/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()


def _admin_headers(client, services):
    r = client.post("/api/auth/login", json={"email": "admin", "password": services.settings.admin_secret})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ------------------------------------------------------------------ RBAC + CRUD


def test_owner_can_create_list_update_delete_rule(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    rule = _create_rule(client_wa, headers, tenant_id)
    assert rule["enabled"] is True
    assert rule["trigger_type"] == "task_overdue"

    r = client_wa.get(f"/api/automation/rules?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["rules"]) == 1

    r = client_wa.patch(
        f"/api/automation/rules/{rule['rule_id']}?tenant_id={tenant_id}", json={"enabled": False}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False

    r = client_wa.delete(f"/api/automation/rules/{rule['rule_id']}?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    r = client_wa.get(f"/api/automation/rules?tenant_id={tenant_id}", headers=headers)
    assert r.json()["rules"] == []


def test_staff_cannot_manage_or_view_automation(client_wa, services_wa):
    """Staff has no dashboard login in this codebase (WhatsApp-only
    identity) — a staff-role JWT is minted directly, same technique used
    for the manager RBAC test below, to exercise the real HTTP routes
    rather than calling authorize() in isolation."""
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _create_rule(client_wa, headers, tenant_id)

    from business_ai.auth import Principal, create_access_token

    staff_principal = Principal.staff("staff_x", tenant_id)
    token = create_access_token(staff_principal, services_wa.settings)
    staff_headers = {"Authorization": f"Bearer {token}"}

    r = client_wa.get(f"/api/automation/rules?tenant_id={tenant_id}", headers=staff_headers)
    assert r.status_code == 403, r.text
    r = client_wa.post(
        f"/api/automation/rules?tenant_id={tenant_id}",
        json={"name": "x", "trigger_type": "task_overdue", "action_type": "notify_owner"},
        headers=staff_headers,
    )
    assert r.status_code == 403, r.text


def test_manager_can_view_but_not_manage_automation_via_api(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _create_rule(client_wa, headers, tenant_id)

    from business_ai.auth import Principal, create_access_token

    manager_principal = Principal.manager("mgr_1", tenant_id)
    token = create_access_token(manager_principal, services_wa.settings)
    manager_headers = {"Authorization": f"Bearer {token}"}

    r = client_wa.get(f"/api/automation/rules?tenant_id={tenant_id}", headers=manager_headers)
    assert r.status_code == 200, r.text

    r = client_wa.post(
        f"/api/automation/rules?tenant_id={tenant_id}",
        json={"name": "x", "trigger_type": "task_overdue", "action_type": "notify_owner"},
        headers=manager_headers,
    )
    assert r.status_code == 403, r.text


def test_rules_are_tenant_isolated(client_wa, services_wa):
    headers_a, tenant_a, _ = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    headers_b, tenant_b = _signup(client_wa, business_name="Other Biz", email="other-owner@example.com")

    _create_rule(client_wa, headers_a, tenant_a)

    r = client_wa.get(f"/api/automation/rules?tenant_id={tenant_b}", headers=headers_b)
    assert r.status_code == 200, r.text
    assert r.json()["rules"] == []

    # Tenant B cannot see or act on tenant A's rules/tasks by guessing tenant_id.
    r = client_wa.get(f"/api/automation/rules?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403, r.text


def test_kill_switch_toggle_requires_owner_and_persists(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    r = client_wa.post(f"/api/automation/kill-switch?tenant_id={tenant_id}", json={"enabled": False}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["automation_enabled"] is False

    config = services_wa.tenant_registry.get_config(tenant_id)
    assert config.automation_enabled is False

    entries = services_wa.audit_log.list_for_tenant(tenant_id, action="automation_kill_switch")
    assert len(entries) == 1
    assert entries[0].metadata["state"] == "off"


# ------------------------------------------------------------------ trigger firing


def test_task_overdue_rule_notifies_owner_via_whatsapp(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="Restock shelf 3",
        assigned_to_employee_id=ravi["employee_id"], assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_id, task.task_id, _iso_hours_ago(5))
    _create_rule(client_wa, headers, tenant_id, trigger_params={"hours": 2})

    result = _run_cron(client_wa, admin_headers)
    assert tenant_id in result["processed"]
    assert len(result["processed"][tenant_id]["fired"]) == 1

    assert len(services_wa.fake_whatsapp_client.sent) == 1
    assert "Restock shelf 3" in services_wa.fake_whatsapp_client.sent[0]["body"]

    runs = services_wa.automation_run_store.list_for_tenant(tenant_id)
    assert len(runs) == 1
    assert runs[0].status == "success"

    audit = services_wa.audit_log.list_for_tenant(tenant_id, action="automation_action_executed")
    assert len(audit) == 1


def test_rule_does_not_refire_after_success(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="Overdue thing",
        assigned_to_employee_id=ravi["employee_id"], assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_id, task.task_id, _iso_hours_ago(5))
    _create_rule(client_wa, headers, tenant_id, trigger_params={"hours": 2})

    _run_cron(client_wa, admin_headers)
    _run_cron(client_wa, admin_headers)

    assert len(services_wa.fake_whatsapp_client.sent) == 1
    assert len(services_wa.automation_run_store.list_for_tenant(tenant_id)) == 1


def test_disabled_rule_never_fires(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="Overdue thing",
        assigned_to_employee_id=ravi["employee_id"], assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_id, task.task_id, _iso_hours_ago(5))
    rule = _create_rule(client_wa, headers, tenant_id, trigger_params={"hours": 2})
    client_wa.patch(f"/api/automation/rules/{rule['rule_id']}?tenant_id={tenant_id}", json={"enabled": False}, headers=headers)

    result = _run_cron(client_wa, admin_headers)
    assert tenant_id not in result["processed"]
    assert services_wa.fake_whatsapp_client.sent == []


def test_kill_switch_blocks_all_rules_for_tenant(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="Overdue thing",
        assigned_to_employee_id=ravi["employee_id"], assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_id, task.task_id, _iso_hours_ago(5))
    _create_rule(client_wa, headers, tenant_id, trigger_params={"hours": 2})
    client_wa.post(f"/api/automation/kill-switch?tenant_id={tenant_id}", json={"enabled": False}, headers=headers)

    result = _run_cron(client_wa, admin_headers)
    assert tenant_id not in result["processed"]
    assert any(s["tenant_id"] == tenant_id for s in result["skipped"])
    assert services_wa.fake_whatsapp_client.sent == []


def test_create_task_action_assigns_to_owner_by_default(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)
    owner_employee = services_wa.employee_store.find_by_whatsapp(tenant_id, OWNER_WA)

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="Handle this",
        assigned_to_employee_id=ravi["employee_id"], assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_id, task.task_id, _iso_hours_ago(5))
    before_count = len(services_wa.task_store.list_for_tenant(tenant_id))

    _create_rule(
        client_wa, headers, tenant_id, trigger_params={"hours": 2},
        action_type="create_task", action_params={"task_title": "Follow up: {title}"},
    )
    _run_cron(client_wa, admin_headers)

    tasks_after = services_wa.task_store.list_for_tenant(tenant_id)
    assert len(tasks_after) == before_count + 1
    new_task = [t for t in tasks_after if t.title.startswith("Follow up:")][0]
    assert new_task.assigned_to_employee_id == owner_employee.employee_id
    assert "Handle this" in new_task.title


def test_negative_feedback_unresolved_rule_fires(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    item = services_wa.feedback_store.record(
        tenant_id=tenant_id, employee_id=ravi["employee_id"], raw_text="The register keeps crashing",
        sentiment="negative", theme="tools_equipment", urgency="high",
    )
    _backdate_feedback_created(services_wa, tenant_id, item.feedback_id, _iso_hours_ago(5))
    _create_rule(
        client_wa, headers, tenant_id, name="Escalate stale complaints",
        trigger_type="negative_feedback_unresolved", trigger_params={"hours": 2},
    )

    result = _run_cron(client_wa, admin_headers)
    assert len(result["processed"][tenant_id]["fired"]) == 1
    assert len(services_wa.fake_whatsapp_client.sent) == 1


def test_recurring_feedback_theme_refires_only_on_growth(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    for _ in range(3):
        services_wa.feedback_store.record(
            tenant_id=tenant_id, employee_id=ravi["employee_id"], raw_text="POS software logs me out",
            sentiment="negative", theme="tools_equipment", urgency="medium",
        )
    _create_rule(
        client_wa, headers, tenant_id, name="Recurring issue alert",
        trigger_type="recurring_feedback_theme", trigger_params={"min_count": 3, "window_hours": 168},
    )

    _run_cron(client_wa, admin_headers)
    assert len(services_wa.fake_whatsapp_client.sent) == 1

    # Running again with no new reports must NOT refire.
    _run_cron(client_wa, admin_headers)
    assert len(services_wa.fake_whatsapp_client.sent) == 1

    # A 4th report grows the count -> allowed to refire.
    services_wa.feedback_store.record(
        tenant_id=tenant_id, employee_id=ravi["employee_id"], raw_text="POS software logs me out again",
        sentiment="negative", theme="tools_equipment", urgency="medium",
    )
    _run_cron(client_wa, admin_headers)
    assert len(services_wa.fake_whatsapp_client.sent) == 2


def test_deposit_unpaid_after_appointment_rule_fires(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    lead = services_wa.lead_store.create(tenant_id=tenant_id, session_id="wa_919000099999", phone="919000099999", name="Neha")
    _set_lead_appointment_and_deposit_link(services_wa, tenant_id, lead.lead_id, _iso_hours_ago(5))
    _create_rule(
        client_wa, headers, tenant_id, name="Chase unpaid deposits",
        trigger_type="deposit_unpaid_after_appointment", trigger_params={"hours": 2},
    )

    result = _run_cron(client_wa, admin_headers)
    assert len(result["processed"][tenant_id]["fired"]) == 1
    assert "Neha" in services_wa.fake_whatsapp_client.sent[0]["body"]

    # Confirming payment must stop it from firing again.
    services_wa.lead_store.mark_deposit_paid(tenant_id, lead.lead_id, amount_inr=500)
    services_wa.fake_whatsapp_client.sent.clear()
    result2 = _run_cron(client_wa, admin_headers)
    assert tenant_id not in result2["processed"]


def test_failed_action_retries_then_gives_up(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="Unreachable notify",
        assigned_to_employee_id=ravi["employee_id"], assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_id, task.task_id, _iso_hours_ago(5))
    _create_rule(client_wa, headers, tenant_id, trigger_params={"hours": 2})

    # Remove the tenant's WhatsApp credentials so _notify_management_whatsapp
    # can never reach anyone -> every attempt fails.
    services_wa.tenant_registry.update_config(tenant_id, whatsapp_phone_number_id="")

    from business_ai.automation import MAX_ATTEMPTS

    for _ in range(MAX_ATTEMPTS):
        _run_cron(client_wa, admin_headers)
    runs = services_wa.automation_run_store.list_for_tenant(tenant_id)
    assert all(r.status == "failed" for r in runs)
    assert len(runs) == MAX_ATTEMPTS

    # One more tick gives up rather than retrying forever.
    _run_cron(client_wa, admin_headers)
    runs = services_wa.automation_run_store.list_for_tenant(tenant_id)
    assert len(runs) == MAX_ATTEMPTS + 1
    assert sum(1 for r in runs if r.status == "given_up") == 1
    assert sum(1 for r in runs if r.status == "failed") == MAX_ATTEMPTS
    assert len(services_wa.fake_whatsapp_client.sent) == 0


def test_only_platform_admin_can_run_automation_cron(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post("/api/v1/admin/automation/run", headers=headers)
    assert r.status_code == 403, r.text


# ------------------------------------------------------------------ Phase 19: LOW_STOCK trigger


def _backdate_run_created_at(services, run_id, created_at_iso):
    with services.automation_run_store._db() as conn:
        conn.execute(
            "UPDATE automation_runs SET created_at = ? WHERE run_id = ?", (created_at_iso, run_id)
        )
        conn.commit()


def test_low_stock_rule_notifies_owner_via_whatsapp(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    services_wa.inventory_store.adjust_quantity(tenant_id, "Paneer", delta=500, unit="g")
    services_wa.inventory_store.set_par_level(tenant_id, "Paneer", par_level=1000, unit="g")
    _create_rule(
        client_wa, headers, tenant_id, name="Chase low stock",
        trigger_type="low_stock", trigger_params={},
    )

    result = _run_cron(client_wa, admin_headers)
    assert len(result["processed"][tenant_id]["fired"]) == 1
    body = services_wa.fake_whatsapp_client.sent[0]["body"]
    assert "Paneer" in body
    assert "low on stock" in body

    # Above par level -> no longer a candidate, no refire.
    services_wa.inventory_store.adjust_quantity(tenant_id, "Paneer", delta=600, unit="g")
    services_wa.fake_whatsapp_client.sent.clear()
    result2 = _run_cron(client_wa, admin_headers)
    assert tenant_id not in result2["processed"]


# ------------------------------------------------------------------ Phase 19: MESSAGE_LEAD action


def test_message_lead_action_sends_directly_to_the_customer(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000011111", phone="919000011111", name="Priya",
    )
    _set_lead_appointment_and_deposit_link(services_wa, tenant_id, lead.lead_id, _iso_hours_ago(5))
    _create_rule(
        client_wa, headers, tenant_id, name="Nudge unpaid deposits",
        trigger_type="deposit_unpaid_after_appointment", trigger_params={"hours": 2},
        action_type="message_lead", action_params={"message": "Hi {name}, just checking in on your deposit!"},
    )

    result = _run_cron(client_wa, admin_headers)
    assert len(result["processed"][tenant_id]["fired"]) == 1
    sent = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == "919000011111"]
    assert len(sent) == 1
    assert sent[0]["body"] == "Hi Priya, just checking in on your deposit!"


def test_message_lead_action_fails_gracefully_without_phone(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_no_phone", phone=None, email="nophone@example.com", name="No Phone",
    )
    _set_lead_appointment_and_deposit_link(services_wa, tenant_id, lead.lead_id, _iso_hours_ago(5))
    _create_rule(
        client_wa, headers, tenant_id, name="Nudge unpaid deposits",
        trigger_type="deposit_unpaid_after_appointment", trigger_params={"hours": 2},
        action_type="message_lead", action_params={"message": "Hi there!"},
    )

    result = _run_cron(client_wa, admin_headers)
    assert len(result["processed"][tenant_id]["failed"]) == 1
    runs = services_wa.automation_run_store.list_for_tenant(tenant_id)
    assert runs[0].status == "failed"
    assert "no phone" in runs[0].error.lower()


def test_message_lead_action_fails_gracefully_without_whatsapp_connected(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000022222", phone="919000022222", name="Neha",
    )
    _set_lead_appointment_and_deposit_link(services_wa, tenant_id, lead.lead_id, _iso_hours_ago(5))
    services_wa.tenant_registry.update_config(tenant_id, whatsapp_phone_number_id="")
    _create_rule(
        client_wa, headers, tenant_id, name="Nudge unpaid deposits",
        trigger_type="deposit_unpaid_after_appointment", trigger_params={"hours": 2},
        action_type="message_lead", action_params={"message": "Hi there!"},
    )

    result = _run_cron(client_wa, admin_headers)
    assert len(result["processed"][tenant_id]["failed"]) == 1
    runs = services_wa.automation_run_store.list_for_tenant(tenant_id)
    assert "whatsapp is not connected" in runs[0].error.lower()


def test_message_lead_action_rejects_a_non_lead_target(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="Overdue thing",
        assigned_to_employee_id=ravi["employee_id"], assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_id, task.task_id, _iso_hours_ago(5))
    _create_rule(
        client_wa, headers, tenant_id, trigger_params={"hours": 2},
        action_type="message_lead", action_params={"message": "Hi there!"},
    )

    result = _run_cron(client_wa, admin_headers)
    assert len(result["processed"][tenant_id]["failed"]) == 1
    runs = services_wa.automation_run_store.list_for_tenant(tenant_id)
    assert "requires a lead target" in runs[0].error.lower()


def test_creating_message_lead_rule_without_a_message_is_rejected(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(
        f"/api/automation/rules?tenant_id={tenant_id}",
        json={
            "name": "Broken rule", "trigger_type": "deposit_unpaid_after_appointment",
            "action_type": "message_lead", "action_params": {},
        },
        headers=headers,
    )
    assert r.status_code == 400, r.text


def test_removing_the_message_from_a_message_lead_rule_via_update_is_rejected(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    rule = _create_rule(
        client_wa, headers, tenant_id, name="Nudge unpaid deposits",
        trigger_type="deposit_unpaid_after_appointment",
        action_type="message_lead", action_params={"message": "Hi there!"},
    )
    r = client_wa.patch(
        f"/api/automation/rules/{rule['rule_id']}?tenant_id={tenant_id}",
        json={"action_params": {}}, headers=headers,
    )
    assert r.status_code == 400, r.text


# ------------------------------------------------------------------ Phase 19: escalate_after_hours


def test_escalate_after_hours_allows_a_generic_refire(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="Still not done",
        assigned_to_employee_id=ravi["employee_id"], assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_id, task.task_id, _iso_hours_ago(50))
    _create_rule(
        client_wa, headers, tenant_id, trigger_params={"hours": 2}, escalate_after_hours=24,
    )

    _run_cron(client_wa, admin_headers)
    assert len(services_wa.fake_whatsapp_client.sent) == 1

    # Immediately again: not due yet (0h since the last success < 24h).
    _run_cron(client_wa, admin_headers)
    assert len(services_wa.fake_whatsapp_client.sent) == 1

    # Backdate the one successful run 25h into the past -> due to escalate.
    run = services_wa.automation_run_store.list_for_tenant(tenant_id)[0]
    _backdate_run_created_at(services_wa, run.run_id, _iso_hours_ago(25))
    _run_cron(client_wa, admin_headers)
    assert len(services_wa.fake_whatsapp_client.sent) == 2
    assert "still unresolved" in services_wa.fake_whatsapp_client.sent[1]["body"].lower()


def test_without_escalate_after_hours_a_one_shot_trigger_never_refires(client_wa, services_wa):
    """Regression guard for the Phase 19 generalization: leaving
    escalate_after_hours unset (the default for every pre-Phase-19 rule)
    must keep the exact original one-shot-until-resolved behavior."""
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="Still not done",
        assigned_to_employee_id=ravi["employee_id"], assigned_by_employee_id=ravi["employee_id"],
    )
    _backdate_task_due(services_wa, tenant_id, task.task_id, _iso_hours_ago(50))
    _create_rule(client_wa, headers, tenant_id, trigger_params={"hours": 2})

    _run_cron(client_wa, admin_headers)
    run = services_wa.automation_run_store.list_for_tenant(tenant_id)[0]
    _backdate_run_created_at(services_wa, run.run_id, _iso_hours_ago(1000))
    _run_cron(client_wa, admin_headers)
    assert len(services_wa.fake_whatsapp_client.sent) == 1
