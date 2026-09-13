"""Tests for Phase 26's dashboard-facing "Ask Your Business Anything"
route: POST /api/business-query. Reuses classify_employee_message's
report_request intent (the SAME classifier/taxonomy admin_bot.py's
WhatsApp NL fallback uses) — these tests configure FakeGenerator's
employee_command_intents exactly like tests/test_admin_bot.py's own NL
tests do.
"""

from __future__ import annotations

from business_ai.auth import Principal, create_access_token
from business_ai.generation import EmployeeCommandIntent


def _ask(client, headers, tenant_id, services, *, question: str, report_type: str | None, intent: str = "report_request"):
    from tests.conftest import FakeGenerator

    services.generator = lambda: FakeGenerator(
        employee_command_intents={question: EmployeeCommandIntent(intent=intent, report_type=report_type)}
    )
    return client.post(f"/api/business-query?tenant_id={tenant_id}", headers=headers, json={"question": question})


def test_business_query_requires_authentication(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(f"/api/business-query?tenant_id={tenant_id}", json={"question": "what's our food cost"})
    assert r.status_code == 401


def test_business_query_rejects_an_empty_question(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(f"/api/business-query?tenant_id={tenant_id}", headers=headers, json={"question": "   "})
    assert r.status_code == 400


def test_business_query_answers_food_cost(client, owner_session, services):
    headers, tenant_id = owner_session
    services.menu_store.create_item(tenant_id=tenant_id, name="Lassi", price_inr=80)
    r = _ask(client, headers, tenant_id, services, question="what's our food cost", report_type="food_cost")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report_type"] == "food_cost"
    assert "Lassi" in body["answer"] or "food cost" in body["answer"].lower()


def test_business_query_answers_reorder(client, owner_session, services):
    headers, tenant_id = owner_session
    r = _ask(client, headers, tenant_id, services, question="do we need to reorder anything", report_type="reorder")
    assert r.status_code == 200, r.text
    assert "No reorder suggestions" in r.json()["answer"]


def test_business_query_answers_reviews(client, owner_session, services):
    headers, tenant_id = owner_session
    services.review_store.record(tenant_id=tenant_id, platform="google", rating=4.6, review_count=213, source="google_places")
    r = _ask(client, headers, tenant_id, services, question="what's our google rating", report_type="reviews")
    assert r.status_code == 200, r.text
    assert "4.6" in r.json()["answer"]


def test_business_query_answers_gm_report(client, owner_session, services):
    headers, tenant_id = owner_session
    r = _ask(client, headers, tenant_id, services, question="give me the daily briefing", report_type="gm_report")
    assert r.status_code == 200, r.text
    assert "Digital GM briefing" in r.json()["answer"]


def test_business_query_returns_a_hint_for_a_no_standalone_renderer_type(client, owner_session, services):
    headers, tenant_id = owner_session
    r = _ask(client, headers, tenant_id, services, question="what tasks are open today", report_type="today")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report_type"] == "today"
    assert "Team & Tasks" in body["answer"]


def test_business_query_returns_a_fallback_for_unrecognized_questions(client, owner_session, services):
    headers, tenant_id = owner_session
    r = _ask(client, headers, tenant_id, services, question="what's the meaning of life", report_type=None, intent="other")
    assert r.status_code == 200, r.text
    assert r.json()["report_type"] is None


def test_business_query_enforces_the_same_gate_as_the_equivalent_get_route(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = _ask(
        client, {"Authorization": f"Bearer {staff_token}"}, tenant_id, services,
        question="what's our food cost", report_type="food_cost",
    )
    assert r.status_code == 403


def test_business_query_is_tenant_isolated(client, owner_session, services):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "bq-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r = _ask(client, headers_b, tenant_a, services, question="food cost", report_type="food_cost")
    assert r.status_code == 403
