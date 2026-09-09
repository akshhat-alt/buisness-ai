"""Tests for the V1.1 additions: the knowledge-gap closer (draft + publish)
and the owner digest email."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from business_ai.analytics import AnalyticsStore


def test_analytics_store_self_heals_a_pre_existing_database_missing_resolved_column(tmp_path: Path):
    """Regression test: an existing analytics.db created before the
    `resolved` column existed must not crash every gap-listing call with
    "no such column: resolved" — caught for real in dev against a stale
    local database. Simulate that by hand-creating the table in its
    original shape, then opening it with the current AnalyticsStore."""
    db_path = tmp_path / "analytics.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE turns (
            turn_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            query TEXT NOT NULL,
            answer_status TEXT NOT NULL,
            shows_buying_intent INTEGER NOT NULL DEFAULT 0,
            suggested_handoff INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO turns VALUES ('turn_old', 't1', 's1', 'pre-existing gap', 'insufficient_evidence', 0, 1, '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    store = AnalyticsStore(db_path)  # must not raise
    gaps = store.list_open_gaps("t1")
    assert len(gaps) == 1
    assert gaps[0].query == "pre-existing gap"

    store.mark_gap_resolved("t1", query="pre-existing gap")
    assert store.list_open_gaps("t1") == []


def _signup(client, business_name="Priya Salon", email="owner@example.com"):
    r = client.post(
        "/api/auth/signup",
        json={"email": email, "password": "secret123", "name": "Priya", "business_name": business_name},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['access_token']}"}, data["tenant_id"]


def _admin_headers(client, settings):
    r = client.post("/api/auth/login", json={"email": "admin", "password": settings.admin_secret})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _activate(client, admin_headers, tenant_id):
    r = client.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ gap closer


def test_gap_draft_and_publish_closes_the_loop(client, settings):
    headers, tenant_id = _signup(client)
    admin_headers = _admin_headers(client, settings)

    r = client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    assert r.status_code == 200, r.text
    _activate(client, admin_headers, tenant_id)

    # Ask something unrelated to the ingested content so it abstains and
    # becomes a logged gap.
    r = client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "do you offer haircuts for men"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "insufficient_evidence"

    r = client.get(f"/api/analytics/gaps?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    gaps = r.json()["gaps"]
    assert len(gaps) == 1
    turn_id = gaps[0]["turn_id"]
    assert gaps[0]["query"] == "do you offer haircuts for men"

    r = client.post(f"/api/knowledge/gaps/{turn_id}/draft?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["question"] == "do you offer haircuts for men"
    assert draft["draft_answer"]

    r = client.post(
        f"/api/knowledge/gaps/{turn_id}/publish?tenant_id={tenant_id}",
        json={"answer_text": "Yes — we offer haircuts for men starting at 300 INR."},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["chunks_indexed"] >= 1

    # The gap must disappear from the open-gaps list once published.
    r = client.get(f"/api/analytics/gaps?tenant_id={tenant_id}", headers=headers)
    assert r.json()["gaps"] == []

    r = client.get(f"/api/knowledge/sources?tenant_id={tenant_id}", headers=headers)
    sources = r.json()["sources"]
    assert any(s["source_type"] == "faq" for s in sources)

    # And the assistant should now answer the same question from the
    # newly published FAQ entry.
    r = client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "do you offer haircuts for men"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "answered"


def test_gap_endpoints_are_tenant_isolated(client, settings):
    headers_a, tenant_a = _signup(client, business_name="Salon A", email="a@example.com")
    headers_b, tenant_b = _signup(client, business_name="Salon B", email="b@example.com")
    admin_headers = _admin_headers(client, settings)

    client.post(f"/api/knowledge/website?tenant_id={tenant_a}", json={"url": "https://example.com"}, headers=headers_a)
    _activate(client, admin_headers, tenant_a)
    r = client.post(f"/api/ask?tenant_id={tenant_a}", json={"query": "a secret gap question"}, headers=headers_a)
    turn_id = None
    gaps = client.get(f"/api/analytics/gaps?tenant_id={tenant_a}", headers=headers_a).json()["gaps"]
    assert len(gaps) == 1
    turn_id = gaps[0]["turn_id"]

    # Tenant B must not be able to draft/publish against tenant A's gap.
    # Role/tenant authorization passes for tenant B acting on tenant_b
    # (that's their own business), so the isolation has to come from the
    # gap lookup itself being tenant-scoped: tenant A's turn_id simply
    # doesn't exist within tenant B's data, hence 404 rather than a 403
    # that would confirm the ID refers to something real elsewhere.
    r = client.post(f"/api/knowledge/gaps/{turn_id}/draft?tenant_id={tenant_b}", headers=headers_b)
    assert r.status_code == 404, r.text

    # Tenant A itself gets a 404 for a turn_id that doesn't belong to it / doesn't exist.
    r = client.post(f"/api/knowledge/gaps/nonexistent/draft?tenant_id={tenant_a}", headers=headers_a)
    assert r.status_code == 404


def test_duplicate_gap_question_resolves_together(client, settings):
    headers, tenant_id = _signup(client)
    admin_headers = _admin_headers(client, settings)
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    _activate(client, admin_headers, tenant_id)

    # Ask the identical unanswerable question twice (two separate turns).
    client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "do you do weddings"}, headers=headers)
    client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "do you do weddings"}, headers=headers)

    gaps = client.get(f"/api/analytics/gaps?tenant_id={tenant_id}", headers=headers).json()["gaps"]
    assert len(gaps) == 1  # deduped by question text, not one row per turn
    turn_id = gaps[0]["turn_id"]

    r = client.post(
        f"/api/knowledge/gaps/{turn_id}/publish?tenant_id={tenant_id}",
        json={"answer_text": "Yes, we do weddings — book 2 weeks ahead."},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    assert client.get(f"/api/analytics/gaps?tenant_id={tenant_id}", headers=headers).json()["gaps"] == []


# ------------------------------------------------------------------ owner digest


def test_digest_run_requires_platform_admin(client):
    headers, _ = _signup(client)
    r = client.post("/api/v1/admin/digest/run", headers=headers)
    assert r.status_code == 403


def test_digest_run_skips_gracefully_when_email_not_configured(client, settings):
    admin_headers = _admin_headers(client, settings)
    r = client.post("/api/v1/admin/digest/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["sent"] == []
    assert "not configured" in data["note"]


def test_digest_run_sends_only_to_active_tenants_with_activity(client_with_email, services_with_email):
    settings = services_with_email.settings
    admin_headers = _admin_headers(client_with_email, settings)

    # Tenant A: active, has a lead -> should get a digest.
    headers_a, tenant_a = _signup(client_with_email, business_name="Active With Activity", email="a@example.com")
    client_with_email.post(f"/api/knowledge/website?tenant_id={tenant_a}", json={"url": "https://example.com"}, headers=headers_a)
    _activate(client_with_email, admin_headers, tenant_a)
    r = client_with_email.post(f"/api/leads?tenant_id={tenant_a}", json={"session_id": "s1", "phone": "9876543210"})
    assert r.status_code == 200, r.text

    # Tenant B: active, but zero activity -> should be skipped, not spammed.
    headers_b, tenant_b = _signup(client_with_email, business_name="Active No Activity", email="b@example.com")
    client_with_email.post(f"/api/knowledge/website?tenant_id={tenant_b}", json={"url": "https://example.com"}, headers=headers_b)
    _activate(client_with_email, admin_headers, tenant_b)

    # Tenant C: never activated -> should be skipped regardless of activity.
    headers_c, tenant_c = _signup(client_with_email, business_name="Still Provisioning", email="c@example.com")

    r = client_with_email.post("/api/v1/admin/digest/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["sent"] == [tenant_a]
    skipped_ids = {s["tenant_id"] for s in result["skipped"]}
    assert tenant_b in skipped_ids
    assert tenant_c in skipped_ids
    assert result["failed"] == []

    sent_emails = services_with_email.fake_email_sender.sent
    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == "a@example.com"
    assert "1 new lead" in sent_emails[0]["subject"]


def test_digest_run_includes_the_action_brief_when_generated(client_with_email, services_with_email):
    from tests.conftest import FakeGenerator

    settings = services_with_email.settings
    admin_headers = _admin_headers(client_with_email, settings)

    headers, tenant_id = _signup(client_with_email)
    client_with_email.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    _activate(client_with_email, admin_headers, tenant_id)
    client_with_email.post(f"/api/leads?tenant_id={tenant_id}", json={"session_id": "s1", "phone": "9876543210"})

    services_with_email.generator = lambda: FakeGenerator(
        action_brief_items=["3 customers asked about weekend hours — consider opening Saturdays."]
    )

    r = client_with_email.post("/api/v1/admin/digest/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["sent"] == [tenant_id]

    sent = services_with_email.fake_email_sender.sent
    assert len(sent) == 1
    assert "What to do about it" in sent[0]["html_body"]
    assert "weekend hours" in sent[0]["html_body"]
