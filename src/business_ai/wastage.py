"""Restaurant Foundation (Phase 17): wastage logging.

Every wastage event records a cost estimate at write time (quantity ×
the ingredient's own average purchase price, when known — never a
guessed number when there's no purchase history yet, in which case the
cost is honestly recorded as 0 rather than invented). Depletes
`InventoryStore` the same way a sale does — spoilage is stock leaving
the kitchen just as surely as a dish going out the door.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from pydantic import BaseModel

from business_ai.storage import SqliteStore

WASTAGE_REASONS = frozenset({"spoiled", "prep_error", "returned", "other"})


class WastageEntry(BaseModel):
    wastage_id: str
    tenant_id: str
    ingredient_name: str
    quantity: float
    unit: str
    reason: str
    estimated_cost_inr: int
    reported_by_employee_id: str | None = None
    created_at: str


class WastageStore(SqliteStore):
    def __init__(self, db_path: Path | str = "data/wastage.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wastage_entries (
                    wastage_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    ingredient_name TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    unit TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    estimated_cost_inr INTEGER NOT NULL DEFAULT 0,
                    reported_by_employee_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wastage_tenant ON wastage_entries(tenant_id)")
            conn.commit()

    def record(
        self, *, tenant_id: str, ingredient_name: str, quantity: float, unit: str, reason: str = "other",
        estimated_cost_inr: int = 0, reported_by_employee_id: str | None = None,
    ) -> WastageEntry:
        if quantity <= 0:
            raise ValueError("quantity must be positive.")
        reason = reason.strip().lower() if reason.strip().lower() in WASTAGE_REASONS else "other"
        entry = WastageEntry(
            wastage_id=f"waste_{secrets.token_hex(8)}", tenant_id=tenant_id, ingredient_name=ingredient_name.strip(),
            quantity=quantity, unit=unit.strip().lower(), reason=reason, estimated_cost_inr=max(0, estimated_cost_inr),
            reported_by_employee_id=reported_by_employee_id, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO wastage_entries "
                "(wastage_id, tenant_id, ingredient_name, quantity, unit, reason, estimated_cost_inr, reported_by_employee_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.wastage_id, entry.tenant_id, entry.ingredient_name, entry.quantity, entry.unit, entry.reason,
                    entry.estimated_cost_inr, entry.reported_by_employee_id, entry.created_at,
                ),
            )
            conn.commit()
        return entry

    def list_for_tenant(self, tenant_id: str, *, since_iso: str | None = None, limit: int = 500) -> list[WastageEntry]:
        clause = "tenant_id = ?" + (" AND created_at >= ?" if since_iso else "")
        params: tuple = (tenant_id, since_iso) if since_iso else (tenant_id,)
        with self._lock, self._db() as conn:
            rows = conn.execute(
                f"SELECT * FROM wastage_entries WHERE {clause} ORDER BY created_at DESC LIMIT ?", (*params, limit),
            ).fetchall()
            return [WastageEntry(**dict(r)) for r in rows]

    def total_cost_for_tenant(self, tenant_id: str, *, since_iso: str | None = None) -> int:
        clause = "tenant_id = ?" + (" AND created_at >= ?" if since_iso else "")
        params: tuple = (tenant_id, since_iso) if since_iso else (tenant_id,)
        with self._lock, self._db() as conn:
            row = conn.execute(f"SELECT SUM(estimated_cost_inr) as total FROM wastage_entries WHERE {clause}", params).fetchone()
            return row["total"] or 0

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM wastage_entries WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount
