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
    # Appointment reminders, deposit links, re-engagement, and win-back all
    # key off these four fields rather than a separate CRM/pipeline table —
    # this Lead row already *is* the durable customer record for a
    # WhatsApp contact (session_id is stable per phone number forever, see
    # whatsapp.py), so extending it directly is the smallest correct model.
    appointment_at: str | None = None  # ISO datetime of the next/last known appointment
    reminder_sent_at: str | None = None  # reminder sent for the CURRENT appointment_at
    reengaged_at: str | None = None  # missed-lead follow-up already sent
    winback_sent_at: str | None = None  # win-back nudge sent for the CURRENT appointment_at
    deposit_link_sent_at: str | None = None  # payment/deposit link already sent


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
            # Additive columns for the automation features (reminders,
            # deposits, re-engagement, win-back) — guarded ALTER TABLE
            # self-heals an existing database without a manual migration,
            # same pattern already used in analytics.py.
            for column in (
                "appointment_at", "reminder_sent_at", "reengaged_at", "winback_sent_at", "deposit_link_sent_at",
            ):
                try:
                    conn.execute(f"ALTER TABLE leads ADD COLUMN {column} TEXT")
                except sqlite3.OperationalError:
                    pass  # column already exists
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

    def exists_for_session(self, tenant_id: str, session_id: str) -> bool:
        """Used by the WhatsApp channel to capture a lead once per
        conversation (a real customer phone number, straight from Meta,
        on the customer's very first message) instead of inserting a new
        row on every single message in the thread."""
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT 1 FROM leads WHERE tenant_id = ? AND session_id = ? LIMIT 1", (tenant_id, session_id)
            ).fetchone()
            return row is not None

    def count_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            row = conn.execute("SELECT COUNT(*) as c FROM leads WHERE tenant_id = ?", (tenant_id,)).fetchone()
            return row["c"] if row else 0

    def set_appointment(self, tenant_id: str, lead_id: str, appointment_at: str) -> Lead | None:
        """A new appointment date invalidates any reminder/win-back marker
        tied to the previous one — otherwise a rescheduled customer could
        silently never get a reminder for their new slot, or get a
        win-back nudge despite having just rebooked."""
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "UPDATE leads SET appointment_at = ?, reminder_sent_at = NULL, winback_sent_at = NULL "
                "WHERE tenant_id = ? AND lead_id = ?",
                (appointment_at, tenant_id, lead_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(tenant_id, lead_id)

    def _mark(self, tenant_id: str, lead_id: str, column: str) -> None:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._db() as conn:
            conn.execute(
                f"UPDATE leads SET {column} = ? WHERE tenant_id = ? AND lead_id = ?", (now_iso, tenant_id, lead_id)
            )
            conn.commit()

    def mark_reminder_sent(self, tenant_id: str, lead_id: str) -> None:
        self._mark(tenant_id, lead_id, "reminder_sent_at")

    def mark_reengaged(self, tenant_id: str, lead_id: str) -> None:
        self._mark(tenant_id, lead_id, "reengaged_at")

    def mark_winback_sent(self, tenant_id: str, lead_id: str) -> None:
        self._mark(tenant_id, lead_id, "winback_sent_at")

    def mark_deposit_link_sent(self, tenant_id: str, lead_id: str) -> None:
        self._mark(tenant_id, lead_id, "deposit_link_sent_at")

    def list_for_reengagement(self, tenant_id: str, *, older_than_iso: str, newer_than_iso: str) -> list[Lead]:
        """Leads that already showed intent (they left contact info at
        all) but never booked — bounded to a bootstrap-safe window so
        turning this on for an established tenant doesn't blast every
        lead in their history at once."""
        with self._lock, self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM leads
                WHERE tenant_id = ? AND reengaged_at IS NULL AND appointment_at IS NULL
                  AND created_at <= ? AND created_at >= ?
                ORDER BY created_at ASC
                """,
                (tenant_id, older_than_iso, newer_than_iso),
            ).fetchall()
            return [Lead(**dict(r)) for r in rows]

    def list_for_reminders(self, tenant_id: str, *, window_start_iso: str, window_end_iso: str) -> list[Lead]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM leads
                WHERE tenant_id = ? AND reminder_sent_at IS NULL
                  AND appointment_at IS NOT NULL AND appointment_at >= ? AND appointment_at <= ?
                ORDER BY appointment_at ASC
                """,
                (tenant_id, window_start_iso, window_end_iso),
            ).fetchall()
            return [Lead(**dict(r)) for r in rows]

    def list_for_winback(self, tenant_id: str, *, source: str, cutoff_iso: str) -> list[Lead]:
        """A lapsed recurring customer: their last known appointment is
        older than the tenant's win-back threshold, and they haven't
        already been nudged for that same appointment. Scoped to `source`
        (WhatsApp in practice) — see winback.py's docstring for why."""
        with self._lock, self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM leads
                WHERE tenant_id = ? AND source = ? AND appointment_at IS NOT NULL AND appointment_at <= ?
                  AND (winback_sent_at IS NULL OR winback_sent_at < appointment_at)
                ORDER BY appointment_at ASC
                """,
                (tenant_id, source, cutoff_iso),
            ).fetchall()
            return [Lead(**dict(r)) for r in rows]
