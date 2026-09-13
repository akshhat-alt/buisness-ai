"""Tests for Phase 26 (Ask Your Business Anything): the NL report_request
classifier's expanded report_type taxonomy correctly routes a free-form
owner question to the SAME existing deterministic command/report a typed
command already produces — no new computation, only a wider set of
things classify_employee_message can recognize and point at.
"""

from __future__ import annotations

from business_ai.generation import EmployeeCommandIntent
from tests.conftest import FakeGenerator
from tests.test_admin_bot import OWNER_WA, _send, _setup_tenant_with_owner_and_staff
from tests.test_whatsapp import client_wa, services_wa

__all__ = ["client_wa", "services_wa"]


def _ask(client_wa, services_wa, *, nl_text: str, report_type: str, message_id: str) -> str:
    services_wa.generator = lambda: FakeGenerator(
        employee_command_intents={nl_text: EmployeeCommandIntent(intent="report_request", report_type=report_type)}
    )
    _send(client_wa, wa_id=OWNER_WA, text=nl_text, message_id=message_id)
    return [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]


def test_nl_query_routes_to_sales_financials(client_wa, services_wa):
    _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    reply = _ask(client_wa, services_wa, nl_text="how much did we make this month", report_type="sales", message_id="wamid.q1")
    assert "No manual sales/expense/collection entries" in reply


def test_nl_query_routes_to_food_cost(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.menu_store.create_item(tenant_id=tenant_id, name="Lassi", price_inr=80)
    reply = _ask(client_wa, services_wa, nl_text="what does our food cost look like", report_type="food_cost", message_id="wamid.q2")
    assert "food cost" in reply.lower() or "Lassi" in reply


def test_nl_query_routes_to_reorder(client_wa, services_wa):
    _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    reply = _ask(client_wa, services_wa, nl_text="do we need to reorder anything", report_type="reorder", message_id="wamid.q3")
    assert "No reorder suggestions" in reply


def test_nl_query_routes_to_reservations(client_wa, services_wa):
    _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    reply = _ask(client_wa, services_wa, nl_text="any bookings coming up", report_type="reservations", message_id="wamid.q4")
    assert "No reservations" in reply


def test_nl_query_routes_to_repeat_customers(client_wa, services_wa):
    _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    reply = _ask(client_wa, services_wa, nl_text="who are our regulars", report_type="repeat_customers", message_id="wamid.q5")
    assert "No repeat customers yet" in reply


def test_nl_query_routes_to_supplier_spend(client_wa, services_wa):
    _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    reply = _ask(client_wa, services_wa, nl_text="how much are we spending with suppliers", report_type="supplier_spend", message_id="wamid.q6")
    assert "No purchases logged yet" in reply


def test_nl_query_routes_to_reviews(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    services_wa.review_store.record(tenant_id=tenant_id, platform="google", rating=4.6, review_count=213, source="google_places")
    reply = _ask(client_wa, services_wa, nl_text="what's our google rating", report_type="reviews", message_id="wamid.q7")
    assert "Google" in reply and "4.6" in reply


def test_nl_query_routes_to_gm_report(client_wa, services_wa):
    _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    reply = _ask(client_wa, services_wa, nl_text="give me the daily briefing", report_type="gm_report", message_id="wamid.q8")
    assert "Digital GM briefing" in reply


def test_nl_query_routes_to_menu_recommendations(client_wa, services_wa):
    _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    reply = _ask(client_wa, services_wa, nl_text="should we change any menu prices", report_type="menu_recommendations", message_id="wamid.q9")
    assert "No menu recommendations" in reply


def test_nl_query_routes_to_revenue_leakage(client_wa, services_wa):
    _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    reply = _ask(client_wa, services_wa, nl_text="are we losing any revenue", report_type="revenue_leakage", message_id="wamid.q10")
    assert "nothing leaking right now" in reply


def test_nl_query_routes_to_inventory(client_wa, services_wa):
    _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    reply = _ask(client_wa, services_wa, nl_text="what's low on stock", report_type="inventory", message_id="wamid.q11")
    assert "Nothing below par level" in reply


def test_nl_query_routes_to_shifts_today(client_wa, services_wa):
    _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    reply = _ask(client_wa, services_wa, nl_text="who's working today", report_type="shifts_today", message_id="wamid.q12")
    assert "No shifts scheduled for today" in reply
