"""Lead capture: a business's customers leaving contact info, tenant-scoped.

New in Business AI — Shri AI has no equivalent. Deliberately simple: one
table, no pipeline stages, no CRM fields. A local business's first need is
"tell me who wants to talk to me," not a sales pipeline.
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


class Lead(BaseModel):
    lead_id: str
    tenant_id: str
    session_id: str
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    message: str | None = None
    source: str = "chat"  # "chat" for now; room for "manual", "form", etc. later
    created_at: str


class LeadStore:
    """Thread-safe SQLite store for captured leads."""

    def __init__(self, db_path: Path | str = "data/leads.db") -> None:
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
                CREATE TABLE IF NOT EXISTS leads (
                    lead_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    name TEXT,
                    phone TEXT,
                    email TEXT,
                    message TEXT,
                    source TEXT NOT NULL DEFAULT 'chat',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_tenant ON leads(tenant_id)")
            conn.commit()

    def create(
        self,
        *,
        tenant_id: str,
        session_id: str,
        name: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        message: str | None = None,
        source: str = "chat",
    ) -> Lead:
        if not tenant_id:
            raise ValueError("tenant_id is required.")
        if not (phone or email):
            raise ValueError("A lead needs at least a phone number or an email address.")

        lead = Lead(
            lead_id=f"lead_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            session_id=session_id,
            name=name,
            phone=phone,
            email=email,
            message=message,
            source=source,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO leads (lead_id, tenant_id, session_id, name, phone, email, message, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (lead.lead_id, lead.tenant_id, lead.session_id, lead.name, lead.phone, lead.email, lead.message, lead.source, lead.created_at),
            )
            conn.commit()
        return lead

    def get(self, tenant_id: str, lead_id: str) -> Lead | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM leads WHERE tenant_id = ? AND lead_id = ?", (tenant_id, lead_id)
            ).fetchone()
            return Lead(**dict(row)) if row else None

    def list_for_tenant(self, tenant_id: str, *, limit: int = 200, since_iso: str | None = None) -> list[Lead]:
        # created_at is "%Y-%m-%dT%H:%M:%SZ" — lexicographically sortable,
        # so a plain string comparison is a correct time-window filter
        # without needing a separate epoch column.
        with self._lock, self._db() as conn:
            if since_iso:
                rows = conn.execute(
                    "SELECT * FROM leads WHERE tenant_id = ? AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
                    (tenant_id, since_iso, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM leads WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
                    (tenant_id, limit),
                ).fetchall()
            return [Lead(**dict(r)) for r in rows]

    def count_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM leads WHERE tenant_id = ?", (tenant_id,)).fetchone()
            return row["c"] if row else 0
