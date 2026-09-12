"""Append-only audit log for the admin WhatsApp bot's state-changing
actions (role changes, task approvals, employee roster edits, and so on).

Per-row timestamps on individual tables answer "when did this change" but
not "who did it, in what order, across the whole tenant" — the latter is
what actually gets checked when there's a dispute about who approved an
expense or who marked a complaint resolved. One small, generic store
rather than bolting an actor/timestamp pair onto every table separately.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from pydantic import BaseModel


class AuditLogEntry(BaseModel):
    log_id: str
    tenant_id: str
    actor_employee_id: str | None  # None for a platform_admin/system-triggered action
    action: str  # e.g. "task_approved", "role_changed", "employee_added"
    target_type: str  # e.g. "task", "employee"
    target_id: str
    metadata: dict[str, Any] = {}
    created_at: str


from business_ai.storage import SqliteStore


class AuditLogStore(SqliteStore):
    """Thread-safe, append-only SQLite log."""

    def __init__(self, db_path: Path | str = "data/audit.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    log_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    actor_employee_id TEXT,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id)")
            conn.commit()

    def record(
        self,
        *,
        tenant_id: str,
        action: str,
        target_type: str,
        target_id: str,
        actor_employee_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditLogEntry:
        entry = AuditLogEntry(
            log_id=f"audit_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            actor_employee_id=actor_employee_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata or {},
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO audit_log
                    (log_id, tenant_id, actor_employee_id, action, target_type, target_id, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.log_id, entry.tenant_id, entry.actor_employee_id, entry.action,
                    entry.target_type, entry.target_id, json.dumps(entry.metadata), entry.created_at,
                ),
            )
            conn.commit()
        return entry

    def list_for_tenant(self, tenant_id: str, *, action: str | None = None, limit: int = 200) -> list[AuditLogEntry]:
        with self._lock, self._db() as conn:
            if action:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE tenant_id = ? AND action = ? ORDER BY created_at DESC LIMIT ?",
                    (tenant_id, action, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
                    (tenant_id, limit),
                ).fetchall()
            entries = []
            for row in rows:
                data = dict(row)
                data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
                entries.append(AuditLogEntry(**data))
            return entries

    def delete_for_tenant(self, tenant_id: str) -> int:
        """Phase 9 tenant data deletion: removes every row for this
        tenant. Returns the number of rows deleted, for the export/
        deletion endpoint's summary report."""
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM audit_log WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount
