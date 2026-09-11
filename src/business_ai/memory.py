"""Business Memory: owner-approved workaround/SOP notes for recurring
employee feedback themes. Deliberately small: one note per theme per
tenant, written by an owner in response to a REAL pattern already seen
in FeedbackStore (see FeedbackStore.summarize_by_theme), never generated
speculatively. The next employee reporting that same theme gets the
existing guidance surfaced back to them — "we know about this" instead
of the same complaint landing unanswered every week.
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


class SopNote(BaseModel):
    note_id: str
    tenant_id: str
    theme: str  # one of generation.FEEDBACK_THEMES
    text: str
    approved_by_employee_id: str
    created_at: str
    updated_at: str


class SopStore:
    """Thread-safe SQLite store. One active note per (tenant, theme) —
    re-approving the same theme updates the existing note rather than
    accumulating a history nobody reads."""

    def __init__(self, db_path: Path | str = "data/sops.db") -> None:
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
                CREATE TABLE IF NOT EXISTS sop_notes (
                    note_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    theme TEXT NOT NULL,
                    text TEXT NOT NULL,
                    approved_by_employee_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sop_tenant_theme ON sop_notes(tenant_id, theme)"
            )
            conn.commit()

    def _row_to_note(self, row: sqlite3.Row) -> SopNote:
        return SopNote(**dict(row))

    def approve(self, *, tenant_id: str, theme: str, text: str, approved_by_employee_id: str) -> SopNote:
        if not text or not text.strip():
            raise ValueError("SOP note text is required.")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._db() as conn:
            existing = conn.execute(
                "SELECT note_id, created_at FROM sop_notes WHERE tenant_id = ? AND theme = ?", (tenant_id, theme)
            ).fetchone()
            if existing:
                note = SopNote(
                    note_id=existing["note_id"], tenant_id=tenant_id, theme=theme, text=text.strip(),
                    approved_by_employee_id=approved_by_employee_id, created_at=existing["created_at"], updated_at=now_iso,
                )
                conn.execute(
                    "UPDATE sop_notes SET text = ?, approved_by_employee_id = ?, updated_at = ? WHERE note_id = ?",
                    (note.text, note.approved_by_employee_id, note.updated_at, note.note_id),
                )
            else:
                note = SopNote(
                    note_id=f"sop_{secrets.token_hex(8)}", tenant_id=tenant_id, theme=theme, text=text.strip(),
                    approved_by_employee_id=approved_by_employee_id, created_at=now_iso, updated_at=now_iso,
                )
                conn.execute(
                    """
                    INSERT INTO sop_notes (note_id, tenant_id, theme, text, approved_by_employee_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (note.note_id, note.tenant_id, note.theme, note.text, note.approved_by_employee_id,
                     note.created_at, note.updated_at),
                )
            conn.commit()
        return note

    def get_for_theme(self, tenant_id: str, theme: str) -> SopNote | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM sop_notes WHERE tenant_id = ? AND theme = ?", (tenant_id, theme)
            ).fetchone()
            return self._row_to_note(row) if row else None

    def list_for_tenant(self, tenant_id: str) -> list[SopNote]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM sop_notes WHERE tenant_id = ? ORDER BY updated_at DESC", (tenant_id,)
            ).fetchall()
            return [self._row_to_note(r) for r in rows]
