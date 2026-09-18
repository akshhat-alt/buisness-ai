"""HTTP + WhatsApp-command tests for Phase 15's Revenue Leakage Radar:
GET /api/revenue-radar and the admin bot's "revenue radar"/"leakage"
commands.
"""

from __future__ import annotations

import pytest

from business_ai.auth import Principal, create_access_token
from tests.test_admin_bot import OWNER_WA, RAVI_WA, _add_employee, _send
from tests.test_whatsapp import _activate_with_whatsapp, _signup, client_wa, services_wa

__all__ = ["client_wa", "services_wa"]


@pytest.fixture()
def owner_session_growth(services, owner_session):
    # Revenue Radar (VIEW_FEEDBACK) is Growth-tier under Phase 1's
    # plan-gating; the WhatsApp command test below uses its own separate
    # client_wa/services_wa fixtures and is unaffected.
    _, tenant_id = owner_session
    services.tenant_registry.update_config(tenant_id, plan="growth")
    return owner_session


def test_revenue_radar_route_requires_owner_or_manager(client, owner_session_growth, services):
    headers, tenant_id = owner_session_growth
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)

    r = client.get(f"/api/revenue-radar?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403

    r = client.get(f"/api/revenue-radar?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["missed_buying_intent_leads"] == []


def test_revenue_radar_route_is_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "radar-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz Radar"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r = client.get(f"/api/revenue-radar?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_revenue_radar_reflects_a_no_show(client, owner_session_growth, services):
    headers, tenant_id = owner_session_growth
    lead = services.lead_store.create(tenant_id=tenant_id, session_id="s1", phone="9876543210")
    services.lead_store.record_appointment_outcome(tenant_id, lead.lead_id, "no_show")
    r = client.get(f"/api/revenue-radar?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["no_show_appointments"]) == 1


def test_revenue_radar_command_is_owner_manager_gated(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    _add_employee(client_wa, headers, tenant_id, whatsapp_number=RAVI_WA, name="Ravi")

    _send(client_wa, wa_id=RAVI_WA, text="revenue radar", message_id="wamid.radar1")
    staff_reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == RAVI_WA][-1]["body"]
    assert "Only an owner or manager" in staff_reply

    _send(client_wa, wa_id=OWNER_WA, text="leakage", message_id="wamid.radar2")
    owner_reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "nothing leaking" in owner_reply
