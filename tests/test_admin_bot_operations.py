"""Tests for Phase 23's Restaurant Operations Intelligence WhatsApp
commands: reservations ("reserve"/"reservations"), shift scheduling
("schedule"/"cancel shift"/"my shifts"/"shifts today"), repeat
customers, and supplier spend.
"""

from __future__ import annotations

from tests.test_admin_bot import OWNER_WA, RAVI_WA, _send, _setup_tenant_with_owner_and_staff
from tests.test_whatsapp import client_wa, services_wa

__all__ = ["client_wa", "services_wa"]


# ------------------------------------------------------------------ reservations


def test_reserve_command_creates_a_lead_with_appointment_and_party_size(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    _send(client_wa, wa_id=OWNER_WA, text="reserve John Smith 9876543210 for 4 on 2026-12-20 19:00", message_id="wamid.res1")

    leads = services_wa.lead_store.list_for_tenant(tenant_id)
    assert len(leads) == 1
    assert leads[0].name == "John Smith"
    assert leads[0].phone == "9876543210"
    assert leads[0].party_size == 4
    assert leads[0].appointment_at is not None

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "John Smith" in reply
    assert "4" in reply


def test_reserve_command_rejects_an_unparseable_date(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="reserve John 9876543210 for 4 on not-a-date", message_id="wamid.res2")
    assert services_wa.lead_store.list_for_tenant(tenant_id) == []
    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Couldn't read" in reply


def test_reservations_command_lists_upcoming_and_is_owner_manager_gated(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    import time
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    lead = services_wa.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210", name="Jane")
    services_wa.lead_store.set_appointment(tenant_id, lead.lead_id, future, party_size=2)

    _send(client_wa, wa_id=RAVI_WA, text="reservations", message_id="wamid.res3")
    staff_reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == RAVI_WA][-1]["body"]
    assert "Only an owner or manager" in staff_reply

    _send(client_wa, wa_id=OWNER_WA, text="reservations", message_id="wamid.res4")
    owner_reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Jane" in owner_reply


def test_reservations_command_all_clear_when_nothing_upcoming(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="bookings", message_id="wamid.res5")
    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "No reservations" in reply


# ------------------------------------------------------------------ shifts


def test_schedule_command_creates_a_shift_and_notifies_the_employee(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)

    _send(client_wa, wa_id=OWNER_WA, text="schedule Ravi 2026-09-20 09:00-17:00 kitchen", message_id="wamid.sh1")

    shifts = services_wa.shift_store.list_for_tenant(tenant_id)
    assert len(shifts) == 1
    assert shifts[0].employee_id == ravi["employee_id"]
    assert shifts[0].role_label == "kitchen"

    ping = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == RAVI_WA]
    assert len(ping) == 1
    assert "2026-09-20" in ping[0]["body"]


def test_schedule_command_is_owner_manager_gated(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=RAVI_WA, text="schedule Ravi 2026-09-20 09:00-17:00", message_id="wamid.sh2")
    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == RAVI_WA][-1]["body"]
    assert "Only an owner or manager" in reply
    assert services_wa.shift_store.list_for_tenant(tenant_id) == []


def test_schedule_command_rejects_an_ambiguous_name(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _send(client_wa, wa_id=OWNER_WA, text="schedule Someone Else 2026-09-20 09:00-17:00", message_id="wamid.sh3")
    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Couldn't find" in reply


def test_cancel_shift_command_removes_the_shift(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    shift = services_wa.shift_store.create(
        tenant_id=tenant_id, employee_id=ravi["employee_id"], shift_date="2026-09-20", start_time="09:00", end_time="17:00",
    )
    short_id = shift.shift_id[-6:]

    _send(client_wa, wa_id=OWNER_WA, text=f"cancel shift {short_id}", message_id="wamid.sh4")
    assert services_wa.shift_store.get(tenant_id, shift.shift_id) is None


def test_my_shifts_command_shows_only_the_callers_own_shifts(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.shift_store.create(
        tenant_id=tenant_id, employee_id=ravi["employee_id"], shift_date="2099-01-01", start_time="09:00", end_time="17:00",
    )
    _send(client_wa, wa_id=RAVI_WA, text="my shifts", message_id="wamid.sh5")
    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == RAVI_WA][-1]["body"]
    assert "2099-01-01" in reply


def test_shifts_today_command_is_owner_manager_gated_and_shows_who(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    import time
    today = time.strftime("%Y-%m-%d", time.gmtime())
    services_wa.shift_store.create(
        tenant_id=tenant_id, employee_id=ravi["employee_id"], shift_date=today, start_time="09:00", end_time="17:00",
    )

    _send(client_wa, wa_id=RAVI_WA, text="shifts today", message_id="wamid.sh6")
    staff_reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == RAVI_WA][-1]["body"]
    assert "Only an owner or manager" in staff_reply

    _send(client_wa, wa_id=OWNER_WA, text="shifts", message_id="wamid.sh7")
    owner_reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Ravi" in owner_reply


# ------------------------------------------------------------------ repeat customers / supplier spend


def test_repeat_customers_command(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    for i in range(2):
        lead = services_wa.lead_store.create(tenant_id=tenant_id, session_id=f"s{i}", phone="9876543210", name="John")
        services_wa.lead_store.record_appointment_outcome(tenant_id, lead.lead_id, "completed")

    _send(client_wa, wa_id=OWNER_WA, text="repeat customers", message_id="wamid.rc1")
    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "John" in reply


def test_supplier_spend_command(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    supplier = services_wa.supplier_store.create(tenant_id=tenant_id, name="Ramesh Traders")
    services_wa.purchase_store.record(
        tenant_id=tenant_id, ingredient_name="Chicken", quantity=10, unit="kg", amount_inr=4000, supplier_id=supplier.supplier_id,
    )

    _send(client_wa, wa_id=OWNER_WA, text="supplier spend", message_id="wamid.ss1")
    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Ramesh Traders" in reply
    assert "4000" in reply
