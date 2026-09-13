"""Restaurant Foundation (Phase 17): the supplier directory.

Deliberately minimal — a name and a contact number, same shape as
`employees.py`'s roster before any of its later richness. Purchases
(`purchases.py`) reference a supplier by id; nothing here computes
supplier performance yet (price trend, on-time delivery) — that's
Phase 23's Restaurant Operations Intelligence, reading this same table.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from pydantic import BaseModel

from business_ai.storage import SqliteStore


class Supplier(BaseModel):
    supplier_id: str
    tenant_id: str
    name: str
    phone: str | None = None
    notes: str = ""
    active: bool = True
    created_at: str


class SupplierStore(SqliteStore):
    def __init__(self, db_path: Path | str = "data/suppliers.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS suppliers (
                    supplier_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    phone TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_suppliers_tenant ON suppliers(tenant_id)")
            conn.commit()

    def _row_to_supplier(self, row) -> Supplier:
        data = dict(row)
        data["active"] = bool(data["active"])
        return Supplier(**data)

    def create(self, *, tenant_id: str, name: str, phone: str | None = None, notes: str = "") -> Supplier:
        if not name.strip():
            raise ValueError("Supplier name is required.")
        supplier = Supplier(
            supplier_id=f"sup_{secrets.token_hex(8)}", tenant_id=tenant_id, name=name.strip(), phone=phone,
            notes=notes.strip(), created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO suppliers (supplier_id, tenant_id, name, phone, notes, active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (supplier.supplier_id, supplier.tenant_id, supplier.name, supplier.phone, supplier.notes, int(supplier.active), supplier.created_at),
            )
            conn.commit()
        return supplier

    def get(self, tenant_id: str, supplier_id: str) -> Supplier | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM suppliers WHERE tenant_id = ? AND supplier_id = ?", (tenant_id, supplier_id),
            ).fetchone()
            return self._row_to_supplier(row) if row else None

    def find_by_name(self, tenant_id: str, name: str) -> Supplier | None:
        target = name.strip().lower()
        with self._lock, self._db() as conn:
            rows = conn.execute("SELECT * FROM suppliers WHERE tenant_id = ? AND active = 1", (tenant_id,)).fetchall()
        for row in rows:
            if row["name"].strip().lower() == target:
                return self._row_to_supplier(row)
        return None

    def list_for_tenant(self, tenant_id: str, *, active_only: bool = False) -> list[Supplier]:
        clause = "tenant_id = ?" + (" AND active = 1" if active_only else "")
        with self._lock, self._db() as conn:
            rows = conn.execute(f"SELECT * FROM suppliers WHERE {clause} ORDER BY name", (tenant_id,)).fetchall()
            return [self._row_to_supplier(r) for r in rows]

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM suppliers WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount
