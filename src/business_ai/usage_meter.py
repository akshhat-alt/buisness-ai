"""Phase 1 monetization infrastructure — durable, per-tenant, per-period
usage counters (e.g. "how many AI messages did tenant X send this
month"). Same SqliteStore pattern as every other store in this codebase
(shifts.py, reviews.py, etc.) — one flat table, tenant-scoped.

Deliberately NOT usage_limiter.py's SQLite quota table: that one tracks
a DECREMENTING per-session "how many questions are left" countdown for
real-time rate limiting, reset by re-inserting a fresh row, with no
concept of a billing period. This store is the opposite shape — a
plain, cumulative, incrementing COUNT per (tenant, period, metric),
kept forever (or until a future retention policy decides otherwise),
answering "how much did tenant X actually use this month" for billing
and dashboard display. Neither store should be made to do the other's
job.

Phase 1 scope is intentionally just counting and exposing usage — no
quota enforcement/blocking here. That's a deliberate later decision
(the Master Plan's own Phase 1 write-up), not an oversight.
"""

from __future__ import annotations

import time
from pathlib import Path

from business_ai.storage import SqliteStore


def current_period() -> str:
    """The current calendar-month period key, e.g. "2026-09". Usage
    resets naturally every month by simply starting a new period key —
    no cron, no explicit rollover step, nothing to forget to run."""
    return time.strftime("%Y-%m", time.gmtime())


class UsageMeterStore(SqliteStore):
    """Thread-safe SQLite store for per-tenant, per-period usage counters."""

    def __init__(self, db_path: Path | str = "data/usage_meter.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_counters (
                    tenant_id TEXT NOT NULL,
                    period TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, period, metric)
                )
                """
            )
            conn.commit()

    def increment(self, *, tenant_id: str, metric: str, period: str | None = None, by: int = 1) -> None:
        """Best-effort accounting, not a gate: callers should never let a
        failure here block the real work (an AI response, a WhatsApp
        send) that already happened — see the call sites in
        routers/admin_bot.py for the try/except wrapping this."""
        period = period or current_period()
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO usage_counters (tenant_id, period, metric, count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id, period, metric) DO UPDATE SET count = count + excluded.count
                """,
                (tenant_id, period, metric, by),
            )
            conn.commit()

    def get_usage(self, *, tenant_id: str, period: str | None = None) -> dict[str, int]:
        period = period or current_period()
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT metric, count FROM usage_counters WHERE tenant_id = ? AND period = ?",
                (tenant_id, period),
            ).fetchall()
            return {row["metric"]: row["count"] for row in rows}
