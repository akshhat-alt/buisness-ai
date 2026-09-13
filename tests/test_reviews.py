"""Tests for Phase 25's review aggregation (reviews.py): ReviewStore
(tenant-scoped SQLite, same shape as shifts.py/tasks.py),
GooglePlacesReviewClient (mocked urlopen — never a real network call),
and the WhatsApp render function.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError

from business_ai.reviews import (
    GooglePlacesError,
    GooglePlacesReviewClient,
    ReviewStore,
    render_reviews_whatsapp,
)

TENANT = "cafe-a"


# ------------------------------------------------------------------ ReviewStore


def test_record_and_list_for_tenant(tmp_path):
    store = ReviewStore(tmp_path / "reviews.db")
    store.record(tenant_id=TENANT, platform="google", rating=4.5, review_count=100, source="google_places")
    store.record(tenant_id=TENANT, platform="zomato", rating=4.2, review_count=50, source="manual", recorded_by_employee_id="emp1")

    rows = store.list_for_tenant(TENANT)
    assert len(rows) == 2
    assert {r.platform for r in rows} == {"google", "zomato"}


def test_list_for_tenant_is_tenant_isolated(tmp_path):
    store = ReviewStore(tmp_path / "reviews.db")
    store.record(tenant_id=TENANT, platform="google", rating=4.5, source="manual")
    store.record(tenant_id="other-tenant", platform="google", rating=3.0, source="manual")
    assert len(store.list_for_tenant(TENANT)) == 1


def test_list_for_tenant_filters_by_platform(tmp_path):
    store = ReviewStore(tmp_path / "reviews.db")
    store.record(tenant_id=TENANT, platform="google", rating=4.5, source="manual")
    store.record(tenant_id=TENANT, platform="zomato", rating=4.0, source="manual")
    assert len(store.list_for_tenant(TENANT, platform="google")) == 1


def test_latest_by_platform_returns_only_the_most_recent_snapshot(tmp_path):
    store = ReviewStore(tmp_path / "reviews.db")
    store.record(tenant_id=TENANT, platform="google", rating=4.0, source="manual")
    store.record(tenant_id=TENANT, platform="google", rating=4.5, source="google_places")  # a later snapshot

    latest = store.latest_by_platform(TENANT)
    assert len(latest) == 1
    assert latest["google"].rating == 4.5
    assert latest["google"].source == "google_places"


def test_delete_for_tenant_removes_only_that_tenants_rows(tmp_path):
    store = ReviewStore(tmp_path / "reviews.db")
    store.record(tenant_id=TENANT, platform="google", rating=4.5, source="manual")
    store.record(tenant_id="other-tenant", platform="google", rating=3.0, source="manual")
    deleted = store.delete_for_tenant(TENANT)
    assert deleted == 1
    assert store.list_for_tenant(TENANT) == []
    assert len(store.list_for_tenant("other-tenant")) == 1


# ------------------------------------------------------------------ GooglePlacesReviewClient


class _FakeHTTPResponse:
    def __init__(self, body: dict) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_rating_returns_rating_and_count(monkeypatch):
    client = GooglePlacesReviewClient(api_key="test-key")

    def fake_urlopen(request, timeout=15):
        return _FakeHTTPResponse({"status": "OK", "result": {"rating": 4.6, "user_ratings_total": 213}})

    monkeypatch.setattr("business_ai.reviews.urlopen", fake_urlopen)
    rating, count = client.fetch_rating("ChIJ_test")
    assert rating == 4.6
    assert count == 213


def test_fetch_rating_raises_on_non_ok_status(monkeypatch):
    client = GooglePlacesReviewClient(api_key="test-key")

    def fake_urlopen(request, timeout=15):
        return _FakeHTTPResponse({"status": "NOT_FOUND"})

    monkeypatch.setattr("business_ai.reviews.urlopen", fake_urlopen)
    try:
        client.fetch_rating("ChIJ_bad")
        assert False, "expected GooglePlacesError"
    except GooglePlacesError as exc:
        assert "NOT_FOUND" in str(exc)


def test_fetch_rating_requires_an_api_key():
    client = GooglePlacesReviewClient(api_key="")
    try:
        client.fetch_rating("ChIJ_test")
        assert False, "expected GooglePlacesError"
    except GooglePlacesError as exc:
        assert "API key" in str(exc)


def test_fetch_rating_requires_a_place_id():
    client = GooglePlacesReviewClient(api_key="test-key")
    try:
        client.fetch_rating("")
        assert False, "expected GooglePlacesError"
    except GooglePlacesError as exc:
        assert "Place ID" in str(exc)


def test_fetch_rating_wraps_http_error(monkeypatch):
    client = GooglePlacesReviewClient(api_key="test-key")

    def fake_urlopen(request, timeout=15):
        raise HTTPError(request.full_url, 403, "Forbidden", None, None)

    monkeypatch.setattr("business_ai.reviews.urlopen", fake_urlopen)
    try:
        client.fetch_rating("ChIJ_test")
        assert False, "expected GooglePlacesError"
    except GooglePlacesError as exc:
        assert "403" in str(exc)


# ------------------------------------------------------------------ WhatsApp render


def test_render_reviews_whatsapp_handles_no_data():
    assert "No reviews logged yet" in render_reviews_whatsapp({})


def test_render_reviews_whatsapp_shows_every_platform(tmp_path):
    store = ReviewStore(tmp_path / "reviews.db")
    store.record(tenant_id=TENANT, platform="google", rating=4.5, review_count=100, source="google_places")
    store.record(tenant_id=TENANT, platform="zomato", rating=4.1, review_count=42, source="manual")

    rendered = render_reviews_whatsapp(store.latest_by_platform(TENANT))
    assert "Google" in rendered and "4.5" in rendered
    assert "Zomato" in rendered and "4.1" in rendered
    assert "auto-synced" in rendered
    assert "manually logged" in rendered
