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
    # Owner-confirmed outcomes — same "request-and-confirm, no webhook"
    # honesty as platform billing's mark-paid: a link/reminder being SENT
    # is never treated as money received or a customer served. These are
    # the only fields anything downstream (revenue totals, conversion
    # rate, lead stage) may treat as "this actually happened."
    deposit_paid_at: str | None = None
    deposit_paid_amount_inr: int | None = None
    appointment_outcome: str | None = None  # "completed" | "no_show" | "cancelled"
    appointment_outcome_at: str | None = None
    # Phase 23 (Restaurant Operations Intelligence) — a restaurant
    # reservation IS a Lead with an appointment; the one thing a salon/
    # coaching appointment never needed and a table booking always does
    # is how many guests. Optional and generically named (not
    # restaurant-specific) since any business could plausibly use it.
    party_size: int | None = None


def lead_stage(lead: Lead) -> str:
    """Deterministic funnel stage derived ONLY from fields already on the
    row — no LLM, no inference, no invented status. Order matters: later
    checks win, since a lead can pass through several of these over time
    (e.g. appointment set, then completed) and the row keeps every marker
    rather than overwriting history.

    new              -> just captured, nothing else has happened yet
    engaged          -> an appointment has been set
    awaiting_payment -> a deposit link was sent, not yet confirmed paid
    converted        -> a deposit was confirmed paid OR the appointment
                        was confirmed completed (the only two states
                        anything downstream may call "revenue"/"won")
    lost             -> the appointment was confirmed cancelled/no-show
    reengaged        -> a missed-lead follow-up was sent, no booking yet
    """
    if lead.deposit_paid_at or lead.appointment_outcome == "completed":
        return "converted"
    if lead.appointment_outcome in ("no_show", "cancelled"):
        return "lost"
    if lead.deposit_link_sent_at:
        return "awaiting_payment"
    if lead.appointment_at:
        return "engaged"
    if lead.reengaged_at or lead.winback_sent_at:
        return "reengaged"
    return "new"


from business_ai.storage import SqliteStore


class LeadStore(SqliteStore):
    """Thread-safe SQLite store for captured leads."""

    def __init__(self, db_path: Path | str = "data/leads.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
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
                "deposit_paid_at", "appointment_outcome", "appointment_outcome_at",
            ):
                try:
                    conn.execute(f"ALTER TABLE leads ADD COLUMN {column} TEXT")
                except sqlite3.OperationalError:
                    pass  # column already exists
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN deposit_paid_amount_inr INTEGER")
            except sqlite3.OperationalError:
                pass  # column already exists
            try:
                conn.execute("ALTER TABLE leads ADD COLUMN party_size INTEGER")
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

    def set_appointment(
        self, tenant_id: str, lead_id: str, appointment_at: str, *, party_size: int | None = None,
    ) -> Lead | None:
        """A new appointment date invalidates any reminder/win-back marker
        tied to the previous one — otherwise a rescheduled customer could
        silently never get a reminder for their new slot, or get a
        win-back nudge despite having just rebooked. party_size is
        optional and only ever overwritten when explicitly given —
        omitting it on a reschedule keeps whatever was already recorded
        rather than blanking out a real reservation's guest count."""
        with self._lock, self._db() as conn:
            if party_size is not None:
                cur = conn.execute(
                    "UPDATE leads SET appointment_at = ?, party_size = ?, reminder_sent_at = NULL, winback_sent_at = NULL "
                    "WHERE tenant_id = ? AND lead_id = ?",
                    (appointment_at, party_size, tenant_id, lead_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE leads SET appointment_at = ?, reminder_sent_at = NULL, winback_sent_at = NULL "
                    "WHERE tenant_id = ? AND lead_id = ?",
                    (appointment_at, tenant_id, lead_id),
                )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(tenant_id, lead_id)

    def list_upcoming_appointments(self, tenant_id: str, *, within_hours: float = 24 * 7) -> list[Lead]:
        """Every lead with a future appointment inside the window, not yet
        confirmed as any outcome — the reservations-book view for
        restaurant staff (Phase 23), same window-query shape as
        list_for_reminders/list_for_winback below."""
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        until_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + within_hours * 3600))
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM leads WHERE tenant_id = ? AND appointment_at IS NOT NULL "
                "AND appointment_at >= ? AND appointment_at <= ? AND appointment_outcome IS NULL "
                "ORDER BY appointment_at ASC",
                (tenant_id, now_iso, until_iso),
            ).fetchall()
            return [Lead(**dict(r)) for r in rows]

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

    def mark_deposit_paid(self, tenant_id: str, lead_id: str, amount_inr: int) -> Lead | None:
        """Owner-confirmed, same "request-and-confirm, no webhook" shape
        as platform billing's mark-paid — a deposit LINK being sent is
        never treated as money received; this is the one action that is.
        The amount is recorded as stated by the owner at confirmation
        time (not assumed from the tenant's configured deposit_amount_inr,
        which may have changed since the link was sent, or the owner may
        be recording a different actual amount received)."""
        if amount_inr <= 0:
            raise ValueError("amount_inr must be positive.")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "UPDATE leads SET deposit_paid_at = ?, deposit_paid_amount_inr = ? WHERE tenant_id = ? AND lead_id = ?",
                (now_iso, amount_inr, tenant_id, lead_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(tenant_id, lead_id)

    def record_appointment_outcome(self, tenant_id: str, lead_id: str, outcome: str) -> Lead | None:
        if outcome not in ("completed", "no_show", "cancelled"):
            raise ValueError(f"Invalid appointment outcome '{outcome}'.")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "UPDATE leads SET appointment_outcome = ?, appointment_outcome_at = ? WHERE tenant_id = ? AND lead_id = ?",
                (outcome, now_iso, tenant_id, lead_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(tenant_id, lead_id)

    def sum_confirmed_revenue(self, tenant_id: str, *, since_iso: str | None = None) -> int:
        """Sum of OWNER-CONFIRMED deposit payments only — never an
        estimate, never a synced/live figure. Windowed by when the
        payment was confirmed (deposit_paid_at), not when the lead was
        created, so a window correctly reflects money confirmed in it."""
        query = "SELECT COALESCE(SUM(deposit_paid_amount_inr), 0) as total FROM leads WHERE tenant_id = ? AND deposit_paid_at IS NOT NULL"
        params: list[str] = [tenant_id]
        if since_iso:
            query += " AND deposit_paid_at >= ?"
            params.append(since_iso)
        with self._lock, self._db() as conn:
            row = conn.execute(query, params).fetchone()
            return int(row["total"]) if row else 0

    def count_converted(self, tenant_id: str, *, since_iso: str | None = None) -> int:
        """A lead counts as converted once EITHER a deposit was
        confirmed paid or an appointment was confirmed completed —
        deliberately OR, not AND, since not every business collects a
        deposit for every booking."""
        query = (
            "SELECT COUNT(*) as c FROM leads WHERE tenant_id = ? AND "
            "(deposit_paid_at IS NOT NULL OR appointment_outcome = 'completed')"
        )
        params: list[str] = [tenant_id]
        if since_iso:
            query += " AND (COALESCE(deposit_paid_at, '') >= ? OR COALESCE(appointment_outcome_at, '') >= ?)"
            params.extend([since_iso, since_iso])
        with self._lock, self._db() as conn:
            row = conn.execute(query, params).fetchone()
            return row["c"] if row else 0

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

    def delete_for_tenant(self, tenant_id: str) -> int:
        """Phase 9 tenant data deletion: removes every row for this
        tenant. Returns the number of rows deleted, for the export/
        deletion endpoint's summary report."""
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM leads WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount
