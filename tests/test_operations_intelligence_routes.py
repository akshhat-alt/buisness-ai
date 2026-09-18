"""HTTP-level tests for Phase 23's remaining Restaurant Operations
Intelligence routes: GET /api/customer-behavior, GET /api/supplier-
intelligence, GET /api/leads/upcoming-appointments, and the
appointment route's new party_size field.
"""

from __future__ import annotations

import pytest

from business_ai.auth import Principal, create_access_token


@pytest.fixture(autouse=True)
def _growth_plan(services, owner_session):
    # Supplier/customer intelligence (VIEW_FINANCIALS/VIEW_INVENTORY) is
    # Growth-tier under Phase 1's plan-gating.
    _, tenant_id = owner_session
    services.tenant_registry.update_config(tenant_id, plan="growth")


# ------------------------------------------------------------------ reservations / party_size


def test_setting_an_appointment_with_party_size_persists_it(client, owner_session, services):
    headers, tenant_id = owner_session
    lead = services.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210", name="John")
    lead_id = lead.lead_id

    r2 = client.put(
        f"/api/leads/{lead_id}/appointment?tenant_id={tenant_id}",
        json={"appointment_at": "2026-09-20T19:00:00", "party_size": 4},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["party_size"] == 4


def test_rescheduling_without_party_size_keeps_the_existing_value(client, owner_session, services):
    headers, tenant_id = owner_session
    lead = services.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210", name="John")
    lead_id = lead.lead_id
    client.put(
        f"/api/leads/{lead_id}/appointment?tenant_id={tenant_id}",
        json={"appointment_at": "2026-09-20T19:00:00", "party_size": 4}, headers=headers,
    )
    r2 = client.put(
        f"/api/leads/{lead_id}/appointment?tenant_id={tenant_id}",
        json={"appointment_at": "2026-09-21T19:00:00"}, headers=headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["party_size"] == 4


def test_appointment_rejects_a_non_positive_party_size(client, owner_session, services):
    headers, tenant_id = owner_session
    lead = services.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210", name="John")
    lead_id = lead.lead_id
    r2 = client.put(
        f"/api/leads/{lead_id}/appointment?tenant_id={tenant_id}",
        json={"appointment_at": "2026-09-20T19:00:00", "party_size": 0}, headers=headers,
    )
    assert r2.status_code == 422


def test_upcoming_appointments_route_lists_only_unconfirmed_future_ones(client, owner_session, services):
    headers, tenant_id = owner_session
    lead = services.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210", name="Upcoming")
    import time
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    services.lead_store.set_appointment(tenant_id, lead.lead_id, future, party_size=2)

    lead2 = services.lead_store.create(tenant_id=tenant_id, session_id="s2", phone="9999999999", name="Already Done")
    services.lead_store.set_appointment(tenant_id, lead2.lead_id, future)
    services.lead_store.record_appointment_outcome(tenant_id, lead2.lead_id, "completed")

    r3 = client.get(f"/api/leads/upcoming-appointments?tenant_id={tenant_id}", headers=headers)
    assert r3.status_code == 200, r3.text
    leads = r3.json()["leads"]
    assert len(leads) == 1
    assert leads[0]["name"] == "Upcoming"
    assert leads[0]["party_size"] == 2


def test_upcoming_appointments_requires_view_leads(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "upcoming-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r = client.get(f"/api/leads/upcoming-appointments?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


# ------------------------------------------------------------------ customer behavior


def test_customer_behavior_route_reflects_real_lead_data(client, owner_session, services):
    headers, tenant_id = owner_session
    for i in range(2):
        lead = services.lead_store.create(tenant_id=tenant_id, session_id=f"s{i}", name="John", phone="9876543210")
        services.lead_store.record_appointment_outcome(tenant_id, lead.lead_id, "completed")

    r = client.get(f"/api/customer-behavior?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["repeat_customers"]) == 1


def test_customer_behavior_requires_view_leads(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = client.get(f"/api/customer-behavior?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 200, r.text  # VIEW_LEADS is available to staff too


# ------------------------------------------------------------------ supplier intelligence


def test_supplier_intelligence_route_reflects_real_purchase_data(client, owner_session, services):
    headers, tenant_id = owner_session
    supplier = services.supplier_store.create(tenant_id=tenant_id, name="Ramesh Traders")
    services.purchase_store.record(
        tenant_id=tenant_id, ingredient_name="Chicken", quantity=10, unit="kg", amount_inr=4000, supplier_id=supplier.supplier_id,
    )
    r = client.get(f"/api/supplier-intelligence?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["suppliers"][0]["name"] == "Ramesh Traders"
    assert r.json()["suppliers"][0]["total_spend_inr"] == 4000


def test_supplier_intelligence_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = client.get(f"/api/supplier-intelligence?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403
