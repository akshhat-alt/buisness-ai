"""Employee feedback: plain-language concerns, complaints, and
suggestions about how the business runs, captured over WhatsApp and
classified by sentiment/theme/urgency (see generation.py's
classify_feedback_sentiment). Tenant-scoped exactly like leads.py.

Deliberately separate from AnalyticsStore's customer-conversation
sentiment (`shows_dissatisfaction`) — this is about the business's own
people and its internal processes, a different audience and a different
sensitivity level (see the RBAC note on FeedbackStore.summarize_by_theme
below: this is never exposed as a per-employee score).
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from pydantic import BaseModel

SENTIMENTS = ("positive", "neutral", "negative")
URGENCIES = ("low", "medium", "high")


class FeedbackItem(BaseModel):
    feedback_id: str
    tenant_id: str
    employee_id: str
    raw_text: str
    sentiment: str  # positive | neutral | negative
    theme: str  # a fixed taxonomy value — see generation.FEEDBACK_THEMES
    urgency: str  # low | medium | high
    root_cause_hint: str | None = None
    suggested_action: str | None = None
    source_message_id: str | None = None
    resolved: bool = False
    created_at: str


class ThemeSummary(BaseModel):
    theme: str
    count: int
    negative_count: int
    most_recent_at: str


class FeedbackStore:
    """Thread-safe SQLite store for classified employee feedback."""

    def __init__(self, db_path: Path | str = "data/feedback.db") -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback_items (
                    feedback_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    employee_id TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    theme TEXT NOT NULL,
                    urgency TEXT NOT NULL,
                    root_cause_hint TEXT,
                    suggested_action TEXT,
                    source_message_id TEXT,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_tenant ON feedback_items(tenant_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_tenant_theme ON feedback_items(tenant_id, theme)")
            conn.commit()

    def _row_to_item(self, row: sqlite3.Row) -> FeedbackItem:
        data = dict(row)
        data["resolved"] = bool(data["resolved"])
        return FeedbackItem(**data)

    def record(
        self,
        *,
        tenant_id: str,
        employee_id: str,
        raw_text: str,
        sentiment: str,
        theme: str,
        urgency: str,
        root_cause_hint: str | None = None,
        suggested_action: str | None = None,
        source_message_id: str | None = None,
    ) -> FeedbackItem:
        if not tenant_id:
            raise ValueError("tenant_id is required.")
        if sentiment not in SENTIMENTS:
            raise ValueError(f"Invalid sentiment '{sentiment}'.")
        if urgency not in URGENCIES:
            raise ValueError(f"Invalid urgency '{urgency}'.")
        if not raw_text or not raw_text.strip():
            raise ValueError("Feedback text is required.")

        item = FeedbackItem(
            feedback_id=f"fb_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            employee_id=employee_id,
            raw_text=raw_text.strip(),
            sentiment=sentiment,
            theme=theme,
            urgency=urgency,
            root_cause_hint=root_cause_hint,
            suggested_action=suggested_action,
            source_message_id=source_message_id,
            resolved=False,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO feedback_items (
                    feedback_id, tenant_id, employee_id, raw_text, sentiment, theme, urgency,
                    root_cause_hint, suggested_action, source_message_id, resolved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.feedback_id, item.tenant_id, item.employee_id, item.raw_text, item.sentiment,
                    item.theme, item.urgency, item.root_cause_hint, item.suggested_action,
                    item.source_message_id, int(item.resolved), item.created_at,
                ),
            )
            conn.commit()
        return item

    def get(self, tenant_id: str, feedback_id: str) -> FeedbackItem | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM feedback_items WHERE tenant_id = ? AND feedback_id = ?", (tenant_id, feedback_id)
            ).fetchone()
            return self._row_to_item(row) if row else None

    def list_for_tenant(
        self, tenant_id: str, *, since_iso: str | None = None, theme: str | None = None,
        sentiment: str | None = None, limit: int = 200,
    ) -> list[FeedbackItem]:
        query = "SELECT * FROM feedback_items WHERE tenant_id = ?"
        params: list[str] = [tenant_id]
        if since_iso:
            query += " AND created_at >= ?"
            params.append(since_iso)
        if theme:
            query += " AND theme = ?"
            params.append(theme)
        if sentiment:
            query += " AND sentiment = ?"
            params.append(sentiment)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(str(limit))
        with self._lock, self._db() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_item(r) for r in rows]

    def summarize_by_theme(self, tenant_id: str, *, since_iso: str | None = None) -> list[ThemeSummary]:
        """Aggregated by theme only — this is the one form feedback data
        is ever surfaced in for management review. Never per-employee: a
        recurring "software logs out" theme is actionable business
        intelligence, a per-person sentiment tally is not something this
        product does."""
        query = "SELECT theme, sentiment, created_at FROM feedback_items WHERE tenant_id = ?"
        params: list[str] = [tenant_id]
        if since_iso:
            query += " AND created_at >= ?"
            params.append(since_iso)
        with self._lock, self._db() as conn:
            rows = conn.execute(query, params).fetchall()

        by_theme: dict[str, dict] = {}
        for row in rows:
            entry = by_theme.setdefault(row["theme"], {"count": 0, "negative_count": 0, "most_recent_at": ""})
            entry["count"] += 1
            if row["sentiment"] == "negative":
                entry["negative_count"] += 1
            if row["created_at"] > entry["most_recent_at"]:
                entry["most_recent_at"] = row["created_at"]

        summaries = [
            ThemeSummary(theme=theme, count=v["count"], negative_count=v["negative_count"], most_recent_at=v["most_recent_at"])
            for theme, v in by_theme.items()
        ]
        summaries.sort(key=lambda s: s.count, reverse=True)
        return summaries

    def list_unresolved_negative(self, tenant_id: str, *, limit: int = 50) -> list[FeedbackItem]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM feedback_items
                WHERE tenant_id = ? AND sentiment = 'negative' AND resolved = 0
                ORDER BY created_at DESC LIMIT ?
                """,
                (tenant_id, limit),
            ).fetchall()
            return [self._row_to_item(r) for r in rows]

    def mark_resolved(self, tenant_id: str, feedback_id: str) -> FeedbackItem | None:
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "UPDATE feedback_items SET resolved = 1 WHERE tenant_id = ? AND feedback_id = ?",
                (tenant_id, feedback_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(tenant_id, feedback_id)
