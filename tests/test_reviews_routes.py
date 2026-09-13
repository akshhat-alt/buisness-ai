"""HTTP-level tests for Phase 25's review routes: GET /api/reviews and
POST /api/reviews/manual. RBAC (owner+manager via VIEW_FINANCIALS, same
gate as POST /api/metrics), tenant isolation.
"""

from __future__ import annotations

from business_ai.auth import Principal, create_access_token


def test_get_reviews_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    services.review_store.record(tenant_id=tenant_id, platform="google", rating=4.5, review_count=100, source="google_places")

    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = client.get(f"/api/reviews?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"})
    assert r.status_code == 403

    r2 = client.get(f"/api/reviews?tenant_id={tenant_id}", headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["platforms"]["google"]["rating"] == 4.5


def test_get_reviews_empty_for_a_tenant_with_no_reviews(client, owner_session):
    headers, tenant_id = owner_session
    r = client.get(f"/api/reviews?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["platforms"] == {}


def test_get_reviews_is_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "reviews-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}
    r = client.get(f"/api/reviews?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_post_manual_review_records_a_snapshot(client, owner_session, services):
    headers, tenant_id = owner_session
    r = client.post(
        f"/api/reviews/manual?tenant_id={tenant_id}", headers=headers,
        json={"platform": "zomato", "rating": 4.3, "review_count": 128},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["platform"] == "zomato"
    assert body["rating"] == 4.3
    assert body["source"] == "manual"

    entries = services.audit_log.list_for_tenant(tenant_id, action="review_logged")
    assert len(entries) == 1


def test_post_manual_review_rejects_an_unknown_platform(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(
        f"/api/reviews/manual?tenant_id={tenant_id}", headers=headers,
        json={"platform": "yelp", "rating": 4.0},
    )
    assert r.status_code == 400


def test_post_manual_review_requires_owner_or_manager(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    r = client.post(
        f"/api/reviews/manual?tenant_id={tenant_id}", headers={"Authorization": f"Bearer {staff_token}"},
        json={"platform": "zomato", "rating": 4.0},
    )
    assert r.status_code == 403


def test_post_manual_review_rejects_a_rating_above_five(client, owner_session):
    headers, tenant_id = owner_session
    r = client.post(
        f"/api/reviews/manual?tenant_id={tenant_id}", headers=headers,
        json={"platform": "google", "rating": 6.0},
    )
    assert r.status_code == 422
