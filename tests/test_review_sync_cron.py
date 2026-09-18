"""Tests for Phase 25's Google Places review-sync cron:
POST /api/v1/admin/review-sync/run. Mocks GooglePlacesReviewClient.fetch_rating
directly — never a real network call — matching this suite's "no OpenAI/
external API cost" convention.
"""

from __future__ import annotations

import dataclasses

import pytest

from business_ai.app import Services, create_app
from business_ai.retrieval import HashEmbeddingProvider
from business_ai.reviews import GooglePlacesError
from tests.conftest import FakeEmailSender, FakeGenerator


@pytest.fixture()
def services_with_places(tmp_path, settings):
    settings_with_key = dataclasses.replace(
        settings, google_places_api_key="test-google-places-key",
        resend_api_key="re_test_key", digest_from_email="digest@business-ai.example",
        public_base_url="https://app.example.com",
    )
    svc = Services(settings_with_key, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    fake_sender = FakeEmailSender()
    svc.email_sender = lambda: fake_sender
    svc.fake_email_sender = fake_sender
    yield svc
    svc.vector_store.close()


@pytest.fixture()
def client_places(services_with_places):
    from fastapi.testclient import TestClient

    return TestClient(create_app(services_with_places))


def test_review_sync_requires_platform_admin(client_places, services_with_places):
    r = client_places.post(
        "/api/auth/signup",
        json={"email": "places-a@example.com", "password": "secret123", "name": "A", "business_name": "Places A"},
    )
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    resp = client_places.post("/api/v1/admin/review-sync/run", headers=headers)
    assert resp.status_code == 403


def test_review_sync_skips_when_api_key_unconfigured(client, admin_headers):
    r = client.post("/api/v1/admin/review-sync/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synced"] == []
    assert body["skipped"][0]["reason"] == "GOOGLE_PLACES_API_KEY not configured on this platform"


def test_review_sync_skips_tenant_without_place_id(client_places, services_with_places, monkeypatch):
    signup = client_places.post(
        "/api/auth/signup",
        json={"email": "places-b@example.com", "password": "secret123", "name": "B", "business_name": "Places B"},
    )
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    tenant_id = signup.json()["tenant_id"]
    client_places.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    admin_login = client_places.post("/api/auth/login", json={"email": "admin", "password": services_with_places.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    client_places.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)

    r = client_places.post("/api/v1/admin/review-sync/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert any(s["tenant_id"] == tenant_id and s["reason"] == "no google_place_id configured" for s in r.json()["skipped"])


def test_review_sync_records_a_snapshot_for_a_configured_tenant(client_places, services_with_places, monkeypatch):
    signup = client_places.post(
        "/api/auth/signup",
        json={"email": "places-c@example.com", "password": "secret123", "name": "C", "business_name": "Places C"},
    )
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    tenant_id = signup.json()["tenant_id"]
    client_places.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    client_places.put(f"/api/tenant?tenant_id={tenant_id}", json={"google_place_id": "ChIJ_test_place_id"}, headers=headers)
    admin_login = client_places.post("/api/auth/login", json={"email": "admin", "password": services_with_places.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    client_places.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)

    from business_ai.reviews import GooglePlacesReviewClient

    monkeypatch.setattr(GooglePlacesReviewClient, "fetch_rating", lambda self, place_id: (4.6, 213))

    r = client_places.post("/api/v1/admin/review-sync/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert tenant_id in r.json()["synced"]

    latest = services_with_places.review_store.latest_by_platform(tenant_id)
    assert latest["google"].rating == 4.6
    assert latest["google"].review_count == 213
    assert latest["google"].source == "google_places"


def test_review_sync_skips_a_tenant_on_google_places_error(client_places, services_with_places, monkeypatch):
    signup = client_places.post(
        "/api/auth/signup",
        json={"email": "places-d@example.com", "password": "secret123", "name": "D", "business_name": "Places D"},
    )
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    tenant_id = signup.json()["tenant_id"]
    client_places.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    client_places.put(f"/api/tenant?tenant_id={tenant_id}", json={"google_place_id": "ChIJ_bad_place_id"}, headers=headers)
    admin_login = client_places.post("/api/auth/login", json={"email": "admin", "password": services_with_places.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    client_places.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)

    from business_ai.reviews import GooglePlacesReviewClient

    def _raise(self, place_id):
        raise GooglePlacesError("Google Places API returned status 'NOT_FOUND'")

    monkeypatch.setattr(GooglePlacesReviewClient, "fetch_rating", _raise)

    r = client_places.post("/api/v1/admin/review-sync/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert tenant_id not in r.json()["synced"]
    assert any(s["tenant_id"] == tenant_id and "NOT_FOUND" in s["reason"] for s in r.json()["skipped"])


def _activated_tenant_with_place_id(client_places, services_with_places, *, email: str, place_id: str):
    signup = client_places.post(
        "/api/auth/signup",
        json={"email": email, "password": "secret123", "name": "Owner", "business_name": "Rating Test Biz"},
    )
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    tenant_id = signup.json()["tenant_id"]
    client_places.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    client_places.put(f"/api/tenant?tenant_id={tenant_id}", json={"google_place_id": place_id}, headers=headers)
    admin_login = client_places.post("/api/auth/login", json={"email": "admin", "password": services_with_places.settings.admin_secret})
    admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
    client_places.post(f"/api/v1/admin/tenants/{tenant_id}/activate", headers=admin_headers)
    return headers, tenant_id, admin_headers


def test_review_sync_alerts_owner_on_meaningful_rating_drop(client_places, services_with_places, monkeypatch):
    headers, tenant_id, admin_headers = _activated_tenant_with_place_id(
        client_places, services_with_places, email="rating-drop@example.com", place_id="ChIJ_drop",
    )
    from business_ai.reviews import GooglePlacesReviewClient

    ratings = iter([4.8, 4.5])  # a 0.3-star drop, above the 0.2 threshold
    monkeypatch.setattr(GooglePlacesReviewClient, "fetch_rating", lambda self, place_id: (next(ratings), 100))

    client_places.post("/api/v1/admin/review-sync/run", headers=admin_headers)  # first sync: records 4.8, no "previous" yet
    r = client_places.post("/api/v1/admin/review-sync/run", headers=admin_headers)  # second sync: 4.5, drop from 4.8
    assert r.status_code == 200, r.text
    assert tenant_id in r.json()["synced"]

    alerts = [e for e in services_with_places.fake_email_sender.sent if "rating dropped" in e["subject"].lower()]
    assert len(alerts) == 1
    assert "4.8" in alerts[0]["html_body"] and "4.5" in alerts[0]["html_body"]


def test_review_sync_does_not_alert_on_small_fluctuation(client_places, services_with_places, monkeypatch):
    headers, tenant_id, admin_headers = _activated_tenant_with_place_id(
        client_places, services_with_places, email="rating-stable@example.com", place_id="ChIJ_stable",
    )
    from business_ai.reviews import GooglePlacesReviewClient

    ratings = iter([4.8, 4.7])  # a 0.1-star drop, below the 0.2 threshold
    monkeypatch.setattr(GooglePlacesReviewClient, "fetch_rating", lambda self, place_id: (next(ratings), 100))

    client_places.post("/api/v1/admin/review-sync/run", headers=admin_headers)
    client_places.post("/api/v1/admin/review-sync/run", headers=admin_headers)

    alerts = [e for e in services_with_places.fake_email_sender.sent if "rating dropped" in e["subject"].lower()]
    assert alerts == []


def test_review_sync_does_not_alert_on_first_ever_sync(client_places, services_with_places, monkeypatch):
    """No "previous" snapshot exists yet, so there's nothing to compare
    against — must not crash and must not alert."""
    headers, tenant_id, admin_headers = _activated_tenant_with_place_id(
        client_places, services_with_places, email="rating-first@example.com", place_id="ChIJ_first",
    )
    from business_ai.reviews import GooglePlacesReviewClient

    monkeypatch.setattr(GooglePlacesReviewClient, "fetch_rating", lambda self, place_id: (3.0, 5))

    r = client_places.post("/api/v1/admin/review-sync/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    alerts = [e for e in services_with_places.fake_email_sender.sent if "rating dropped" in e["subject"].lower()]
    assert alerts == []


def test_review_sync_does_not_alert_on_rating_increase(client_places, services_with_places, monkeypatch):
    headers, tenant_id, admin_headers = _activated_tenant_with_place_id(
        client_places, services_with_places, email="rating-up@example.com", place_id="ChIJ_up",
    )
    from business_ai.reviews import GooglePlacesReviewClient

    ratings = iter([4.0, 4.5])
    monkeypatch.setattr(GooglePlacesReviewClient, "fetch_rating", lambda self, place_id: (next(ratings), 50))

    client_places.post("/api/v1/admin/review-sync/run", headers=admin_headers)
    client_places.post("/api/v1/admin/review-sync/run", headers=admin_headers)

    alerts = [e for e in services_with_places.fake_email_sender.sent if "rating dropped" in e["subject"].lower()]
    assert alerts == []
