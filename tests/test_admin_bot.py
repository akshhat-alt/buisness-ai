"""Tests for the admin WhatsApp bot: employee roster identity, webhook
routing split (employee -> admin workflow, customer -> unchanged RAG
path), and the Phase 0 task command grammar (assign/start/done/blocked/
cancel/approve/reject/reassign, today/tasks/my tasks/overdue).

Reuses test_whatsapp.py's fixtures/helpers (services_wa, client_wa,
_signup, _activate_with_whatsapp, _wa_payload, _signed_post) rather than
duplicating the webhook-signing/payload-building machinery.
"""

from __future__ import annotations

from tests.test_whatsapp import (
    _activate_with_whatsapp,
    _signed_post,
    _signup,
    _wa_payload,
    client_wa,
    services_wa,
)

__all__ = ["client_wa", "services_wa"]  # re-exported fixtures, not unused imports

OWNER_WA = "919999888877"
RAVI_WA = "919876500001"
PRIYA_WA = "919876500002"


def _set_owner_number(client, headers, tenant_id, number=OWNER_WA):
    r = client.put(f"/api/tenant?tenant_id={tenant_id}", json={"owner_whatsapp_number": number}, headers=headers)
    assert r.status_code == 200, r.text


def _add_employee(client, headers, tenant_id, *, whatsapp_number, name, role="staff"):
    r = client.post(
        f"/api/employees?tenant_id={tenant_id}",
        json={"whatsapp_number": whatsapp_number, "name": name, "role": role},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _setup_tenant_with_owner_and_staff(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    _set_owner_number(client_wa, headers, tenant_id)
    # Normally lazy (triggered by the owner's first inbound message —
    # see EmployeeStore.ensure_owner_bootstrap), forced here so tests can
    # look up the owner's employee_id without needing a throwaway message.
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    ravi = _add_employee(client_wa, headers, tenant_id, whatsapp_number=RAVI_WA, name="Ravi", role="staff")
    return headers, tenant_id, ravi


def _send(client_wa, *, wa_id, text, message_id):
    payload = _wa_payload(phone_number_id="PNID_1", wa_id=wa_id, message_id=message_id, text=text)
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ employee roster API


def test_owner_can_add_and_list_employees(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    employee = _add_employee(client_wa, headers, tenant_id, whatsapp_number="+91 98765 00001", name="Ravi")
    assert employee["whatsapp_number"] == RAVI_WA  # normalized on write
    assert employee["role"] == "staff"

    listed = client_wa.get(f"/api/employees?tenant_id={tenant_id}", headers=headers).json()["employees"]
    assert len(listed) == 1
    assert listed[0]["name"] == "Ravi"


def test_staff_cannot_manage_employee_roster(client_wa, services_wa):
    headers, tenant_id, _ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    # Ravi has no dashboard login (WhatsApp-only identity) — simulate a
    # staff *login* principal instead, hitting the same route a real
    # staff dashboard account would.
    from business_ai.auth import create_access_token, Principal

    staff_token = create_access_token(Principal.staff("staff_1", tenant_id), services_wa.settings)
    staff_headers = {"Authorization": f"Bearer {staff_token}"}
    r = client_wa.post(
        f"/api/employees?tenant_id={tenant_id}",
        json={"whatsapp_number": PRIYA_WA, "name": "Priya", "role": "staff"},
        headers=staff_headers,
    )
    assert r.status_code == 403


def test_invalid_employee_role_rejected(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    r = client_wa.post(
        f"/api/employees?tenant_id={tenant_id}",
        json={"whatsapp_number": RAVI_WA, "name": "Ravi", "role": "platform_admin"},
        headers=headers,
    )
    assert r.status_code == 400


def test_deactivated_employee_no_longer_routes_to_admin_bot(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.put(
        f"/api/employees/{ravi['employee_id']}?tenant_id={tenant_id}", json={"active": False}, headers=headers
    )
    assert r.status_code == 200
    assert r.json()["active"] is False

    _send(client_wa, wa_id=RAVI_WA, text="what are your hours?", message_id="wamid.deact1")
    # Deactivated -> no longer in the active roster -> falls through to
    # the ordinary customer pipeline and gets captured as a lead.
    leads = client_wa.get(f"/api/leads?tenant_id={tenant_id}", headers=headers).json()["leads"]
    assert len(leads) == 1
    assert leads[0]["phone"] == RAVI_WA


def test_employee_roster_is_tenant_isolated(client_wa, services_wa):
    """The same WhatsApp number registered on tenant A's roster must
    never be recognized as an employee on tenant B's line."""
    headers_a, tenant_a = _signup(client_wa, business_name="Salon A", email="a@example.com")
    _activate_with_whatsapp(client_wa, headers_a, tenant_a, services_wa.settings.admin_secret, phone_number_id="PNID_A")
    _add_employee(client_wa, headers_a, tenant_a, whatsapp_number=RAVI_WA, name="Ravi")

    headers_b, tenant_b = _signup(client_wa, business_name="Salon B", email="b@example.com")
    _activate_with_whatsapp(client_wa, headers_b, tenant_b, services_wa.settings.admin_secret, phone_number_id="PNID_B")

    # Ravi's number messages Salon B's line — Salon B never registered
    # him, so this must be treated as an ordinary customer, not routed
    # into the admin bot.
    payload = _wa_payload(phone_number_id="PNID_B", wa_id=RAVI_WA, message_id="wamid.iso1", text="today")
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200, r.text
    leads_b = client_wa.get(f"/api/leads?tenant_id={tenant_b}", headers=headers_b).json()["leads"]
    assert len(leads_b) == 1
    assert leads_b[0]["phone"] == RAVI_WA


# ------------------------------------------------------------------ task command grammar


def test_owner_assigns_task_and_employee_gets_notified(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    _send(client_wa, wa_id=OWNER_WA, text="assign restock shelf 3 to Ravi by 2027-01-15T18:00", message_id="wamid.a1")

    sent = services_wa.fake_whatsapp_client.sent
    assert len(sent) == 2  # confirmation to owner + notification to Ravi
    assert sent[0]["to"] == OWNER_WA
    assert "created for Ravi" in sent[0]["body"]
    assert sent[1]["to"] == RAVI_WA
    assert "New task from" in sent[1]["body"]
    assert "restock shelf 3" in sent[1]["body"]

    tasks = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["assigned_to_employee_id"] == ravi["employee_id"]
    assert tasks[0]["due_at"] == "2027-01-15T12:30:00Z"  # IST -> UTC


def test_staff_cannot_assign_tasks(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _add_employee(client_wa, headers, tenant_id, whatsapp_number=PRIYA_WA, name="Priya", role="staff")

    _send(client_wa, wa_id=RAVI_WA, text="assign close register to Priya", message_id="wamid.a2")
    sent = services_wa.fake_whatsapp_client.sent
    assert len(sent) == 1
    assert "Only an owner or manager" in sent[0]["body"]
    assert client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"] == []


def test_employee_marks_own_task_done_notifies_assigner(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="assign restock shelf 3 to Ravi", message_id="wamid.b1")
    task_id_short = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]["task_id"][-6:]

    _send(client_wa, wa_id=RAVI_WA, text=f"done {task_id_short}", message_id="wamid.b2")

    sent = services_wa.fake_whatsapp_client.sent
    assert sent[-2]["to"] == RAVI_WA
    assert "Nice work" in sent[-2]["body"]
    assert sent[-1]["to"] == OWNER_WA
    assert "finished" in sent[-1]["body"]

    task = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]
    assert task["status"] == "done"


def test_employee_cannot_update_someone_elses_task(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _add_employee(client_wa, headers, tenant_id, whatsapp_number=PRIYA_WA, name="Priya", role="staff")
    _send(client_wa, wa_id=OWNER_WA, text="assign restock shelf 3 to Ravi", message_id="wamid.c1")
    task_id_short = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]["task_id"][-6:]

    _send(client_wa, wa_id=PRIYA_WA, text=f"done {task_id_short}", message_id="wamid.c2")
    sent = services_wa.fake_whatsapp_client.sent
    assert "only update the status of tasks assigned to you" in sent[-1]["body"].lower()
    task = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]
    assert task["status"] == "open"


def test_approval_required_flow(client_wa, services_wa):
    """Approval-required tasks aren't created via the WhatsApp keyword
    grammar (Phase 0 has no "require approval" clause), so this exercises
    the store + dispatcher directly the way a future NL-driven creation
    path would."""
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    owner_employees = client_wa.get(f"/api/employees?tenant_id={tenant_id}", headers=headers).json()["employees"]
    owner_employee_id = next(e["employee_id"] for e in owner_employees if e["role"] == "owner")

    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="refund customer #482", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=owner_employee_id, approval_required=True,
    )
    short_id = task.task_id[-6:]

    _send(client_wa, wa_id=RAVI_WA, text=f"done {short_id}", message_id="wamid.d1")
    task_after = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]
    assert task_after["status"] == "awaiting_approval"
    owner_notice = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA]
    assert any("reply \"approve" in m["body"] for m in owner_notice)

    _send(client_wa, wa_id=OWNER_WA, text=f"approve {short_id}", message_id="wamid.d2")
    task_final = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]
    assert task_final["status"] == "done"
    assert task_final["approved_by_employee_id"] == owner_employee_id


def test_reject_sends_task_back_to_in_progress(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    owner_employees = client_wa.get(f"/api/employees?tenant_id={tenant_id}", headers=headers).json()["employees"]
    owner_employee_id = next(e["employee_id"] for e in owner_employees if e["role"] == "owner")
    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="refund customer #482", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=owner_employee_id, approval_required=True,
    )
    short_id = task.task_id[-6:]
    services_wa.task_store.update_status(tenant_id, task.task_id, "awaiting_approval")

    _send(client_wa, wa_id=OWNER_WA, text=f"reject {short_id} needs the receipt", message_id="wamid.e1")
    task_after = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]
    assert task_after["status"] == "in_progress"
    assert task_after["block_reason"] == "needs the receipt"


def test_blocked_and_cancel_commands(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="assign restock shelf 3 to Ravi", message_id="wamid.f1")
    short_id = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]["task_id"][-6:]

    _send(client_wa, wa_id=RAVI_WA, text=f"blocked {short_id} waiting on delivery", message_id="wamid.f2")
    task = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]
    assert task["status"] == "blocked"
    assert task["block_reason"] == "waiting on delivery"

    _send(client_wa, wa_id=OWNER_WA, text=f"cancel {short_id}", message_id="wamid.f3")
    task = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]
    assert task["status"] == "cancelled"


def test_reassign_command(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    priya = _add_employee(client_wa, headers, tenant_id, whatsapp_number=PRIYA_WA, name="Priya", role="staff")
    _send(client_wa, wa_id=OWNER_WA, text="assign restock shelf 3 to Ravi", message_id="wamid.g1")
    short_id = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]["task_id"][-6:]

    _send(client_wa, wa_id=OWNER_WA, text=f"reassign {short_id} to Priya", message_id="wamid.g2")
    task = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]
    assert task["assigned_to_employee_id"] == priya["employee_id"]


def test_overdue_command_names_the_responsible_employee(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.task_store.create(
        tenant_id=tenant_id, title="restock shelf 3", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"], due_at="2020-01-01T00:00:00Z",
    )
    _send(client_wa, wa_id=OWNER_WA, text="overdue", message_id="wamid.h1")
    body = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "Ravi" in body
    assert "restock shelf 3" in body


def test_my_tasks_and_tasks_command_scope_by_role(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    priya = _add_employee(client_wa, headers, tenant_id, whatsapp_number=PRIYA_WA, name="Priya", role="staff")
    services_wa.task_store.create(
        tenant_id=tenant_id, title="task for Ravi", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"],
    )
    services_wa.task_store.create(
        tenant_id=tenant_id, title="task for Priya", assigned_to_employee_id=priya["employee_id"],
        assigned_by_employee_id=priya["employee_id"],
    )

    _send(client_wa, wa_id=RAVI_WA, text="my tasks", message_id="wamid.i1")
    ravi_view = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "task for Ravi" in ravi_view
    assert "task for Priya" not in ravi_view

    _send(client_wa, wa_id=OWNER_WA, text="tasks", message_id="wamid.i2")
    owner_view = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "task for Ravi" in owner_view
    assert "task for Priya" in owner_view


def test_help_on_unrecognized_command(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="blah blah nonsense", message_id="wamid.j1")
    assert "Commands:" in services_wa.fake_whatsapp_client.sent[-1]["body"]


def test_manager_can_assign_but_not_manage_employees_via_api(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    manager_wa = "919876500003"
    _add_employee(client_wa, headers, tenant_id, whatsapp_number=manager_wa, name="Meena", role="manager")

    _send(client_wa, wa_id=manager_wa, text="assign close register to Ravi", message_id="wamid.k1")
    tasks = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"]
    assert len(tasks) == 1  # manager CAN assign via the bot

    from business_ai.auth import create_access_token, Principal

    manager_token = create_access_token(Principal.manager("mgr_1", tenant_id), services_wa.settings)
    manager_headers = {"Authorization": f"Bearer {manager_token}"}
    r = client_wa.post(
        f"/api/employees?tenant_id={tenant_id}",
        json={"whatsapp_number": PRIYA_WA, "name": "Priya", "role": "staff"},
        headers=manager_headers,
    )
    assert r.status_code == 403  # but MANAGE_EMPLOYEES stays owner-only
