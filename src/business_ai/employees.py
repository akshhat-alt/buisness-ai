"""Employee roster: tenant-scoped WhatsApp identity for the admin bot.

This is what turns the single scalar `TenantConfig.owner_whatsapp_number`
into a real roster — every business owner, manager, and staff member who
should be recognized as "internal" (routed to the admin workflow, never
the customer-facing assistant) on the tenant's WhatsApp line gets one row
here. No password, no dashboard login required: an employee's identity on
the admin bot is their WhatsApp number, which is exactly what "simple
enough for any employee" requires.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from pydantic import BaseModel

from business_ai.auth import ROLES


def normalize_whatsapp_number(raw: str | None) -> str:
    """Strips everything but digits, so "+91 98765 43210" and
    "919876543210" (Meta's own wa_id format) compare equal. The single
    canonical normalizer for every WhatsApp number comparison in the
    admin-bot roster — callers must normalize both the stored value and
    the inbound value with this same function."""
    return re.sub(r"\D", "", raw or "")


class Employee(BaseModel):
    employee_id: str
    tenant_id: str
    whatsapp_number: str  # always normalize_whatsapp_number()'d before storage
    name: str
    role: str  # "owner" | "manager" | "staff" — see auth.ROLES
    active: bool = True
    created_at: str


from business_ai.storage import SqliteStore


class EmployeeStore(SqliteStore):
    """Thread-safe SQLite store for a tenant's employee roster."""

    def __init__(self, db_path: Path | str = "data/employees.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS employees (
                    employee_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    whatsapp_number TEXT NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_employees_tenant ON employees(tenant_id)")
            # One roster entry per (tenant, number) — re-adding the same
            # number updates the existing row (see add()) rather than
            # creating a confusing duplicate identity.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_tenant_number "
                "ON employees(tenant_id, whatsapp_number)"
            )
            conn.commit()

    def _row_to_employee(self, row: sqlite3.Row) -> Employee:
        data = dict(row)
        data["active"] = bool(data["active"])
        return Employee(**data)

    def add(self, *, tenant_id: str, whatsapp_number: str, name: str, role: str = "staff") -> Employee:
        if not tenant_id:
            raise ValueError("tenant_id is required.")
        if role not in ROLES or role == "platform_admin":
            raise ValueError(f"Invalid employee role '{role}'.")
        number = normalize_whatsapp_number(whatsapp_number)
        if not number:
            raise ValueError("A valid WhatsApp number is required.")
        if not name or not name.strip():
            raise ValueError("A name is required.")

        with self._lock, self._db() as conn:
            existing = conn.execute(
                "SELECT employee_id FROM employees WHERE tenant_id = ? AND whatsapp_number = ?",
                (tenant_id, number),
            ).fetchone()
            if existing:
                # Re-registering the same number updates the roster entry
                # in place (e.g. a role change, a name correction) rather
                # than erroring — the number is the identity, not the row.
                conn.execute(
                    "UPDATE employees SET name = ?, role = ?, active = 1 WHERE employee_id = ?",
                    (name.strip(), role, existing["employee_id"]),
                )
                conn.commit()
                return self.get(tenant_id, existing["employee_id"])  # type: ignore[return-value]

            employee = Employee(
                employee_id=f"emp_{secrets.token_hex(8)}",
                tenant_id=tenant_id,
                whatsapp_number=number,
                name=name.strip(),
                role=role,
                active=True,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            conn.execute(
                """
                INSERT INTO employees (employee_id, tenant_id, whatsapp_number, name, role, active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    employee.employee_id, employee.tenant_id, employee.whatsapp_number,
                    employee.name, employee.role, int(employee.active), employee.created_at,
                ),
            )
            conn.commit()
        return employee

    def get(self, tenant_id: str, employee_id: str) -> Employee | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM employees WHERE tenant_id = ? AND employee_id = ?", (tenant_id, employee_id)
            ).fetchone()
            return self._row_to_employee(row) if row else None

    def find_by_whatsapp(self, tenant_id: str, whatsapp_number: str) -> Employee | None:
        """Always tenant-scoped: the tenant is already resolved via the
        BUSINESS's own phone_number_id before this is ever called (see
        app.py's webhook handler), so a number is only meaningful within
        that resolved tenant's own roster. A bare global lookup here
        would let a number registered under one tenant match another
        tenant's inbound traffic — a real cross-tenant identity leak."""
        number = normalize_whatsapp_number(whatsapp_number)
        if not number:
            return None
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM employees WHERE tenant_id = ? AND whatsapp_number = ? AND active = 1",
                (tenant_id, number),
            ).fetchone()
            return self._row_to_employee(row) if row else None

    def list_for_tenant(self, tenant_id: str, *, active_only: bool = True) -> list[Employee]:
        with self._lock, self._db() as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT * FROM employees WHERE tenant_id = ? AND active = 1 ORDER BY created_at ASC",
                    (tenant_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM employees WHERE tenant_id = ? ORDER BY created_at ASC", (tenant_id,)
                ).fetchall()
            return [self._row_to_employee(r) for r in rows]

    def set_role(self, tenant_id: str, employee_id: str, role: str) -> Employee | None:
        if role not in ROLES or role == "platform_admin":
            raise ValueError(f"Invalid employee role '{role}'.")
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "UPDATE employees SET role = ? WHERE tenant_id = ? AND employee_id = ?",
                (role, tenant_id, employee_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(tenant_id, employee_id)

    def deactivate(self, tenant_id: str, employee_id: str) -> Employee | None:
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "UPDATE employees SET active = 0 WHERE tenant_id = ? AND employee_id = ?",
                (tenant_id, employee_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return self.get(tenant_id, employee_id)

    def ensure_owner_bootstrap(self, tenant_id: str, whatsapp_number: str | None, name: str = "Owner") -> None:
        """Auto-upserts the tenant's `owner_whatsapp_number` into the
        roster as an owner-role row, so a tenant configured before the
        roster existed keeps working with zero action on their part —
        see app.py's webhook handler, which calls this on every inbound
        message before checking the roster."""
        if not whatsapp_number:
            return
        number = normalize_whatsapp_number(whatsapp_number)
        if not number:
            return
        with self._lock, self._db() as conn:
            existing = conn.execute(
                "SELECT employee_id FROM employees WHERE tenant_id = ? AND whatsapp_number = ?",
                (tenant_id, number),
            ).fetchone()
            if existing:
                return  # already present (possibly re-registered with a different role since) — don't overwrite
        self.add(tenant_id=tenant_id, whatsapp_number=number, name=name, role="owner")

    def delete_for_tenant(self, tenant_id: str) -> int:
        """Phase 9 tenant data deletion: removes every row for this
        tenant. Returns the number of rows deleted, for the export/
        deletion endpoint's summary report."""
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM employees WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount
