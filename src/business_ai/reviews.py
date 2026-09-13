"""Review aggregation (Phase 25 — Perception & Input Expansion).

One store, two write paths: an automated Google Places pull
(`source="google_places"`) and a manual paste-in for platforms with no
public merchant review-pull API yet — Zomato and Swiggy, confirmed
during this phase's own research to have no such API a small business
could reasonably use (`source="manual"`). Each row is a POINT-IN-TIME
snapshot per platform, not a summable flow — this is genuinely new,
unlike menu_engineering.py/customer_intelligence.py, since nothing in
this codebase tracked review ratings before this. Same tenant-scoped
SqliteStore shape as tasks.py/shifts.py.

Uses stdlib urllib for the Google Places call, the same "one HTTP call,
no SDK dependency justified" convention as email_sender.py/whatsapp.py/
payments.py — Google Places is a platform-level credential (one Google
Cloud API key, billed to the platform operator, the same shape as
WHATSAPP_APP_SECRET) while each tenant brings their own `google_place_id`
(their own Google Business Profile) — asking every small business owner
to create their own Google Cloud project would be a much higher-friction
ask than WhatsApp's Meta Developer flow, so this deliberately does NOT
follow the "bring your own credential" shape Razorpay/WhatsApp use.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel

from business_ai.storage import SqliteStore

KNOWN_PLATFORMS = frozenset({"google", "zomato", "swiggy"})

_GOOGLE_PLACES_HOST = "https://maps.googleapis.com"


class GooglePlacesError(Exception):
    """Raised when a Google Places rating lookup fails."""


class GooglePlacesReviewClient:
    """Thin wrapper over the Places API's Place Details endpoint,
    reading only the two fields this app needs: the aggregate rating and
    the total review count. Never fetches individual review text — this
    app has no use for it yet and Google's ToS restricts caching/
    redistributing full review content."""

    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key

    def fetch_rating(self, place_id: str) -> tuple[float, int | None]:
        if not self._api_key:
            raise GooglePlacesError("No Google Places API key configured for this platform.")
        if not place_id:
            raise GooglePlacesError("No Google Place ID configured for this business.")
        query = urlencode({"place_id": place_id, "fields": "rating,user_ratings_total", "key": self._api_key})
        url = f"{_GOOGLE_PLACES_HOST}/maps/api/place/details/json?{query}"
        try:
            with urlopen(Request(url, method="GET"), timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise GooglePlacesError(f"Google Places API error ({exc.code}): {detail}") from exc
        except URLError as exc:
            raise GooglePlacesError(f"Could not reach the Google Places API: {exc}") from exc

        if body.get("status") != "OK":
            raise GooglePlacesError(f"Google Places API returned status {body.get('status')!r}: {body.get('error_message', '')}")
        result = body.get("result") or {}
        rating = result.get("rating")
        if rating is None:
            raise GooglePlacesError("Google Places returned no rating for this place ID.")
        return float(rating), result.get("user_ratings_total")


class ReviewSnapshot(BaseModel):
    review_id: str
    tenant_id: str
    platform: str  # "google" | "zomato" | "swiggy"
    rating: float
    review_count: int | None = None
    source: str  # "google_places" | "manual"
    recorded_by_employee_id: str | None = None  # None for automated google_places pulls
    recorded_at: str


class ReviewStore(SqliteStore):
    """Thread-safe SQLite store for review-rating snapshots."""

    def __init__(self, db_path: Path | str = "data/reviews.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    rating REAL NOT NULL,
                    review_count INTEGER,
                    source TEXT NOT NULL,
                    recorded_by_employee_id TEXT,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_tenant ON reviews(tenant_id, platform, recorded_at)")
            conn.commit()

    def record(
        self, *, tenant_id: str, platform: str, rating: float, source: str,
        review_count: int | None = None, recorded_by_employee_id: str | None = None,
    ) -> ReviewSnapshot:
        snapshot = ReviewSnapshot(
            review_id=f"review_{secrets.token_hex(8)}", tenant_id=tenant_id, platform=platform, rating=rating,
            review_count=review_count, source=source, recorded_by_employee_id=recorded_by_employee_id,
            recorded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO reviews (review_id, tenant_id, platform, rating, review_count, source,
                    recorded_by_employee_id, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.review_id, snapshot.tenant_id, snapshot.platform, snapshot.rating,
                    snapshot.review_count, snapshot.source, snapshot.recorded_by_employee_id, snapshot.recorded_at,
                ),
            )
            conn.commit()
        return snapshot

    def list_for_tenant(self, tenant_id: str, *, platform: str | None = None, limit: int = 200) -> list[ReviewSnapshot]:
        clause = "tenant_id = ?"
        params: list = [tenant_id]
        if platform:
            clause += " AND platform = ?"
            params.append(platform)
        params.append(limit)
        with self._lock, self._db() as conn:
            # rowid DESC as a tiebreaker: recorded_at is second-resolution,
            # so two snapshots recorded within the same second would
            # otherwise sort arbitrarily relative to each other.
            rows = conn.execute(
                f"SELECT *, rowid FROM reviews WHERE {clause} ORDER BY recorded_at DESC, rowid DESC LIMIT ?", params
            ).fetchall()
            return [self._row_to_snapshot(r) for r in rows]

    def latest_by_platform(self, tenant_id: str) -> dict[str, ReviewSnapshot]:
        """The most recent snapshot per platform — what a "current
        rating" view should actually show, not a history dump."""
        latest: dict[str, ReviewSnapshot] = {}
        for snapshot in self.list_for_tenant(tenant_id, limit=1000):
            if snapshot.platform not in latest:  # rows already ordered newest-first
                latest[snapshot.platform] = snapshot
        return latest

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM reviews WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount

    def _row_to_snapshot(self, row: sqlite3.Row) -> ReviewSnapshot:
        return ReviewSnapshot(
            review_id=row["review_id"], tenant_id=row["tenant_id"], platform=row["platform"], rating=row["rating"],
            review_count=row["review_count"], source=row["source"],
            recorded_by_employee_id=row["recorded_by_employee_id"], recorded_at=row["recorded_at"],
        )


_PLATFORM_LABELS = {"google": "Google", "zomato": "Zomato", "swiggy": "Swiggy"}


def render_reviews_whatsapp(latest: dict[str, ReviewSnapshot]) -> str:
    if not latest:
        return (
            'No reviews logged yet. Connect a Google Place ID in settings for automatic tracking, '
            'or log one manually: "log review zomato 4.3 128".'
        )
    lines = ["⭐ Reviews:"]
    for platform in sorted(latest, key=lambda p: _PLATFORM_LABELS.get(p, p)):
        snapshot = latest[platform]
        count_note = f" ({snapshot.review_count} reviews)" if snapshot.review_count is not None else ""
        source_note = " — auto-synced" if snapshot.source == "google_places" else " — manually logged"
        lines.append(f"• {_PLATFORM_LABELS.get(platform, platform.title())}: {snapshot.rating:g}★{count_note}{source_note}")
    return "\n".join(lines)
