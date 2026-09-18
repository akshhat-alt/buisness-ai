"""HTTP-level tests for Phase 16's on-demand scorecard read route:
GET /api/scorecard.
"""

from __future__ import annotations

import pytest

from business_ai.auth import Principal, create_access_token


@pytest.fixture(autouse=True)
def _growth_plan(services, owner_session):
    # Scorecard (VIEW_FINANCIALS) is Growth-tier under Phase 1's plan-gating.
    _, tenant_id = owner_session
    services.tenant_registry.update_config(tenant_id, plan="growth")


def test_scorecard_route_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)

    r = client.get(f"/api/scorecard?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403

    r = client.get(f"/api/scorecard?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert "this_week" in r.json()


def test_scorecard_route_is_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "scorecard-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz SC"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r = client.get(f"/api/scorecard?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_scorecard_route_reflects_real_financial_activity(client, owner_session, services):
    headers, tenant_id = owner_session
    services.metric_store.record(tenant_id=tenant_id, metric_type="sale", amount_inr=4200)
    r = client.get(f"/api/scorecard?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["this_week"]["sales_inr"] == 4200


def test_scorecard_route_has_no_side_effects(client, owner_session, services):
    """Pulling the scorecard on demand must never send email/WhatsApp or
    write any dedup state — it's a pure read, unlike the cron."""
    headers, tenant_id = owner_session
    client.get(f"/api/scorecard?tenant_id={tenant_id}", headers=headers)
    client.get(f"/api/scorecard?tenant_id={tenant_id}", headers=headers)
    assert services.audit_log.list_for_tenant(tenant_id) == []
