"""HTTP-level tests for Phase 13's weekly scorecard cron:
POST /api/v1/admin/weekly-scorecard/run.
"""

from __future__ import annotations


def test_weekly_scorecard_requires_platform_admin(client, owner_session):
    headers, _ = owner_session
    r = client.post("/api/v1/admin/weekly-scorecard/run", headers=headers)
    assert r.status_code == 403


def test_weekly_scorecard_skips_inactive_tenant(client, owner_session, admin_headers):
    headers, tenant_id = owner_session
    r = client.post("/api/v1/admin/weekly-scorecard/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert any(s["tenant_id"] == tenant_id and s["reason"] == "not active" for s in r.json()["skipped"])


def test_weekly_scorecard_skips_active_tenant_with_no_activity(client, owner_session, activate_tenant, admin_headers):
    headers, tenant_id = owner_session
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    activate_tenant(tenant_id)

    r = client.post("/api/v1/admin/weekly-scorecard/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert any(s["tenant_id"] == tenant_id and s["reason"] == "no activity this week" for s in r.json()["skipped"])


def test_weekly_scorecard_sends_and_delivers_via_whatsapp_when_there_is_activity(
    client, owner_session, activate_tenant, services, admin_headers,
):
    headers, tenant_id = owner_session
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    activate_tenant(tenant_id)
    services.metric_store.record(tenant_id=tenant_id, metric_type="sale", amount_inr=3000)

    r = client.post("/api/v1/admin/weekly-scorecard/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert tenant_id in r.json()["sent"]
