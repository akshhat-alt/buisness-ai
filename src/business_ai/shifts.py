"""Staff shift scheduling (Phase 23 — Restaurant Operations Intelligence).

New in Business AI — nothing in this codebase tracked WORKING HOURS
before this; EmployeeStore's "roster" is only the list of who works
here, TaskStore is what they should do, neither says when they're on
the clock. One flat table, tenant-scoped exactly like tasks.py, same
SqliteStore pattern every other store in this codebase already uses.
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

from business_ai.storage import SqliteStore


class Shift(BaseModel):
    shift_id: str
    tenant_id: str
    employee_id: str
    shift_date: str  # "YYYY-MM-DD", the business's own local (IST) calendar date
    start_time: str  # "HH:MM", 24-hour, IST wall-clock — same single-timezone V1 scope as appointments
    end_time: str  # "HH:MM"
    role_label: str = ""  # optional free-text station/role, e.g. "kitchen", "front of house"
    created_by_employee_id: str | None = None
    created_at: str


class ShiftStore(SqliteStore):
    """Thread-safe SQLite store for staff shifts."""

    def __init__(self, db_path: Path | str = "data/shifts.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shifts (
                    shift_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    employee_id TEXT NOT NULL,
                    shift_date TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    role_label TEXT NOT NULL DEFAULT '',
                    created_by_employee_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shifts_tenant ON shifts(tenant_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shifts_tenant_date ON shifts(tenant_id, shift_date)")
            conn.commit()

    def _row_to_shift(self, row: sqlite3.Row) -> Shift:
        return Shift(**dict(row))

    def create(
        self, *, tenant_id: str, employee_id: str, shift_date: str, start_time: str, end_time: str,
        role_label: str = "", created_by_employee_id: str | None = None,
    ) -> Shift:
        if not (tenant_id and employee_id and shift_date and start_time and end_time):
            raise ValueError("tenant_id, employee_id, shift_date, start_time, and end_time are all required.")
        shift = Shift(
            shift_id=f"shift_{secrets.token_hex(8)}", tenant_id=tenant_id, employee_id=employee_id,
            shift_date=shift_date, start_time=start_time, end_time=end_time, role_label=role_label,
            created_by_employee_id=created_by_employee_id,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO shifts (shift_id, tenant_id, employee_id, shift_date, start_time, end_time,
                    role_label, created_by_employee_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shift.shift_id, shift.tenant_id, shift.employee_id, shift.shift_date, shift.start_time,
                    shift.end_time, shift.role_label, shift.created_by_employee_id, shift.created_at,
                ),
            )
            conn.commit()
        return shift

    def get(self, tenant_id: str, shift_id: str) -> Shift | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM shifts WHERE tenant_id = ? AND shift_id = ?", (tenant_id, shift_id)
            ).fetchone()
            return self._row_to_shift(row) if row else None

    def list_for_tenant(
        self, tenant_id: str, *, employee_id: str | None = None, shift_date: str | None = None,
    ) -> list[Shift]:
        clause = "tenant_id = ?"
        params: list = [tenant_id]
        if employee_id:
            clause += " AND employee_id = ?"
            params.append(employee_id)
        if shift_date:
            clause += " AND shift_date = ?"
            params.append(shift_date)
        with self._lock, self._db() as conn:
            rows = conn.execute(
                f"SELECT * FROM shifts WHERE {clause} ORDER BY shift_date ASC, start_time ASC", params
            ).fetchall()
            return [self._row_to_shift(r) for r in rows]

    def delete(self, tenant_id: str, shift_id: str) -> bool:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM shifts WHERE tenant_id = ? AND shift_id = ?", (tenant_id, shift_id))
            conn.commit()
            return cur.rowcount > 0

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM shifts WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount
