"""Tests for POST /api/knowledge/text — direct text knowledge ingestion."""

import pytest
from fastapi.testclient import TestClient

from business_ai.auth import Principal, create_access_token
from business_ai.tenant import TenantConfig, TenantStatus


@pytest.fixture
def tenant_and_tokens(services):
    tenant = TenantConfig(
        tenant_id="indore-spices",
        business_name="Indore Spices Cafe",
        owner_email="owner@indorespices.example",
        status=TenantStatus.ACTIVE,
    )
    services.tenant_registry.register(tenant)

    other = TenantConfig(
        tenant_id="other-shop",
        business_name="Other Shop",
        owner_email="other@shop.example",
        status=TenantStatus.ACTIVE,
    )
    services.tenant_registry.register(other)

    settings = services.settings
    owner_token = create_access_token(
        Principal.owner("owner@indorespices.example", tenant.tenant_id),
        settings,
    )
    manager_token = create_access_token(
        Principal.manager("mgr@indorespices.example", tenant.tenant_id),
        settings,
    )
    staff_token = create_access_token(
        Principal.staff("staff@indorespices.example", tenant.tenant_id),
        settings,
    )
    other_token = create_access_token(
        Principal.owner("owner@other.example", other.tenant_id),
        settings,
    )

    return {
        "tenant_id": tenant.tenant_id,
        "other_tenant_id": other.tenant_id,
        "owner_token": owner_token,
        "manager_token": manager_token,
        "staff_token": staff_token,
        "other_token": other_token,
    }


def test_ingest_text_success(client, tenant_and_tokens):
    t = tenant_and_tokens
    res = client.post(
        f"/api/knowledge/text?tenant_id={t['tenant_id']}",
        headers={"Authorization": f"Bearer {t['owner_token']}"},
        json={
            "title": "Breakfast Timings & Rules",
            "text": "We serve hot Poha and Jalebi daily from 7:00 AM to 11:30 AM. Outside food is strictly prohibited.",
        },
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert "source_id" in data
    assert data["source_id"].startswith("text_")
    assert data["chunks_indexed"] >= 1

    # Verify source was recorded
    src_res = client.get(
        f"/api/knowledge/sources?tenant_id={t['tenant_id']}",
        headers={"Authorization": f"Bearer {t['owner_token']}"},
    )
    assert src_res.status_code == 200
    sources = src_res.json()["sources"]
    matching = [s for s in sources if s["source_id"] == data["source_id"]]
    assert len(matching) == 1
    assert matching[0]["label"] == "Breakfast Timings & Rules"
    assert matching[0]["source_type"] == "text"


def test_ingest_text_empty_rejected(client, tenant_and_tokens):
    t = tenant_and_tokens
    res = client.post(
        f"/api/knowledge/text?tenant_id={t['tenant_id']}",
        headers={"Authorization": f"Bearer {t['owner_token']}"},
        json={"title": "Empty", "text": "   "},
    )
    assert res.status_code == 400


def test_ingest_text_staff_and_manager_forbidden(client, tenant_and_tokens):
    t = tenant_and_tokens
    payload = {"title": "Staff Note", "text": "Some text content here."}

    # Staff forbidden
    res = client.post(
        f"/api/knowledge/text?tenant_id={t['tenant_id']}",
        headers={"Authorization": f"Bearer {t['staff_token']}"},
        json=payload,
    )
    assert res.status_code == 403

    # Manager forbidden (manager cannot ingest knowledge)
    res = client.post(
        f"/api/knowledge/text?tenant_id={t['tenant_id']}",
        headers={"Authorization": f"Bearer {t['manager_token']}"},
        json=payload,
    )
    assert res.status_code == 403


def test_ingest_text_cross_tenant_forbidden(client, tenant_and_tokens):
    t = tenant_and_tokens
    res = client.post(
        f"/api/knowledge/text?tenant_id={t['tenant_id']}",
        headers={"Authorization": f"Bearer {t['other_token']}"},
        json={"title": "Attack", "text": "Malicious inject text."},
    )
    assert res.status_code == 403


def test_answer_grounded_from_pasted_text(client, tenant_and_tokens):
    t = tenant_and_tokens
    ingest_res = client.post(
        f"/api/knowledge/text?tenant_id={t['tenant_id']}",
        headers={"Authorization": f"Bearer {t['owner_token']}"},
        json={
            "title": "Specialty Drinks",
            "text": "Our signature drink is Indore Masala Chai brewed fresh with cardamom, ginger, and saffron.",
        },
    )
    assert ingest_res.status_code == 200

    ask_res = client.post(
        f"/api/ask?tenant_id={t['tenant_id']}",
        json={"query": "signature drink Indore Masala Chai"},
    )
    assert ask_res.status_code == 200
    answer_data = ask_res.json()
    assert answer_data["status"] == "answered"
    assert len(answer_data.get("citations_used", [])) > 0
