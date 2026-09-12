"""SQLite-backed atomic usage limiter: quota, concurrency, and rate limiting.

Directly adapted from Shri AI's runtime/usage_limiter.py (SQLiteUsageLimiter),
including the fix applied there in the same session this was written: a
fresh session for a real ACTIVE tenant gets the tenant's production quota
by default, not a small demo-tier cap. Redis/distributed mode is
deliberately not ported — Business AI runs as a single Railway instance for
the MVP; add it back only if/when there's more than one instance to
coordinate across.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from enum import Enum

from business_ai.config import Settings


class AccessDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class ReservationResult:
    decision: AccessDecision
    reservation_id: str | None = None
    reason: str | None = None
    questions_remaining: int | None = None
    questions_limit: int | None = None


class UsageLimiter:
    """Atomic SQLite usage limiter.

    Guarantees concurrency-safe quota decrements using single-statement
    atomic SQL updates before any paid external API call is made.
    """

    def __init__(self, db_path: Path | str, settings: Settings) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self._init_db()

    def _resolve_quota_limit(self, quota_override: int | None) -> int:
        return quota_override if quota_override is not None else self.settings.active_tenant_quota

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_usage (
                    session_key TEXT PRIMARY KEY,
                    remaining_quota INTEGER NOT NULL,
                    quota_limit INTEGER NOT NULL,
                    active_requests INTEGER NOT NULL DEFAULT 0,
                    window_start_time REAL NOT NULL,
                    window_request_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.commit()

    def _key(self, tenant_id: str, session_id: str) -> str:
        return f"{tenant_id}:{session_id.strip()}"

    def check_and_reserve(
        self,
        tenant_id: str,
        session_id: str,
        reservation_id: str,
        *,
        quota_override: int | None = None,
    ) -> ReservationResult:
        if not self.settings.enabled:
            return ReservationResult(
                decision=AccessDecision.DENY,
                reason="KILL_SWITCH_ACTIVE: AI operations are currently paused by the administrator.",
            )

        target_key = self._key(tenant_id, session_id)
        quota_limit = self._resolve_quota_limit(quota_override)
        now = time.time()

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO session_usage (
                    session_key, remaining_quota, quota_limit,
                    active_requests, window_start_time, window_request_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 0, ?, 0, ?, ?)
                ON CONFLICT(session_key) DO NOTHING;
                """,
                (target_key, quota_limit, quota_limit, now, now, now),
            )

            row = conn.execute("SELECT * FROM session_usage WHERE session_key = ?;", (target_key,)).fetchone()
            if not row:
                return ReservationResult(decision=AccessDecision.DENY, reason="UNKNOWN_SESSION")

            rem_quota = row["remaining_quota"]
            act_reqs = row["active_requests"]
            win_start = row["window_start_time"]
            win_count = row["window_request_count"]

            if act_reqs >= self.settings.max_concurrent_requests:
                return ReservationResult(
                    decision=AccessDecision.RATE_LIMITED,
                    reason="CONCURRENT_REQUEST_LIMIT: Another question is already being answered in this session.",
                    questions_remaining=rem_quota,
                    questions_limit=row["quota_limit"],
                )

            if now - win_start > 60.0:
                win_start = now
                win_count = 0

            if win_count >= self.settings.requests_per_minute:
                return ReservationResult(
                    decision=AccessDecision.RATE_LIMITED,
                    reason=f"RATE_LIMIT_EXCEEDED: Maximum {self.settings.requests_per_minute} questions per minute allowed.",
                    questions_remaining=rem_quota,
                    questions_limit=row["quota_limit"],
                )

            if rem_quota <= 0:
                return ReservationResult(
                    decision=AccessDecision.DENY,
                    reason="QUESTION_LIMIT_REACHED: This session has reached its question limit.",
                    questions_remaining=0,
                    questions_limit=row["quota_limit"],
                )

            cur = conn.execute(
                """
                UPDATE session_usage
                SET remaining_quota = remaining_quota - 1,
                    active_requests = active_requests + 1,
                    window_start_time = ?,
                    window_request_count = window_request_count + 1,
                    updated_at = ?
                WHERE session_key = ? AND remaining_quota > 0 AND active_requests < ?;
                """,
                (win_start, now, target_key, self.settings.max_concurrent_requests),
            )

            if cur.rowcount != 1:
                return ReservationResult(
                    decision=AccessDecision.DENY,
                    reason="QUESTION_LIMIT_REACHED: Quota exhausted or a parallel request is in progress.",
                    questions_remaining=0,
                    questions_limit=row["quota_limit"],
                )

            conn.execute(
                "INSERT INTO usage_reservations (reservation_id, session_key, status, created_at, updated_at) "
                "VALUES (?, ?, 'reserved', ?, ?);",
                (reservation_id, target_key, now, now),
            )
            conn.commit()

            return ReservationResult(
                decision=AccessDecision.ALLOW,
                reservation_id=reservation_id,
                questions_remaining=rem_quota - 1,
                questions_limit=row["quota_limit"],
            )

    def release_reservation(self, reservation_id: str) -> None:
        """Restore quota for an abstention/error — no answer was actually delivered."""
        now = time.time()
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT session_key, status FROM usage_reservations WHERE reservation_id = ?;", (reservation_id,)
            ).fetchone()
            if not row or row["status"] != "reserved":
                return
            session_key = row["session_key"]
            conn.execute(
                "UPDATE usage_reservations SET status = 'released', updated_at = ? WHERE reservation_id = ?;",
                (now, reservation_id),
            )
            conn.execute(
                """
                UPDATE session_usage
                SET remaining_quota = remaining_quota + 1,
                    active_requests = MAX(0, active_requests - 1),
                    updated_at = ?
                WHERE session_key = ?;
                """,
                (now, session_key),
            )
            conn.commit()

    def finalize_reservation(self, reservation_id: str) -> None:
        """Consume the reserved quota slot — a real answer was delivered."""
        now = time.time()
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT session_key, status FROM usage_reservations WHERE reservation_id = ?;", (reservation_id,)
            ).fetchone()
            if not row or row["status"] != "reserved":
                return
            session_key = row["session_key"]
            conn.execute(
                "UPDATE usage_reservations SET status = 'consumed', updated_at = ? WHERE reservation_id = ?;",
                (now, reservation_id),
            )
            conn.execute(
                "UPDATE session_usage SET active_requests = MAX(0, active_requests - 1), updated_at = ? WHERE session_key = ?;",
                (now, session_key),
            )
            conn.commit()

    def get_session_status(self, tenant_id: str, session_id: str, *, quota_override: int | None = None) -> dict:
        target_key = self._key(tenant_id, session_id)
        quota_limit = self._resolve_quota_limit(quota_override)
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT remaining_quota, quota_limit FROM session_usage WHERE session_key = ?;", (target_key,)
            ).fetchone()
            if not row:
                return {"questions_remaining": quota_limit, "questions_limit": quota_limit}
            return {"questions_remaining": row["remaining_quota"], "questions_limit": row["quota_limit"]}

    def delete_for_tenant(self, tenant_id: str) -> int:
        """Phase 9 tenant data deletion. session_usage/usage_reservations
        key on `f"{tenant_id}:{session_id}"`, not a separate tenant_id
        column (see `_key`) — matched here with a LIKE prefix instead."""
        prefix = f"{tenant_id}:%"
        with self._get_connection() as conn:
            keys = [r["session_key"] for r in conn.execute(
                "SELECT session_key FROM session_usage WHERE session_key LIKE ?;", (prefix,)
            ).fetchall()]
            cur = conn.execute("DELETE FROM session_usage WHERE session_key LIKE ?;", (prefix,))
            deleted = cur.rowcount
            if keys:
                placeholders = ",".join("?" for _ in keys)
                conn.execute(f"DELETE FROM usage_reservations WHERE session_key IN ({placeholders});", keys)
            conn.commit()
            return deleted
