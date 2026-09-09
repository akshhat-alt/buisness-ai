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
    created_at: str


class AnalyticsSummary(BaseModel):
    total_questions: int
    answered_count: int
    abstention_count: int
    buying_intent_count: int
    handoff_suggested_count: int
    recent_knowledge_gaps: list[str]  # recent questions the assistant couldn't answer


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
    ) -> ConversationTurn:
        turn = ConversationTurn(
            turn_id=f"turn_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            session_id=session_id,
            query=query,
            answer_status=answer_status,
            shows_buying_intent=shows_buying_intent,
            suggested_handoff=suggested_handoff,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO turns (turn_id, tenant_id, session_id, query, answer_status,
                    shows_buying_intent, suggested_handoff, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.turn_id, turn.tenant_id, turn.session_id, turn.query, turn.answer_status,
                    int(turn.shows_buying_intent), int(turn.suggested_handoff), turn.created_at,
                ),
            )
            conn.commit()
        return turn

    def summary_for_tenant(self, tenant_id: str, *, gap_limit: int = 10) -> AnalyticsSummary:
        with self._lock, self._db() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM turns WHERE tenant_id = ?", (tenant_id,)).fetchone()["c"]
            answered = conn.execute(
                "SELECT COUNT(*) c FROM turns WHERE tenant_id = ? AND answer_status = 'answered'", (tenant_id,)
            ).fetchone()["c"]
            abstained = conn.execute(
                "SELECT COUNT(*) c FROM turns WHERE tenant_id = ? AND answer_status = 'insufficient_evidence'", (tenant_id,)
            ).fetchone()["c"]
            buying_intent = conn.execute(
                "SELECT COUNT(*) c FROM turns WHERE tenant_id = ? AND shows_buying_intent = 1", (tenant_id,)
            ).fetchone()["c"]
            handoff = conn.execute(
                "SELECT COUNT(*) c FROM turns WHERE tenant_id = ? AND suggested_handoff = 1", (tenant_id,)
            ).fetchone()["c"]
            gap_rows = conn.execute(
                "SELECT query FROM turns WHERE tenant_id = ? AND answer_status = 'insufficient_evidence' "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant_id, gap_limit),
            ).fetchall()

        return AnalyticsSummary(
            total_questions=total,
            answered_count=answered,
            abstention_count=abstained,
            buying_intent_count=buying_intent,
            handoff_suggested_count=handoff,
            recent_knowledge_gaps=[r["query"] for r in gap_rows],
        )
