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
from tests.conftest import FakeGenerator


@pytest.fixture()
def services_with_places(tmp_path, settings):
    settings_with_key = dataclasses.replace(settings, google_places_api_key="test-google-places-key")
    svc = Services(settings_with_key, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
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
