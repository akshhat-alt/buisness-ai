"""Tests for the admin WhatsApp bot: employee roster identity, webhook
routing split (employee -> admin workflow, customer -> unchanged RAG
path), and the Phase 0 task command grammar (assign/start/done/blocked/
cancel/approve/reject/reassign, today/tasks/my tasks/overdue).

Reuses test_whatsapp.py's fixtures/helpers (services_wa, client_wa,
_signup, _activate_with_whatsapp, _wa_payload, _signed_post) rather than
duplicating the webhook-signing/payload-building machinery.
"""

from __future__ import annotations

import dataclasses
import time

import pytest

from business_ai.app import Services, create_app
from business_ai.generation import EmployeeCommandIntent, FeedbackClassification
from business_ai.retrieval import HashEmbeddingProvider
from tests.conftest import FakeEmailSender, FakeGenerator
from tests.test_whatsapp import (
    APP_SECRET,
    VERIFY_TOKEN,
    FakeWhatsAppClient,
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


@pytest.fixture()
def services_full(tmp_path, settings):
    """Same as test_whatsapp.py's services_wa, but with digest email ALSO
    configured — needed for tests that exercise the digest/action-brief/
    scorecard content across both channels at once."""
    settings_full = dataclasses.replace(
        settings, whatsapp_app_secret=APP_SECRET, whatsapp_verify_token=VERIFY_TOKEN,
        resend_api_key="re_test_key", digest_from_email="digest@business-ai.example",
        public_base_url="https://app.example.com",
    )
    svc = Services(settings_full, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    fake_wa = FakeWhatsAppClient()
    svc.whatsapp_client = lambda: fake_wa
    svc.fake_whatsapp_client = fake_wa
    fake_email = FakeEmailSender()
    svc.email_sender = lambda: fake_email
    svc.fake_email_sender = fake_email
    yield svc
    svc.vector_store.close()


@pytest.fixture()
def client_full(services_full):
    from fastapi.testclient import TestClient

    return TestClient(create_app(services_full))


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


# ------------------------------------------------------------------ feedback capture + theme classification


def test_employee_feedback_is_classified_and_stored(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    _send(
        client_wa, wa_id=RAVI_WA, text="feedback the checkout software keeps logging me out mid-sale",
        message_id="wamid.fb1",
    )
    ack = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "logged" in ack.lower()

    items = client_wa.get(f"/api/feedback?tenant_id={tenant_id}", headers=headers).json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["raw_text"] == "the checkout software keeps logging me out mid-sale"
    assert item["employee_id"] == ravi["employee_id"]
    assert item["source_message_id"] == "wamid.fb1"
    # FakeGenerator's deterministic default classification (conftest.py):
    assert item["sentiment"] == "negative"
    assert item["theme"] == "software_or_tools"
    assert item["urgency"] == "medium"
    assert item["resolved"] is False


def test_any_role_can_submit_feedback(client_wa, services_wa):
    """Submitting feedback needs no permission check — every employee in
    the roster can report a concern; only VIEWING the aggregate is gated."""
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="feedback not enough gloves in stock", message_id="wamid.fb2")
    items = client_wa.get(f"/api/feedback?tenant_id={tenant_id}", headers=headers).json()["items"]
    assert len(items) == 1


def test_staff_cannot_view_feedback_themes_over_whatsapp_or_api(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="feedback the printer is broken again", message_id="wamid.fb3")

    _send(client_wa, wa_id=RAVI_WA, text="feedback themes", message_id="wamid.fb4")
    assert "only an owner or manager" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()

    from business_ai.auth import create_access_token, Principal

    staff_token = create_access_token(Principal.staff("staff_2", tenant_id), services_wa.settings)
    r = client_wa.get(f"/api/feedback?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403


def test_owner_can_view_feedback_themes_over_whatsapp(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="feedback the printer is broken again", message_id="wamid.fb5")
    _send(client_wa, wa_id=OWNER_WA, text="feedback themes", message_id="wamid.fb6")
    body = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "Software/tools" in body
    assert "1 report" in body


def test_feedback_is_tenant_isolated(client_wa, services_wa):
    headers_a, tenant_a = _signup(client_wa, business_name="Salon A", email="fa@example.com")
    _activate_with_whatsapp(client_wa, headers_a, tenant_a, services_wa.settings.admin_secret, phone_number_id="PNID_FA")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_a, OWNER_WA)
    services_wa.employee_store.add(tenant_id=tenant_a, whatsapp_number=RAVI_WA, name="Ravi", role="staff")

    headers_b, tenant_b = _signup(client_wa, business_name="Salon B", email="fb@example.com")
    _activate_with_whatsapp(client_wa, headers_b, tenant_b, services_wa.settings.admin_secret, phone_number_id="PNID_FB")

    payload = _wa_payload(phone_number_id="PNID_FA", wa_id=RAVI_WA, message_id="wamid.fb7", text="feedback broken chair")
    r = _signed_post(client_wa, payload)
    assert r.status_code == 200, r.text

    items_a = client_wa.get(f"/api/feedback?tenant_id={tenant_a}", headers=headers_a).json()["items"]
    items_b = client_wa.get(f"/api/feedback?tenant_id={tenant_b}", headers=headers_b).json()["items"]
    assert len(items_a) == 1
    assert items_b == []


def test_resolve_feedback(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="feedback the AC is too loud", message_id="wamid.fb8")
    feedback_id = client_wa.get(f"/api/feedback?tenant_id={tenant_id}", headers=headers).json()["items"][0]["feedback_id"]

    r = client_wa.post(f"/api/feedback/{feedback_id}/resolve?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["resolved"] is True


# ------------------------------------------------------------------ instant urgent-feedback alert


def test_high_urgency_feedback_triggers_instant_alert(client_full, services_full):
    services_full.generator = lambda: FakeGenerator(
        feedback_classifications={
            "the walk-in freezer is broken and stock is spoiling": FeedbackClassification(
                sentiment="negative", theme="equipment_or_supplies", urgency="high",
                root_cause_hint="Freezer failure.", suggested_action="Call a repair technician immediately.",
            )
        }
    )
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_full, services_full)
    services_full.fake_email_sender.sent.clear()  # drop the activation email from setup

    payload = _wa_payload(
        phone_number_id="PNID_1", wa_id=RAVI_WA, message_id="wamid.urgent1",
        text="feedback the walk-in freezer is broken and stock is spoiling",
    )
    r = _signed_post(client_full, payload)
    assert r.status_code == 200, r.text

    ack = services_full.fake_whatsapp_client.sent[0]["body"]
    assert "urgent" in ack.lower()

    management_alerts = [m for m in services_full.fake_whatsapp_client.sent if m["to"] == OWNER_WA]
    assert any("🚨" in m["body"] and "freezer" in m["body"].lower() for m in management_alerts)

    assert len(services_full.fake_email_sender.sent) == 1
    assert "equipment_or_supplies" not in services_full.fake_email_sender.sent[0]["subject"]  # human label, not the raw key
    assert "Needs attention" in services_full.fake_email_sender.sent[0]["subject"]


def test_low_urgency_feedback_does_not_trigger_instant_alert(client_wa, services_wa):
    """Default FakeGenerator classification is urgency="medium" — should
    log normally with no instant alert."""
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="feedback could we get a better mop", message_id="wamid.calm1")
    sent_to_owner = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA]
    assert sent_to_owner == []


# ------------------------------------------------------------------ digest: recurring issues + owner insights


class SpyGenerator(FakeGenerator):
    """Records the kwargs generate_action_brief was called with, so a
    test can assert the digest actually fed it the new task/feedback
    signals rather than just checking the FakeGenerator's fixed output."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.action_brief_calls: list[dict] = []

    def generate_action_brief(self, **kwargs):
        self.action_brief_calls.append(kwargs)
        return super().generate_action_brief(**kwargs)


def test_digest_includes_overdue_tasks_and_recurring_feedback(client_full, services_full):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_full, services_full)
    spy = SpyGenerator()
    services_full.generator = lambda: spy

    services_full.task_store.create(
        tenant_id=tenant_id, title="restock shelf 3", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"], due_at="2020-01-01T00:00:00Z",
    )
    for i in range(3):
        services_full.feedback_store.record(
            tenant_id=tenant_id, employee_id=ravi["employee_id"], raw_text=f"software issue {i}",
            sentiment="negative", theme="software_or_tools", urgency="low",
        )
    services_full.fake_email_sender.sent.clear()  # drop the activation email from setup

    admin_login = client_full.post("/api/auth/login", json={"email": "admin", "password": services_full.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_full.post("/api/v1/admin/digest/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert tenant_id in r.json()["sent"]

    assert len(spy.action_brief_calls) == 1
    call = spy.action_brief_calls[0]
    assert call["overdue_task_lines"] and "Ravi" in call["overdue_task_lines"][0]
    assert call["recurring_feedback_lines"] and "3 report" in call["recurring_feedback_lines"][0]

    email_html = services_full.fake_email_sender.sent[0]["html_body"]
    assert "Ravi" in email_html and "restock shelf 3" in email_html
    assert "Software/tools" in email_html

    whatsapp_body = [m for m in services_full.fake_whatsapp_client.sent if m["to"] == OWNER_WA][0]["body"]
    assert "Overdue" in whatsapp_body
    assert "Recurring feedback" in whatsapp_body


def test_digest_reaches_every_owner_and_manager_on_roster(client_full, services_full):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_full, services_full)
    manager_wa = "919876500009"
    _add_employee(client_full, headers, tenant_id, whatsapp_number=manager_wa, name="Meena", role="manager")
    services_full.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="919000000001", message="hi")

    admin_login = client_full.post("/api/auth/login", json={"email": "admin", "password": services_full.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_full.post("/api/v1/admin/digest/run", headers=admin_headers)
    assert r.status_code == 200, r.text

    recipients = {m["to"] for m in services_full.fake_whatsapp_client.sent}
    assert recipients == {OWNER_WA, manager_wa}


def test_whatsapp_template_fallback_when_window_closed(client_full, services_full):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_full, services_full)
    client_full.put(
        f"/api/tenant?tenant_id={tenant_id}", json={"admin_notify_template_name": "daily_update_v1"}, headers=headers,
    )
    services_full.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="919000000001", message="hi")
    services_full.fake_whatsapp_client.raise_reengagement = True

    admin_login = client_full.post("/api/auth/login", json={"email": "admin", "password": services_full.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_full.post("/api/v1/admin/digest/run", headers=admin_headers)
    assert r.status_code == 200, r.text

    assert services_full.fake_whatsapp_client.sent == []  # free-text send failed (window closed)
    assert len(services_full.fake_whatsapp_client.template_sent) == 1
    assert services_full.fake_whatsapp_client.template_sent[0]["template_name"] == "daily_update_v1"
    assert services_full.fake_whatsapp_client.template_sent[0]["to"] == OWNER_WA


def test_no_template_fallback_when_none_configured(client_full, services_full):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_full, services_full)
    services_full.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="919000000001", message="hi")
    services_full.fake_whatsapp_client.raise_reengagement = True
    services_full.fake_email_sender.sent.clear()  # drop the activation email from setup

    admin_login = client_full.post("/api/auth/login", json={"email": "admin", "password": services_full.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    r = client_full.post("/api/v1/admin/digest/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert services_full.fake_whatsapp_client.sent == []
    assert services_full.fake_whatsapp_client.template_sent == []  # no template configured -> no fallback attempt
    assert len(services_full.fake_email_sender.sent) == 1  # email is still the guaranteed channel


# ------------------------------------------------------------------ scorecard / business health


def test_scorecard_command_shows_task_and_feedback_snapshot(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    done_task = services_wa.task_store.create(
        tenant_id=tenant_id, title="close register", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"],
    )
    services_wa.task_store.update_status(tenant_id, done_task.task_id, "done")
    services_wa.task_store.create(
        tenant_id=tenant_id, title="restock shelf 3", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"], due_at="2020-01-01T00:00:00Z",
    )

    _send(client_wa, wa_id=OWNER_WA, text="scorecard", message_id="wamid.sc1")
    body = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "1/2 completed" in body
    assert "1 overdue" in body
    assert "Ravi" in body


def test_scorecard_requires_owner_or_manager(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="scorecard", message_id="wamid.sc2")
    assert "only an owner or manager" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()


def test_scorecard_shows_sop_coverage_of_recurring_themes(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    for i in range(3):
        services_wa.feedback_store.record(
            tenant_id=tenant_id, employee_id=ravi["employee_id"], raw_text=f"software issue {i}",
            sentiment="negative", theme="software_or_tools", urgency="low",
        )
    _send(client_wa, wa_id=OWNER_WA, text="scorecard", message_id="wamid.sc3")
    assert "(0/1 have approved guidance)" in services_wa.fake_whatsapp_client.sent[-1]["body"]

    services_wa.sop_store.approve(
        tenant_id=tenant_id, theme="software_or_tools", text="restart daily", approved_by_employee_id="emp_1"
    )
    _send(client_wa, wa_id=OWNER_WA, text="scorecard", message_id="wamid.sc4")
    assert "(1/1 have approved guidance)" in services_wa.fake_whatsapp_client.sent[-1]["body"]


def test_business_health_api_matches_scorecard_data_and_is_rbac_gated(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.task_store.create(
        tenant_id=tenant_id, title="restock shelf 3", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"], due_at="2020-01-01T00:00:00Z",
    )

    r = client_wa.get(f"/api/business-health?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["tasks_overdue"] == 1
    assert "Ravi" in data["overdue_lines"][0]

    from business_ai.auth import Principal, create_access_token

    staff_token = create_access_token(Principal.staff("staff_3", tenant_id), services_wa.settings)
    r = client_wa.get(f"/api/business-health?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403


# ------------------------------------------------------------------ SOP / action conversion


def test_owner_approves_sop_and_it_surfaces_on_next_matching_feedback(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    _send(
        client_wa, wa_id=OWNER_WA,
        text="approve sop software/tools: re-login every 2 hours until the vendor fixes it",
        message_id="wamid.sop1",
    )
    ack = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "Saved guidance" in ack

    # A later feedback report on the SAME theme (FakeGenerator's default
    # classification is software_or_tools) now gets the guidance echoed back.
    _send(client_wa, wa_id=RAVI_WA, text="feedback the register software crashed again", message_id="wamid.sop2")
    ack2 = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "We know about this" in ack2
    assert "re-login every 2 hours" in ack2


def test_manager_cannot_approve_sop(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    manager_wa = "919876500011"
    _add_employee(client_wa, headers, tenant_id, whatsapp_number=manager_wa, name="Meena", role="manager")

    _send(client_wa, wa_id=manager_wa, text="approve sop software/tools: workaround text", message_id="wamid.sop3")
    assert "only the owner" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()
    assert services_wa.sop_store.list_for_tenant(tenant_id) == []


def test_unknown_theme_reference_rejected(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="approve sop nonsense theme: some text", message_id="wamid.sop4")
    assert "couldn't match" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()


def test_sop_api_rbac_and_content(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(
        f"/api/sops?tenant_id={tenant_id}", json={"theme": "software_or_tools", "text": "restart the router"}, headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["theme"] == "software_or_tools"

    listed = client_wa.get(f"/api/sops?tenant_id={tenant_id}", headers=headers).json()["sops"]
    assert len(listed) == 1

    from business_ai.auth import Principal, create_access_token

    manager_token = create_access_token(Principal.manager("mgr_2", tenant_id), services_wa.settings)
    r = client_wa.post(
        f"/api/sops?tenant_id={tenant_id}", json={"theme": "software_or_tools", "text": "x"},
        headers={"Authorization": f"Bearer {manager_token}"},
    )
    assert r.status_code == 403  # MANAGE_SOPS is owner-only

    r = client_wa.get(f"/api/sops?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {manager_token}"})
    assert r.status_code == 200  # VIEW_FEEDBACK covers manager read access


def test_feedback_theme_command_shows_sop_status(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="feedback the mop is falling apart", message_id="wamid.sop5")
    _send(client_wa, wa_id=OWNER_WA, text="approve sop software/tools: ordered a replacement", message_id="wamid.sop6")
    _send(client_wa, wa_id=OWNER_WA, text="feedback themes", message_id="wamid.sop7")
    body = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "has approved guidance" in body


# ------------------------------------------------------------------ proactive task-escalation alert


def _admin_headers(client, services):
    r = client.post("/api/auth/login", json={"email": "admin", "password": services.settings.admin_secret})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ------------------------------------------------------------------ admin onboarding-progress view (Phase 8)


def test_admin_tenant_list_includes_onboarding_progress(client_wa, services_wa):
    """The admin panel's Phase 8 addition: enough per-tenant signal to
    see where a stuck self-serve signup actually is, without opening
    their dashboard — reusing existing stores, no new state."""
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    r = client_wa.get("/api/v1/admin/tenants", headers=admin_headers)
    assert r.status_code == 200, r.text
    row = next(t for t in r.json()["tenants"] if t["tenant_id"] == tenant_id)
    onboarding = row["onboarding"]
    assert onboarding["whatsapp_connected"] is True  # _activate_with_whatsapp already connected PNID_1
    assert onboarding["employees"] == 2  # owner (bootstrapped) + Ravi
    assert onboarding["knowledge_sources"] == 1  # _activate_with_whatsapp ingests example.com
    assert onboarding["automation_rules"] == 0

    services_wa.automation_rule_store.create(
        tenant_id=tenant_id, name="Notify on overdue", trigger_type="task_overdue", trigger_params={"hours": 24},
        action_type="notify_owner", action_params={},
    )
    r2 = client_wa.get("/api/v1/admin/tenants", headers=admin_headers)
    row2 = next(t for t in r2.json()["tenants"] if t["tenant_id"] == tenant_id)
    assert row2["onboarding"]["automation_rules"] == 1


def test_admin_tenant_list_requires_platform_admin(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.get("/api/v1/admin/tenants", headers=headers)
    assert r.status_code == 403


def test_task_escalation_alerts_and_dedupes(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.task_store.create(
        tenant_id=tenant_id, title="deep clean", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"], due_at="2020-01-01T00:00:00Z",  # far overdue
    )
    services_wa.task_store.create(
        tenant_id=tenant_id, title="restock shelf 3", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"],
        due_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600)),  # overdue, but only 1h — not escalation-worthy
    )
    admin_headers = _admin_headers(client_wa, services_wa)

    r = client_wa.post("/api/v1/admin/task-escalation/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert tenant_id in r.json()["escalated"]

    body = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "deep clean" in body
    assert "restock shelf 3" not in body  # not overdue long enough yet

    # Second run: the same task shouldn't re-escalate (reminder_sent_at marker).
    services_wa.fake_whatsapp_client.sent.clear()
    r2 = client_wa.post("/api/v1/admin/task-escalation/run", headers=admin_headers)
    assert r2.status_code == 200
    assert tenant_id in [s["tenant_id"] for s in r2.json()["skipped"]]
    assert services_wa.fake_whatsapp_client.sent == []


def test_task_escalation_requires_platform_admin(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post("/api/v1/admin/task-escalation/run", headers=headers)
    assert r.status_code == 403


# ------------------------------------------------------------------ verified customer outcomes


def test_completing_customer_facing_task_pings_the_customer(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000099999", phone="919000099999",
        message="my order never arrived", source="whatsapp",
    )
    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="follow up on missing order", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"], customer_facing_lead_id=lead.lead_id,
    )
    short_id = task.task_id[-6:]

    _send(client_wa, wa_id=RAVI_WA, text=f"done {short_id}", message_id="wamid.vo1")

    ping = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == "919000099999"]
    assert len(ping) == 1
    assert "resolved to your satisfaction" in ping[0]["body"]


def test_completing_non_customer_task_does_not_ping_anyone_unexpected(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="restock shelf 3", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=ravi["employee_id"],
    )
    short_id = task.task_id[-6:]
    _send(client_wa, wa_id=RAVI_WA, text=f"done {short_id}", message_id="wamid.vo2")
    # Only the ack to Ravi — no customer, no unexpected recipient.
    assert {m["to"] for m in services_wa.fake_whatsapp_client.sent} == {RAVI_WA}


def test_approving_customer_facing_task_also_pings_the_customer(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    owner_employee_id = next(e["employee_id"] for e in client_wa.get(f"/api/employees?tenant_id={tenant_id}", headers=headers).json()["employees"] if e["role"] == "owner")
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000088888", phone="919000088888", message="refund please", source="whatsapp",
    )
    task = services_wa.task_store.create(
        tenant_id=tenant_id, title="process refund", assigned_to_employee_id=ravi["employee_id"],
        assigned_by_employee_id=owner_employee_id, approval_required=True, customer_facing_lead_id=lead.lead_id,
    )
    short_id = task.task_id[-6:]
    services_wa.task_store.update_status(tenant_id, task.task_id, "awaiting_approval")

    _send(client_wa, wa_id=OWNER_WA, text=f"approve {short_id}", message_id="wamid.vo3")
    ping = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == "919000088888"]
    assert len(ping) == 1


def test_assign_command_links_task_to_lead(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000077777", phone="919000077777", message="broken item", source="whatsapp",
    )
    lead_short = lead.lead_id[-6:]
    _send(client_wa, wa_id=OWNER_WA, text=f"assign call back the customer to Ravi for lead {lead_short}", message_id="wamid.vo4")
    tasks = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"]
    assert tasks[0]["customer_facing_lead_id"] == lead.lead_id


def test_assign_command_rejects_unknown_lead_reference(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="assign call back the customer to Ravi for lead zzzzzz", message_id="wamid.vo5")
    assert "couldn't find a customer" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()
    assert client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"] == []


# ------------------------------------------------------------------ POST /api/tasks (dashboard task creation)


def test_create_task_via_api_notifies_assignee(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_id}",
        json={"title": "close register", "assigned_to_employee_id": ravi["employee_id"]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "close register"
    notif = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == RAVI_WA]
    assert len(notif) == 1


def test_create_task_via_api_with_customer_link(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000066666", phone="919000066666", message="hi", source="whatsapp",
    )
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_id}",
        json={"title": "resolve complaint", "assigned_to_employee_id": ravi["employee_id"], "customer_facing_lead_id": lead.lead_id},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["customer_facing_lead_id"] == lead.lead_id


def test_create_task_via_api_unknown_assignee_404(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_id}", json={"title": "x", "assigned_to_employee_id": "emp_nonexistent"}, headers=headers,
    )
    assert r.status_code == 404


def test_staff_cannot_create_task_via_api(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    from business_ai.auth import Principal, create_access_token

    staff_token = create_access_token(Principal.staff("staff_4", tenant_id), services_wa.settings)
    r = client_wa.post(
        f"/api/tasks?tenant_id={tenant_id}", json={"title": "x", "assigned_to_employee_id": ravi["employee_id"]},
        headers={"Authorization": f"Bearer {staff_token}"},
    )
    assert r.status_code == 403


# ------------------------------------------------------------------ NL fallback command routing


def test_nl_assign_task_creates_and_notifies(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    nl_text = "hey can ravi handle the shelf restock"
    services_wa.generator = lambda: FakeGenerator(
        employee_command_intents={
            nl_text: EmployeeCommandIntent(intent="assign_task", task_title="shelf restock", assignee_name="Ravi"),
        }
    )
    _send(client_wa, wa_id=OWNER_WA, text=nl_text, message_id="wamid.nl1")
    tasks = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["title"] == "shelf restock"
    assert tasks[0]["assigned_to_employee_id"] == ravi["employee_id"]
    notif = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == RAVI_WA]
    assert len(notif) == 1


def test_nl_assign_task_requires_manager(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _add_employee(client_wa, headers, tenant_id, whatsapp_number=PRIYA_WA, name="Priya", role="staff")
    nl_text = "priya should take the register task"
    services_wa.generator = lambda: FakeGenerator(
        employee_command_intents={
            nl_text: EmployeeCommandIntent(intent="assign_task", task_title="register task", assignee_name="Priya"),
        }
    )
    _send(client_wa, wa_id=RAVI_WA, text=nl_text, message_id="wamid.nl2")  # Ravi is staff, not owner/manager
    assert "only an owner or manager" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()
    assert client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"] == []


def test_nl_task_status_update_resolves_by_title_fragment(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="assign restock shelf 3 to Ravi", message_id="wamid.nl3")

    nl_text = "just finished the shelf restock"
    services_wa.generator = lambda: FakeGenerator(
        employee_command_intents={
            nl_text: EmployeeCommandIntent(intent="task_status_update", task_reference="shelf restock", new_status="done"),
        }
    )
    _send(client_wa, wa_id=RAVI_WA, text=nl_text, message_id="wamid.nl4")
    task = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]
    assert task["status"] == "done"


def test_nl_task_status_update_ambiguous_reference_asks_for_clarity(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    nl_text = "finished the thing"
    services_wa.generator = lambda: FakeGenerator(
        employee_command_intents={
            nl_text: EmployeeCommandIntent(intent="task_status_update", task_reference="thing", new_status="done"),
        }
    )
    _send(client_wa, wa_id=RAVI_WA, text=nl_text, message_id="wamid.nl5")
    assert "couldn't find exactly" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()


def test_nl_feedback_intent_records_via_shared_pipeline(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    nl_text = "the delivery van has been making a weird noise for days"
    services_wa.generator = lambda: FakeGenerator(
        employee_command_intents={
            nl_text: EmployeeCommandIntent(intent="feedback", feedback_text="the delivery van makes a weird noise"),
        }
    )
    _send(client_wa, wa_id=RAVI_WA, text=nl_text, message_id="wamid.nl6")
    items = client_wa.get(f"/api/feedback?tenant_id={tenant_id}", headers=headers).json()["items"]
    assert len(items) == 1
    assert items[0]["raw_text"] == "the delivery van makes a weird noise"


def test_nl_report_request_maps_to_scorecard(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    nl_text = "how are we doing this week"
    services_wa.generator = lambda: FakeGenerator(
        employee_command_intents={nl_text: EmployeeCommandIntent(intent="report_request", report_type="scorecard")}
    )
    _send(client_wa, wa_id=OWNER_WA, text=nl_text, message_id="wamid.nl7")
    assert "Business health" in services_wa.fake_whatsapp_client.sent[-1]["body"]


def test_nl_other_intent_falls_back_to_help(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="good morning!", message_id="wamid.nl8")
    assert "Commands:" in services_wa.fake_whatsapp_client.sent[-1]["body"]


def test_nl_classification_failure_falls_back_to_help_not_crash(client_wa, services_wa):
    class BrokenGenerator(FakeGenerator):
        def classify_employee_message(self, **kwargs):
            raise RuntimeError("simulated API outage")

    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.generator = lambda: BrokenGenerator()
    _send(client_wa, wa_id=RAVI_WA, text="something free-form here", message_id="wamid.nl9")
    assert "Commands:" in services_wa.fake_whatsapp_client.sent[-1]["body"]


# ------------------------------------------------------------------ outcome confirmation (revenue/appointments)


def test_whatsapp_mark_paid_records_confirmed_revenue(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000055555", phone="919000055555", name="Asha", source="whatsapp",
    )
    short_id = lead.lead_id[-6:]
    _send(client_wa, wa_id=OWNER_WA, text=f"mark paid {short_id} 800", message_id="wamid.rev1")
    ack = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "₹800" in ack and "Asha" in ack

    updated = services_wa.lead_store.get(tenant_id, lead.lead_id)
    assert updated.deposit_paid_amount_inr == 800
    assert updated.deposit_paid_at is not None


def test_whatsapp_mark_paid_falls_back_to_configured_deposit_amount(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    client_wa.put(f"/api/tenant?tenant_id={tenant_id}", json={"deposit_amount_inr": 300}, headers=headers)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000044444", phone="919000044444", source="whatsapp",
    )
    short_id = lead.lead_id[-6:]
    _send(client_wa, wa_id=OWNER_WA, text=f"mark paid {short_id}", message_id="wamid.rev2")
    assert services_wa.lead_store.get(tenant_id, lead.lead_id).deposit_paid_amount_inr == 300


def test_whatsapp_mark_paid_no_amount_no_config_asks_for_amount(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000033333", phone="919000033333", source="whatsapp",
    )
    short_id = lead.lead_id[-6:]
    _send(client_wa, wa_id=OWNER_WA, text=f"mark paid {short_id}", message_id="wamid.rev3")
    assert "no amount given" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()
    assert services_wa.lead_store.get(tenant_id, lead.lead_id).deposit_paid_at is None


def test_whatsapp_mark_outcome_variants(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000022222", phone="919000022222", source="whatsapp",
    )
    short_id = lead.lead_id[-6:]
    _send(client_wa, wa_id=OWNER_WA, text=f"mark no-show {short_id}", message_id="wamid.rev4")
    assert services_wa.lead_store.get(tenant_id, lead.lead_id).appointment_outcome == "no_show"

    _send(client_wa, wa_id=OWNER_WA, text=f"mark completed {short_id}", message_id="wamid.rev5")
    assert services_wa.lead_store.get(tenant_id, lead.lead_id).appointment_outcome == "completed"


def test_mark_outcome_and_paid_are_audited(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000011122", phone="919000011122", source="whatsapp",
    )
    short_id = lead.lead_id[-6:]
    _send(client_wa, wa_id=OWNER_WA, text=f"mark paid {short_id} 400", message_id="wamid.rev6")
    _send(client_wa, wa_id=OWNER_WA, text=f"mark cancelled {short_id}", message_id="wamid.rev7")
    actions = {e.action for e in services_wa.audit_log.list_for_tenant(tenant_id)}
    assert "deposit_confirmed_paid" in actions
    assert "appointment_outcome_recorded" in actions


def test_deposit_paid_api_rbac_and_audit(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000099911", phone="919000099911", source="whatsapp",
    )
    r = client_wa.post(f"/api/leads/{lead.lead_id}/deposit-paid?tenant_id={tenant_id}", json={"amount_inr": 650}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["deposit_paid_amount_inr"] == 650

    r2 = client_wa.post(
        f"/api/leads/{lead.lead_id}/appointment-outcome?tenant_id={tenant_id}", json={"outcome": "completed"}, headers=headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["appointment_outcome"] == "completed"

    r3 = client_wa.post(
        f"/api/leads/{lead.lead_id}/appointment-outcome?tenant_id={tenant_id}", json={"outcome": "bogus"}, headers=headers,
    )
    assert r3.status_code == 400


def test_deposit_paid_api_is_tenant_isolated(client_wa, services_wa):
    headers_a, tenant_a, ravi_a = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead_a = services_wa.lead_store.create(tenant_id=tenant_a, session_id="wa_919000088811", phone="919000088811")

    headers_b, tenant_b = _signup(client_wa, business_name="Salon B", email="depb@example.com")
    _activate_with_whatsapp(client_wa, headers_b, tenant_b, services_wa.settings.admin_secret, phone_number_id="PNID_DEPB")

    r = client_wa.post(f"/api/leads/{lead_a.lead_id}/deposit-paid?tenant_id={tenant_b}", json={"amount_inr": 500}, headers=headers_b)
    assert r.status_code == 404  # lead belongs to tenant A, not B


# ------------------------------------------------------------------ missed opportunities + conversion/revenue insights


def test_scorecard_shows_missed_opportunity(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000012345", phone="919000012345", name="Kiran", source="whatsapp",
    )
    services_wa.analytics_store.log_turn(
        tenant_id=tenant_id, session_id="wa_919000012345", query="do you have this in blue?",
        answer_status="answered", shows_buying_intent=True, suggested_handoff=False,
        shows_dissatisfaction=False, channel="whatsapp",
    )
    _send(client_wa, wa_id=OWNER_WA, text="scorecard", message_id="wamid.mo1")
    body = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "missed opportunity" in body.lower()
    assert "Kiran" in body


def test_missed_opportunity_excludes_leads_with_appointment(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000054321", phone="919000054321", name="Meera", source="whatsapp",
    )
    services_wa.analytics_store.log_turn(
        tenant_id=tenant_id, session_id="wa_919000054321", query="can I book?", answer_status="answered",
        shows_buying_intent=True, suggested_handoff=False, shows_dissatisfaction=False, channel="whatsapp",
    )
    services_wa.lead_store.set_appointment(tenant_id, lead.lead_id, "2027-01-01T10:00:00Z")

    _send(client_wa, wa_id=OWNER_WA, text="scorecard", message_id="wamid.mo2")
    body = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "missed opportunity" not in body.lower()


def test_scorecard_shows_conversion_and_confirmed_revenue(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(
        tenant_id=tenant_id, session_id="wa_919000067890", phone="919000067890", source="whatsapp",
    )
    services_wa.lead_store.mark_deposit_paid(tenant_id, lead.lead_id, 900)
    services_wa.lead_store.create(tenant_id=tenant_id, session_id="wa_919000011111", phone="919000011111", source="whatsapp")

    _send(client_wa, wa_id=OWNER_WA, text="scorecard", message_id="wamid.mo3")
    body = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "Leads: 2" in body and "1 converted" in body and "(50%)" in body
    assert "₹900" in body


def test_business_health_api_includes_revenue_and_opportunity_fields(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    lead = services_wa.lead_store.create(tenant_id=tenant_id, session_id="wa_919000099001", phone="919000099001")
    services_wa.lead_store.mark_deposit_paid(tenant_id, lead.lead_id, 1200)

    r = client_wa.get(f"/api/business-health?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["confirmed_revenue_inr"] == 1200
    assert data["converted_count"] == 1
    assert data["leads_count"] == 1
    assert data["conversion_rate_pct"] == 100


# ------------------------------------------------------------------ business timeline


def test_timeline_command_shows_task_and_revenue_events(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="assign restock shelf 3 to Ravi", message_id="wamid.tl1")
    short_id = client_wa.get(f"/api/tasks?tenant_id={tenant_id}", headers=headers).json()["tasks"][0]["task_id"][-6:]
    _send(client_wa, wa_id=RAVI_WA, text=f"start {short_id}", message_id="wamid.tl2")
    _send(client_wa, wa_id=RAVI_WA, text=f"done {short_id}", message_id="wamid.tl3")

    lead = services_wa.lead_store.create(tenant_id=tenant_id, session_id="wa_919000077001", phone="919000077001")
    lead_short = lead.lead_id[-6:]
    _send(client_wa, wa_id=OWNER_WA, text=f"mark paid {lead_short} 700", message_id="wamid.tl3b")

    _send(client_wa, wa_id=OWNER_WA, text="timeline", message_id="wamid.tl4")
    body = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "assigned a task" in body
    assert "started a task" in body
    assert "completed a task" in body
    assert "confirmed a deposit of ₹700" in body


def test_timeline_requires_owner_or_manager(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="timeline", message_id="wamid.tl5")
    assert "only an owner or manager" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()


def test_timeline_api_rbac_and_ordering(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="assign restock shelf 3 to Ravi", message_id="wamid.tl6")
    _send(client_wa, wa_id=OWNER_WA, text="assign close register to Ravi", message_id="wamid.tl7")

    r = client_wa.get(f"/api/timeline?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]
    # setup itself adds one "employee_added" entry — assert the two new
    # assignments are present and ordered most-recent-first, not an exact count.
    assign_entries = [e for e in entries if e["action"] == "task_assigned"]
    assert len(assign_entries) == 2
    assert entries[0]["created_at"] >= entries[-1]["created_at"]  # most recent first

    from business_ai.auth import Principal, create_access_token

    staff_token = create_access_token(Principal.staff("staff_5", tenant_id), services_wa.settings)
    r2 = client_wa.get(f"/api/timeline?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r2.status_code == 403


def test_timeline_is_tenant_isolated(client_wa, services_wa):
    headers_a, tenant_a, ravi_a = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="assign restock shelf 3 to Ravi", message_id="wamid.tl8")

    headers_b, tenant_b = _signup(client_wa, business_name="Salon B", email="tlb@example.com")
    _activate_with_whatsapp(client_wa, headers_b, tenant_b, services_wa.settings.admin_secret, phone_number_id="PNID_TLB")

    r = client_wa.get(f"/api/timeline?tenant_id={tenant_b}", headers=headers_b)
    assert r.json()["entries"] == []


# ------------------------------------------------------------------ trend detection (window vs prior window)


def _backdate_lead(services, tenant_id, lead_id, created_at_iso):
    with services.lead_store._db() as conn:
        conn.execute(
            "UPDATE leads SET created_at = ? WHERE tenant_id = ? AND lead_id = ?", (created_at_iso, tenant_id, lead_id)
        )
        conn.commit()


def test_business_health_trend_is_new_when_no_prior_period_data(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.lead_store.create(tenant_id=tenant_id, session_id="wa_919000010002", phone="919000010002")

    r = client_wa.get(f"/api/business-health?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["trends"]["leads"] == "▲ new"


def test_business_health_trend_percentage_when_prior_period_has_data(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    prior_window_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30 * 3600))  # 30h ago, in the prior 24-48h window

    for i in range(2):
        lead = services_wa.lead_store.create(tenant_id=tenant_id, session_id=f"wa_91900002000{i}", phone=f"91900002000{i}")
        _backdate_lead(services_wa, tenant_id, lead.lead_id, prior_window_ts)
    for i in range(4):
        services_wa.lead_store.create(tenant_id=tenant_id, session_id=f"wa_91900003000{i}", phone=f"91900003000{i}")

    r = client_wa.get(f"/api/business-health?tenant_id={tenant_id}", headers=headers)
    data = r.json()
    assert data["leads_count"] == 4
    assert data["trends"]["leads"] == "▲ 100%"  # 4 vs 2 prior = +100%


# ------------------------------------------------------------------ SOP draft assist (business memory)


def test_suggest_sop_drafts_from_recent_reports(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="feedback the register software crashed again", message_id="wamid.sg1")

    _send(client_wa, wa_id=OWNER_WA, text="suggest sop software/tools", message_id="wamid.sg2")
    body = services_wa.fake_whatsapp_client.sent[-1]["body"]
    assert "Draft guidance" in body
    assert "1 report(s)" in body  # FakeGenerator's deterministic draft echoes the report count
    assert "approve sop software_or_tools:" in body


def test_suggest_sop_requires_owner(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    manager_wa = "919876500099"
    _add_employee(client_wa, headers, tenant_id, whatsapp_number=manager_wa, name="Meena", role="manager")
    _send(client_wa, wa_id=manager_wa, text="suggest sop software/tools", message_id="wamid.sg3")
    assert "only the owner" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()


def test_suggest_sop_with_no_reports_yet(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="suggest sop pay/compensation", message_id="wamid.sg4")
    assert "no feedback reports found" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()


def test_suggest_sop_generation_failure_is_graceful(client_wa, services_wa):
    class BrokenGenerator(FakeGenerator):
        def draft_sop_note(self, **kwargs):
            raise RuntimeError("simulated outage")

    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="feedback the register software crashed again", message_id="wamid.sg5")
    services_wa.generator = lambda: BrokenGenerator()
    _send(client_wa, wa_id=OWNER_WA, text="suggest sop software/tools", message_id="wamid.sg6")
    assert "couldn't generate a draft" in services_wa.fake_whatsapp_client.sent[-1]["body"].lower()
