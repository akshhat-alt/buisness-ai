"""End-to-end HTTP flow: the same path a real business owner and their
customers actually take, exercised through FastAPI's TestClient with a
mocked generator and offline embeddings (no OpenAI cost, but every real
code path — routing, auth, tenant authorization, quota, ingestion,
analytics — runs for real)."""

from __future__ import annotations


def test_full_business_lifecycle(client, owner_session, admin_headers, activate_tenant):
    headers, tenant_id = owner_session

    r = client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["chunks_indexed"] >= 1

    # Owner must be able to see their own dashboard while still PROVISIONING.
    r = client.get(f"/api/knowledge/sources?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert len(r.json()["sources"]) == 1

    r = client.get(f"/api/analytics?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text

    # But a customer cannot query the assistant before activation.
    r = client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "hello"}, headers=headers)
    assert r.status_code == 403, r.text

    activate_tenant(tenant_id)

    r = client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "what is this domain for"}, headers=headers)
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["status"] == "answered"
    assert len(answer["citations_used"]) == 1
    assert answer["questions_limit"] == 500
    assert answer["questions_remaining"] == 499

    # Anonymous customers cannot ask without a session token.
    r = client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "hi"})
    assert r.status_code == 403

    r = client.get(f"/api/analytics?tenant_id={tenant_id}", headers=headers)
    analytics = r.json()
    assert analytics["total_questions"] == 1
    assert analytics["answered_count"] == 1

    r = client.post(f"/api/leads?tenant_id={tenant_id}", json={"session_id": "sess_x", "phone": "9876543210", "name": "Test Customer"})
    assert r.status_code == 200, r.text

    r = client.get(f"/api/leads?tenant_id={tenant_id}", headers=headers)
    assert len(r.json()["leads"]) == 1


def test_tenant_isolation_blocks_cross_business_access(client, owner_session):
    headers_a, tenant_a = owner_session

    r = client.post(
        "/api/auth/signup",
        json={"email": "other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.get(f"/api/leads?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403

    r = client.get(f"/api/analytics?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_abstains_without_relevant_knowledge(client, owner_session, admin_headers, activate_tenant):
    headers, tenant_id = owner_session

    r = client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    assert r.status_code == 200, r.text

    activate_tenant(tenant_id)

    # A query sharing no vocabulary with the ingested content: the
    # evidence gate must abstain rather than let a low-confidence match
    # through as if it were a real answer.
    r = client.post(f"/api/ask?tenant_id={tenant_id}", json={"query": "zzyxw qwerty flibbertigibbet"}, headers=headers)
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["status"] == "insufficient_evidence"
    assert answer["citations_used"] == []
    # An abstention must not consume the caller's quota.
    assert answer["questions_remaining"] == answer["questions_limit"]


def test_signup_rejects_duplicate_email(client):
    payload = {"email": "dup@example.com", "password": "secret123", "name": "A", "business_name": "Biz One"}
    r1 = client.post("/api/auth/signup", json=payload)
    assert r1.status_code == 200

    payload2 = dict(payload, business_name="Biz Two")
    r2 = client.post("/api/auth/signup", json=payload2)
    assert r2.status_code == 400


def test_admin_login_requires_correct_secret(client):
    r = client.post("/api/auth/login", json={"email": "admin", "password": "wrong-secret"})
    assert r.status_code == 401
