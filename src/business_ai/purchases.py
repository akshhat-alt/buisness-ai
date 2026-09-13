"""Restaurant Foundation (Phase 17): purchase receipts.

Logging a purchase does two things atomically-in-intent (this codebase
never uses cross-store SQL transactions — every multi-store write here
follows the same "each store commits its own row, best-effort order"
convention already used by tenant_data.py's delete and revenue_radar's
reads): records the purchase as its own row (supplier, cost, quantity)
AND increments `InventoryStore`'s stock for that ingredient. The receipt
is the source of truth for "we now have more stock" — never edited
directly on the inventory row itself.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from pydantic import BaseModel

from business_ai.menu import normalize_ingredient_name
from business_ai.storage import SqliteStore


class Purchase(BaseModel):
    purchase_id: str
    tenant_id: str
    ingredient_name: str
    quantity: float
    unit: str
    amount_inr: int
    supplier_id: str | None = None
    reported_by_employee_id: str | None = None
    created_at: str


class PurchaseStore(SqliteStore):
    def __init__(self, db_path: Path | str = "data/purchases.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS purchases (
                    purchase_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    ingredient_name TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    unit TEXT NOT NULL,
                    amount_inr INTEGER NOT NULL,
                    supplier_id TEXT,
                    reported_by_employee_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_purchases_tenant ON purchases(tenant_id)")
            conn.commit()

    def record(
        self, *, tenant_id: str, ingredient_name: str, quantity: float, unit: str, amount_inr: int,
        supplier_id: str | None = None, reported_by_employee_id: str | None = None,
    ) -> Purchase:
        if quantity <= 0:
            raise ValueError("quantity must be positive.")
        if amount_inr < 0:
            raise ValueError("amount_inr cannot be negative.")
        purchase = Purchase(
            purchase_id=f"pur_{secrets.token_hex(8)}", tenant_id=tenant_id, ingredient_name=ingredient_name.strip(),
            quantity=quantity, unit=unit.strip().lower(), amount_inr=amount_inr, supplier_id=supplier_id,
            reported_by_employee_id=reported_by_employee_id, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO purchases "
                "(purchase_id, tenant_id, ingredient_name, quantity, unit, amount_inr, supplier_id, reported_by_employee_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    purchase.purchase_id, purchase.tenant_id, purchase.ingredient_name, purchase.quantity, purchase.unit,
                    purchase.amount_inr, purchase.supplier_id, purchase.reported_by_employee_id, purchase.created_at,
                ),
            )
            conn.commit()
        return purchase

    def list_for_tenant(self, tenant_id: str, *, since_iso: str | None = None, limit: int = 500) -> list[Purchase]:
        clause = "tenant_id = ?" + (" AND created_at >= ?" if since_iso else "")
        params: tuple = (tenant_id, since_iso) if since_iso else (tenant_id,)
        with self._lock, self._db() as conn:
            rows = conn.execute(
                f"SELECT * FROM purchases WHERE {clause} ORDER BY created_at DESC LIMIT ?", (*params, limit),
            ).fetchall()
            return [Purchase(**dict(r)) for r in rows]

    def sum_for_ingredient(self, tenant_id: str, ingredient_name: str, *, since_iso: str | None = None) -> dict:
        """Total quantity/spend for one ingredient — the raw material
        Phase 23's supplier-price-trend intelligence will read; built
        now because logging purchases without any way to look them back
        up would be an incomplete store."""
        key = normalize_ingredient_name(ingredient_name)
        clause = "tenant_id = ?" + (" AND created_at >= ?" if since_iso else "")
        params: tuple = (tenant_id, since_iso) if since_iso else (tenant_id,)
        with self._lock, self._db() as conn:
            rows = conn.execute(f"SELECT ingredient_name, quantity, amount_inr FROM purchases WHERE {clause}", params).fetchall()
        matching = [r for r in rows if normalize_ingredient_name(r["ingredient_name"]) == key]
        return {
            "ingredient_name": ingredient_name, "total_quantity": sum(r["quantity"] for r in matching),
            "total_amount_inr": sum(r["amount_inr"] for r in matching), "purchase_count": len(matching),
        }

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM purchases WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount
