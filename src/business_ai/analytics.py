"""Conversation analytics: log every turn, summarize it for the business owner.

New in Business AI, though the instinct — log every query/answer, then
surface abstained/ungrounded questions as "knowledge gaps" — mirrors what
Shri AI's evaluation logger already does. Kept intentionally small: a
summary + a gaps list is what a business owner actually acts on; a full
BI dashboard is not an MVP feature.
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


class ConversationTurn(BaseModel):
    turn_id: str
    tenant_id: str
    session_id: str
    query: str
    answer_status: str  # "answered" | "insufficient_evidence" | "error"
    shows_buying_intent: bool = False
    suggested_handoff: bool = False
    shows_dissatisfaction: bool = False
    channel: str = "web"  # "web" | "whatsapp" — which surface the question came in on
    created_at: str


class AnalyticsSummary(BaseModel):
    total_questions: int
    answered_count: int
    abstention_count: int
    buying_intent_count: int
    handoff_suggested_count: int
    dissatisfaction_count: int
    recent_knowledge_gaps: list[str]  # recent questions the assistant couldn't answer


class KnowledgeGap(BaseModel):
    turn_id: str
    tenant_id: str
    query: str
    created_at: str


class AnalyticsStore:
    """Thread-safe SQLite store for conversation turn logging."""

    def __init__(self, db_path: Path | str = "data/analytics.db") -> None:
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
                CREATE TABLE IF NOT EXISTS turns (
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
            # Additive column added after the table's initial shape.
            # CREATE TABLE IF NOT EXISTS silently no-ops on a database that
            # already has this table without the new column — caught in
            # dev when an existing local data/analytics.db from before this
            # change crashed every gap-listing call with "no such column:
            # resolved". Guarded ALTER TABLE self-heals any existing
            # database instead of requiring a manual reset.
            try:
                conn.execute("ALTER TABLE turns ADD COLUMN resolved INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already exists
            try:
                conn.execute("ALTER TABLE turns ADD COLUMN shows_dissatisfaction INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already exists
            try:
                conn.execute("ALTER TABLE turns ADD COLUMN channel TEXT NOT NULL DEFAULT 'web'")
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_tenant ON turns(tenant_id)")
            conn.commit()

    def log_turn(
        self,
        *,
        tenant_id: str,
        session_id: str,
        query: str,
        answer_status: str,
        shows_buying_intent: bool = False,
        suggested_handoff: bool = False,
        shows_dissatisfaction: bool = False,
        channel: str = "web",
    ) -> ConversationTurn:
        turn = ConversationTurn(
            turn_id=f"turn_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            session_id=session_id,
            query=query,
            answer_status=answer_status,
            shows_buying_intent=shows_buying_intent,
            suggested_handoff=suggested_handoff,
            shows_dissatisfaction=shows_dissatisfaction,
            channel=channel,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO turns (turn_id, tenant_id, session_id, query, answer_status,
                    shows_buying_intent, suggested_handoff, shows_dissatisfaction, channel, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.turn_id, turn.tenant_id, turn.session_id, turn.query, turn.answer_status,
                    int(turn.shows_buying_intent), int(turn.suggested_handoff), int(turn.shows_dissatisfaction),
                    turn.channel, turn.created_at,
                ),
            )
            conn.commit()
        return turn

    def summary_for_tenant(self, tenant_id: str, *, gap_limit: int = 10, since_iso: str | None = None) -> AnalyticsSummary:
        # created_at is "%Y-%m-%dT%H:%M:%SZ" — lexicographically sortable,
        # so a plain string comparison is a correct time-window filter.
        clause = "tenant_id = ?" + (" AND created_at >= ?" if since_iso else "")
        params: tuple = (tenant_id, since_iso) if since_iso else (tenant_id,)

        with self._lock, self._db() as conn:
            total = conn.execute(f"SELECT COUNT(*) c FROM turns WHERE {clause}", params).fetchone()["c"]
            answered = conn.execute(
                f"SELECT COUNT(*) c FROM turns WHERE {clause} AND answer_status = 'answered'", params
            ).fetchone()["c"]
            abstained = conn.execute(
                f"SELECT COUNT(*) c FROM turns WHERE {clause} AND answer_status = 'insufficient_evidence'", params
            ).fetchone()["c"]
            buying_intent = conn.execute(
                f"SELECT COUNT(*) c FROM turns WHERE {clause} AND shows_buying_intent = 1", params
            ).fetchone()["c"]
            handoff = conn.execute(
                f"SELECT COUNT(*) c FROM turns WHERE {clause} AND suggested_handoff = 1", params
            ).fetchone()["c"]
            dissatisfaction = conn.execute(
                f"SELECT COUNT(*) c FROM turns WHERE {clause} AND shows_dissatisfaction = 1", params
            ).fetchone()["c"]
            gap_rows = conn.execute(
                f"SELECT query FROM turns WHERE {clause} AND answer_status = 'insufficient_evidence' AND resolved = 0 "
                "ORDER BY created_at DESC LIMIT ?",
                params + (gap_limit,),
            ).fetchall()

        return AnalyticsSummary(
            total_questions=total,
            answered_count=answered,
            abstention_count=abstained,
            buying_intent_count=buying_intent,
            handoff_suggested_count=handoff,
            dissatisfaction_count=dissatisfaction,
            recent_knowledge_gaps=[r["query"] for r in gap_rows],
        )

    def session_ids_with_buying_intent(self, tenant_id: str, *, since_iso: str | None = None) -> set[str]:
        """Distinct sessions where at least one turn showed buying intent
        — the join key app.py uses against LeadStore (Lead.session_id is
        the same session_id logged here) to find leads who showed real
        purchase interest but never got booked, i.e. a missed opportunity
        rather than a generic re-engagement candidate."""
        query = "SELECT DISTINCT session_id FROM turns WHERE tenant_id = ? AND shows_buying_intent = 1"
        params: list[str] = [tenant_id]
        if since_iso:
            query += " AND created_at >= ?"
            params.append(since_iso)
        with self._lock, self._db() as conn:
            rows = conn.execute(query, params).fetchall()
            return {r["session_id"] for r in rows}

    def list_open_gaps(self, tenant_id: str, *, limit: int = 20) -> list[KnowledgeGap]:
        """Unresolved knowledge gaps (most recent occurrence per question),
        for the dashboard's gap-closer UI — richer than the plain string
        list in AnalyticsSummary because publishing an answer needs a
        turn_id to mark resolved."""
        with self._lock, self._db() as conn:
            rows = conn.execute(
                """
                SELECT turn_id, tenant_id, query, MAX(created_at) as created_at
                FROM turns
                WHERE tenant_id = ? AND answer_status = 'insufficient_evidence' AND resolved = 0
                GROUP BY query
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (tenant_id, limit),
            ).fetchall()
            return [KnowledgeGap(**dict(r)) for r in rows]

    def get_gap(self, tenant_id: str, turn_id: str) -> KnowledgeGap | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT turn_id, tenant_id, query, created_at FROM turns WHERE tenant_id = ? AND turn_id = ?",
                (tenant_id, turn_id),
            ).fetchone()
            return KnowledgeGap(**dict(row)) if row else None

    def mark_gap_resolved(self, tenant_id: str, *, query: str) -> None:
        """Marks every occurrence of this exact question (for this tenant)
        as resolved, so it stops reappearing after the owner publishes an
        answer for it — the gap-closer UI groups by question text, not a
        single turn_id, since the same question may have been asked (and
        logged as a separate turn) more than once before it was answered."""
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE turns SET resolved = 1 WHERE tenant_id = ? AND query = ? AND answer_status = 'insufficient_evidence'",
                (tenant_id, query),
            )
            conn.commit()
