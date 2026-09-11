"""Employee task coordination: assignment, status, deadlines, approvals.

New in the admin WhatsApp bot — Business AI's customer-facing product has
no equivalent. One flat table, tenant-scoped exactly like leads.py, with
enough state (status, due date, approval) to answer the questions an
owner actually asks on WhatsApp: what's overdue, who owns it, is it done.
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

OPEN_STATUSES = ("open", "in_progress", "blocked", "awaiting_approval")
TERMINAL_STATUSES = ("done", "cancelled")
ALL_STATUSES = OPEN_STATUSES + TERMINAL_STATUSES


class Task(BaseModel):
    task_id: str
    tenant_id: str
    title: str
    description: str | None = None
    assigned_to_employee_id: str
    assigned_by_employee_id: str
    status: str = "open"  # open | in_progress | awaiting_approval | blocked | done | cancelled
    due_at: str | None = None  # ISO datetime, UTC
    created_at: str
    updated_at: str
    reminder_sent_at: str | None = None
    approval_required: bool = False
    approved_by_employee_id: str | None = None
    approved_at: str | None = None
    block_reason: str | None = None
    # Links this task back to the lead/customer conversation it originated
    # from, if any — enables a future "verify with the customer" step once
    # a customer-facing task is marked done, without needing a second field.
    customer_facing_lead_id: str | None = None


class TaskStore:
    """Thread-safe SQLite store for employee tasks."""

    def __init__(self, db_path: Path | str = "data/tasks.db") -> None:
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
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    assigned_to_employee_id TEXT NOT NULL,
                    assigned_by_employee_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    due_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reminder_sent_at TEXT,
                    approval_required INTEGER NOT NULL DEFAULT 0,
                    approved_by_employee_id TEXT,
                    approved_at TEXT,
                    block_reason TEXT,
                    customer_facing_lead_id TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks(tenant_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(tenant_id, assigned_to_employee_id)")
            conn.commit()

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        data = dict(row)
        data["approval_required"] = bool(data["approval_required"])
        return Task(**data)

    def create(
        self,
        *,
        tenant_id: str,
        title: str,
        assigned_to_employee_id: str,
        assigned_by_employee_id: str,
        description: str | None = None,
        due_at: str | None = None,
        approval_required: bool = False,
        customer_facing_lead_id: str | None = None,
    ) -> Task:
        if not tenant_id:
            raise ValueError("tenant_id is required.")
        if not title or not title.strip():
            raise ValueError("A task title is required.")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        task = Task(
            task_id=f"task_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            title=title.strip(),
            description=description,
            assigned_to_employee_id=assigned_to_employee_id,
            assigned_by_employee_id=assigned_by_employee_id,
            status="open",
            due_at=due_at,
            created_at=now_iso,
            updated_at=now_iso,
            approval_required=approval_required,
            customer_facing_lead_id=customer_facing_lead_id,
        )
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, tenant_id, title, description, assigned_to_employee_id, assigned_by_employee_id,
                    status, due_at, created_at, updated_at, approval_required, customer_facing_lead_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id, task.tenant_id, task.title, task.description,
                    task.assigned_to_employee_id, task.assigned_by_employee_id,
                    task.status, task.due_at, task.created_at, task.updated_at,
                    int(task.approval_required), task.customer_facing_lead_id,
                ),
            )
            conn.commit()
        return task

    def get(self, tenant_id: str, task_id: str) -> Task | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE tenant_id = ? AND task_id = ?", (tenant_id, task_id)
            ).fetchone()
            return self._row_to_task(row) if row else None

    def list_for_tenant(
        self, tenant_id: str, *, status: str | None = None, assigned_to_employee_id: str | None = None
    ) -> list[Task]:
        query = "SELECT * FROM tasks WHERE tenant_id = ?"
        params: list[str] = [tenant_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        if assigned_to_employee_id:
            query += " AND assigned_to_employee_id = ?"
            params.append(assigned_to_employee_id)
        query += " ORDER BY (due_at IS NULL), due_at ASC, created_at ASC"
        with self._lock, self._db() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_task(r) for r in rows]

    def list_open_for_tenant(self, tenant_id: str, *, assigned_to_employee_id: str | None = None) -> list[Task]:
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        query = f"SELECT * FROM tasks WHERE tenant_id = ? AND status IN ({placeholders})"
        params: list[str] = [tenant_id, *OPEN_STATUSES]
        if assigned_to_employee_id:
            query += " AND assigned_to_employee_id = ?"
            params.append(assigned_to_employee_id)
        query += " ORDER BY (due_at IS NULL), due_at ASC, created_at ASC"
        with self._lock, self._db() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_task(r) for r in rows]

    def list_overdue(self, tenant_id: str, *, now_iso: str | None = None) -> list[Task]:
        """Overdue = has a due date in the past and is still in an open
        status. Returns full rows (not a count) — the daily pulse and
        `overdue` command need to name who owns what, not just a number."""
        cutoff = now_iso or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        with self._lock, self._db() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM tasks
                WHERE tenant_id = ? AND status IN ({placeholders}) AND due_at IS NOT NULL AND due_at < ?
                ORDER BY due_at ASC
                """,
                (tenant_id, *OPEN_STATUSES, cutoff),
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    def count_open_for_tenant(self, tenant_id: str) -> int:
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        with self._lock, self._db() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) as c FROM tasks WHERE tenant_id = ? AND status IN ({placeholders})",
                (tenant_id, *OPEN_STATUSES),
            ).fetchone()
            return row["c"] if row else 0

    def update_status(self, tenant_id: str, task_id: str, status: str, *, block_reason: str | None = None) -> Task | None:
        if status not in ALL_STATUSES:
            raise ValueError(f"Invalid task status '{status}'.")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = ?, block_reason = ?, updated_at = ? WHERE tenant_id = ? AND task_id = ?",
                (status, block_reason, now_iso, tenant_id, task_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(tenant_id, task_id)

    def reassign(self, tenant_id: str, task_id: str, new_assignee_employee_id: str) -> Task | None:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "UPDATE tasks SET assigned_to_employee_id = ?, updated_at = ? WHERE tenant_id = ? AND task_id = ?",
                (new_assignee_employee_id, now_iso, tenant_id, task_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(tenant_id, task_id)

    def approve(self, tenant_id: str, task_id: str, approved_by_employee_id: str) -> Task | None:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._db() as conn:
            cur = conn.execute(
                """
                UPDATE tasks SET approved_by_employee_id = ?, approved_at = ?, status = 'done', updated_at = ?
                WHERE tenant_id = ? AND task_id = ?
                """,
                (approved_by_employee_id, now_iso, now_iso, tenant_id, task_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(tenant_id, task_id)

    def reject(self, tenant_id: str, task_id: str, *, reason: str | None = None) -> Task | None:
        """A rejected approval sends the task back to in_progress (not
        cancelled) — the work isn't abandoned, it needs another pass."""
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = 'in_progress', block_reason = ?, updated_at = ? WHERE tenant_id = ? AND task_id = ?",
                (reason, now_iso, tenant_id, task_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(tenant_id, task_id)

    def mark_reminder_sent(self, tenant_id: str, task_id: str) -> None:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE tasks SET reminder_sent_at = ? WHERE tenant_id = ? AND task_id = ?",
                (now_iso, tenant_id, task_id),
            )
            conn.commit()
