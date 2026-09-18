from __future__ import annotations

import pytest

from business_ai.alerts import render_plan_upgrade_request_alert
from business_ai.tenant import TenantConfig, TenantStatus


def test_render_plan_upgrade_request_alert():
    subject, html = render_plan_upgrade_request_alert(
        business_name="Priya Salon",
        tenant_id="salon-123",
        owner_email="priya@example.com",
        current_plan="starter",
        target_plan="growth",
        note="We are opening a second branch and need shift management.",
        dashboard_url="https://app.bizistic.com/dashboard",
    )
    assert "Plan upgrade request: Priya Salon → Growth" in subject
    assert "priya@example.com" in html
    assert "Priya Salon" in html
    assert "Starter" in html
    assert "Growth" in html
    assert "second branch" in html
    assert "https://app.bizistic.com/dashboard" in html


def test_plan_upgrade_request_endpoint_success(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(
        f"/api/tenant/plan/upgrade-request?tenant_id={tenant_id}",
        json={"target_plan": "growth", "note": "Need team feedback tools"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "received"
    assert data["tenant_id"] == tenant_id
    assert data["current_plan"] == "starter"
    assert data["target_plan"] == "growth"
    assert "Growth" in data["message"]


def test_plan_upgrade_request_validation(client, owner_session, services):
    headers, tenant_id = owner_session

    # Invalid plan name
    r = client.post(
        f"/api/tenant/plan/upgrade-request?tenant_id={tenant_id}",
        json={"target_plan": "enterprise"},
        headers=headers,
    )
    assert r.status_code == 400
    assert "Invalid target plan" in r.json()["detail"]

    # Target plan same as current
    r = client.post(
        f"/api/tenant/plan/upgrade-request?tenant_id={tenant_id}",
        json={"target_plan": "starter"},
        headers=headers,
    )
    assert r.status_code == 400
    assert "Invalid target plan" in r.json()["detail"]

    # Set tenant plan to scale, then request growth (downgrade)
    services.tenant_registry.update_config(tenant_id, plan="scale")
    r = client.post(
        f"/api/tenant/plan/upgrade-request?tenant_id={tenant_id}",
        json={"target_plan": "growth"},
        headers=headers,
    )
    assert r.status_code == 400
    assert "Cannot request downgrade" in r.json()["detail"]

    # Already on scale, requesting scale
    r = client.post(
        f"/api/tenant/plan/upgrade-request?tenant_id={tenant_id}",
        json={"target_plan": "scale"},
        headers=headers,
    )
    assert r.status_code == 400
    assert "Already on the scale plan" in r.json()["detail"]


def test_plan_upgrade_request_requires_auth(client, owner_session):
    _, tenant_id = owner_session
    r = client.post(
        f"/api/tenant/plan/upgrade-request?tenant_id={tenant_id}",
        json={"target_plan": "growth"},
    )
    assert r.status_code == 403


def test_plan_upgrade_request_sends_email(client_with_email, services_with_email):
    import dataclasses
    services_with_email.settings = dataclasses.replace(services_with_email.settings, platform_admin_email="admin@bizistic.example")

    # signup fresh owner
    r = client_with_email.post(
        "/api/auth/signup",
        json={"email": "upgradeowner@example.com", "password": "password123", "name": "Upgrade Owner", "business_name": "Upgrade Co"},
    )
    assert r.status_code == 200
    token = r.json()["access_token"]
    tenant_id = r.json()["tenant_id"]
    headers = {"Authorization": f"Bearer {token}"}

    services_with_email.fake_email_sender.sent.clear()  # clear signup alert

    r = client_with_email.post(
        f"/api/tenant/plan/upgrade-request?tenant_id={tenant_id}",
        json={"target_plan": "scale", "note": "Need self-evolution"},
        headers=headers,
    )
    assert r.status_code == 200
    sent = [s for s in services_with_email.fake_email_sender.sent if "Plan upgrade request" in s["subject"]]
    assert len(sent) == 1
    assert sent[0]["to"] == "admin@bizistic.example"
    assert "Scale" in sent[0]["subject"]
    assert "Upgrade Co" in sent[0]["subject"]
