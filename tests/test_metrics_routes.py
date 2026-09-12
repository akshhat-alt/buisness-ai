"""HTTP + WhatsApp-command tests for Phase 12's Financial Truth Layer:
logging via the admin bot's `log <type> <amount> [note]` grammar (open to
any roster member, no permission check), and viewing the aggregated
summary/CSV export (owner/manager-gated, tenant-isolated).
"""

from __future__ import annotations

from business_ai.auth import Principal, create_access_token
from tests.test_admin_bot import OWNER_WA, RAVI_WA, _add_employee, _send
from tests.test_whatsapp import _activate_with_whatsapp, _signup, client_wa, services_wa

__all__ = ["client_wa", "services_wa"]


def test_log_sale_via_whatsapp_creates_a_metric_and_replies(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    _send(client_wa, wa_id=OWNER_WA, text="log sale 1500 haircut", message_id="wamid.log1")

    entries = services_wa.metric_store.list_for_tenant(tenant_id)
    assert len(entries) == 1
    assert entries[0].metric_type == "sale"
    assert entries[0].amount_inr == 1500
    assert entries[0].note == "haircut"

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "1500" in reply


def test_log_metric_open_to_any_roster_member_not_just_owner(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    _add_employee(client_wa, headers, tenant_id, whatsapp_number=RAVI_WA, name="Ravi")

    _send(client_wa, wa_id=RAVI_WA, text="log expense 300 supplies", message_id="wamid.log2")

    entries = services_wa.metric_store.list_for_tenant(tenant_id)
    assert len(entries) == 1
    assert entries[0].metric_type == "expense"


def test_log_metric_rejects_bad_amount_without_crashing(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    _send(client_wa, wa_id=OWNER_WA, text="log sale notanumber", message_id="wamid.log3")
    assert services_wa.metric_store.list_for_tenant(tenant_id) == []


def test_financials_command_is_owner_manager_gated(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    _add_employee(client_wa, headers, tenant_id, whatsapp_number=RAVI_WA, name="Ravi")
    services_wa.metric_store.record(tenant_id=tenant_id, metric_type="sale", amount_inr=1000)

    _send(client_wa, wa_id=RAVI_WA, text="financials", message_id="wamid.fin1")
    staff_reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == RAVI_WA][-1]["body"]
    assert "Only an owner or manager" in staff_reply

    _send(client_wa, wa_id=OWNER_WA, text="financials", message_id="wamid.fin2")
    owner_reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "1000" in owner_reply


def test_metrics_summary_route_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    services.metric_store.record(tenant_id=tenant_id, metric_type="sale", amount_inr=2500)
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)

    r = client.get(f"/api/metrics/summary?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403

    r = client.get(f"/api/metrics/summary?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] == "manual"
    assert any(s["metric_type"] == "sale" and s["total_inr"] == 2500 for s in data["summary"])


def test_metrics_summary_is_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "metrics-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r = client.get(f"/api/metrics/summary?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_metrics_export_csv_contains_logged_entries(client, owner_session, services):
    headers, tenant_id = owner_session
    services.metric_store.record(tenant_id=tenant_id, metric_type="expense", amount_inr=750, note="supplies")
    r = client.get(f"/api/metrics/export.csv?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert "expense" in r.text
    assert "750" in r.text
    assert "supplies" in r.text
