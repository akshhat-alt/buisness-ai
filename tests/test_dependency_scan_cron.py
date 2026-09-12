"""Tests for Phase 10's proactive dependency-risk scan:
POST /api/v1/admin/dependency-scan/run — notifies on NEW high-severity
(single-point-of-failure) risk, dedupes via the audit log exactly like
every other proactive cron in this codebase.
"""

from __future__ import annotations

from tests.test_admin_bot import OWNER_WA, _admin_headers, _setup_tenant_with_owner_and_staff
from tests.test_whatsapp import client_wa, services_wa

__all__ = ["client_wa", "services_wa"]


def _make_bus_factor_one_process(services, tenant_id, employee_id, title="Restock shelf 3", count=2):
    for _ in range(count):
        t = services.task_store.create(
            tenant_id=tenant_id, title=title, assigned_to_employee_id=employee_id, assigned_by_employee_id=employee_id,
        )
        services.task_store.update_status(tenant_id, t.task_id, "done")


def test_dependency_scan_notifies_on_new_high_severity_risk(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _make_bus_factor_one_process(services_wa, tenant_id, ravi["employee_id"])
    admin_headers = _admin_headers(client_wa, services_wa)

    r = client_wa.post("/api/v1/admin/dependency-scan/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert tenant_id in r.json()["notified"]

    body = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Restock shelf 3" in body


def test_dependency_scan_does_not_renotify_the_same_risk(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _make_bus_factor_one_process(services_wa, tenant_id, ravi["employee_id"])
    admin_headers = _admin_headers(client_wa, services_wa)

    client_wa.post("/api/v1/admin/dependency-scan/run", headers=admin_headers)
    services_wa.fake_whatsapp_client.sent.clear()

    r2 = client_wa.post("/api/v1/admin/dependency-scan/run", headers=admin_headers)
    assert r2.status_code == 200, r2.text
    assert tenant_id not in r2.json()["notified"]
    assert services_wa.fake_whatsapp_client.sent == []


def test_dependency_scan_skips_tenant_with_no_high_severity_risk(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    admin_headers = _admin_headers(client_wa, services_wa)

    r = client_wa.post("/api/v1/admin/dependency-scan/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert tenant_id not in r.json()["notified"]
    assert any(s["tenant_id"] == tenant_id for s in r.json()["skipped"])


def test_dependency_scan_writes_audit_entry(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _make_bus_factor_one_process(services_wa, tenant_id, ravi["employee_id"])
    admin_headers = _admin_headers(client_wa, services_wa)

    client_wa.post("/api/v1/admin/dependency-scan/run", headers=admin_headers)
    entries = services_wa.audit_log.list_for_tenant(tenant_id, action="dependency_risk_flagged")
    assert len(entries) == 1
    assert "Restock shelf 3" in entries[0].metadata["summary"]


def test_dependency_scan_renotifies_a_genuinely_new_risk(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    _make_bus_factor_one_process(services_wa, tenant_id, ravi["employee_id"], title="Restock shelf 3")
    admin_headers = _admin_headers(client_wa, services_wa)
    client_wa.post("/api/v1/admin/dependency-scan/run", headers=admin_headers)
    services_wa.fake_whatsapp_client.sent.clear()

    # A second, DIFFERENT bus-factor-1 process appears later — must still notify.
    _make_bus_factor_one_process(services_wa, tenant_id, ravi["employee_id"], title="Close register")
    r2 = client_wa.post("/api/v1/admin/dependency-scan/run", headers=admin_headers)
    assert tenant_id in r2.json()["notified"]
    body = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Close register" in body
    assert "Restock shelf 3" not in body  # already flagged, not repeated


def test_dependency_scan_requires_platform_admin(client_wa, services_wa):
    headers, tenant_id, ravi = _setup_tenant_with_owner_and_staff(client_wa, services_wa)
    r = client_wa.post("/api/v1/admin/dependency-scan/run", headers=headers)
    assert r.status_code == 403
