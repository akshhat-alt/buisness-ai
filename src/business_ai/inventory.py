"""Restaurant Foundation (Phase 17): ingredient-level stock.

Keyed by the same normalized ingredient name `menu.py`'s recipe lines
use, so a recipe always resolves to the right stock row regardless of
how an owner capitalized/spaced an ingredient. `quantity_on_hand` moves
in exactly two ways in this codebase: up via `purchases.py` receiving
stock, down via a recipe-based sale depletion or `wastage.py` — never
edited directly, so the number is always an honest sum of real events,
the same "never invented, always derived from real rows" discipline as
`BusinessMetricStore`.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from pydantic import BaseModel

from business_ai.menu import normalize_ingredient_name
from business_ai.storage import SqliteStore


class UnitMismatchError(ValueError):
    """Raised by adjust_quantity when the caller's unit doesn't match
    the ingredient's already-established unit. Found during Phase 17's
    own live-verification audit: silently accepting a mismatched unit
    (e.g. a 5kg purchase followed by a 200g depletion) computed a
    confidently wrong quantity_on_hand — this codebase's "fail closed,
    never silently corrupt a number" discipline applies here exactly
    like it does to authorize(). No unit-conversion table is built
    (that's real, separate scope); the fix is refusing the mismatch and
    telling the caller to use a consistent unit, not guessing a
    conversion."""


class InventoryItem(BaseModel):
    inventory_item_id: str
    tenant_id: str
    ingredient_name: str  # display form, most-recently-used casing
    ingredient_key: str
    unit: str
    quantity_on_hand: float
    par_level: float = 0.0  # 0 = no low-stock alerting configured for this ingredient yet
    updated_at: str


class InventoryStore(SqliteStore):
    def __init__(self, db_path: Path | str = "data/inventory.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inventory_items (
                    inventory_item_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    ingredient_name TEXT NOT NULL,
                    ingredient_key TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    quantity_on_hand REAL NOT NULL DEFAULT 0,
                    par_level REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, ingredient_key)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_tenant ON inventory_items(tenant_id)")
            conn.commit()

    def _row_to_item(self, row) -> InventoryItem:
        return InventoryItem(**dict(row))

    def get(self, tenant_id: str, ingredient_name: str) -> InventoryItem | None:
        key = normalize_ingredient_name(ingredient_name)
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_items WHERE tenant_id = ? AND ingredient_key = ?", (tenant_id, key),
            ).fetchone()
            return self._row_to_item(row) if row else None

    def list_for_tenant(self, tenant_id: str) -> list[InventoryItem]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM inventory_items WHERE tenant_id = ? ORDER BY ingredient_name", (tenant_id,),
            ).fetchall()
            return [self._row_to_item(r) for r in rows]

    def set_par_level(self, tenant_id: str, ingredient_name: str, *, par_level: float, unit: str | None = None) -> InventoryItem:
        """Defines (or redefines) an ingredient for tracking — creates
        the row at zero stock if it doesn't exist yet, so an owner can
        set a par level before the first purchase is ever logged.

        Unlike `adjust_quantity`, a mismatched `unit` here is allowed to
        overwrite — this is an explicit, deliberate owner/manager setup
        action (via the dashboard/API, not a fast WhatsApp log command),
        and "I set this in kg but meant g, let me fix it" is a real,
        wanted capability here, not a transaction that could silently
        drift."""
        key = normalize_ingredient_name(ingredient_name)
        existing = self.get(tenant_id, ingredient_name)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._db() as conn:
            if existing:
                conn.execute(
                    "UPDATE inventory_items SET par_level = ?, unit = COALESCE(?, unit), updated_at = ? "
                    "WHERE tenant_id = ? AND ingredient_key = ?",
                    (par_level, unit, now, tenant_id, key),
                )
            else:
                conn.execute(
                    "INSERT INTO inventory_items "
                    "(inventory_item_id, tenant_id, ingredient_name, ingredient_key, unit, quantity_on_hand, par_level, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                    (f"inv_{secrets.token_hex(8)}", tenant_id, ingredient_name.strip(), key, (unit or "units").strip().lower(), par_level, now),
                )
            conn.commit()
        return self.get(tenant_id, ingredient_name)  # type: ignore[return-value]

    def adjust_quantity(self, tenant_id: str, ingredient_name: str, *, delta: float, unit: str | None = None) -> InventoryItem:
        """The only way `quantity_on_hand` ever changes. A positive
        delta is a receipt (purchase); a negative delta is a depletion
        (sale or wastage). Auto-creates the ingredient row at zero par
        level if this is the first time it's ever been mentioned — an
        owner who never explicitly set a par level still gets accurate
        stock tracking, just no low-stock alert until they do.

        Raises UnitMismatchError if `unit` disagrees with the
        ingredient's already-established unit — see that class's own
        docstring for why this must fail closed, not silently convert
        or overwrite."""
        key = normalize_ingredient_name(ingredient_name)
        existing = self.get(tenant_id, ingredient_name)
        if existing and unit and unit.strip().lower() != existing.unit.strip().lower():
            raise UnitMismatchError(
                f"{ingredient_name!r} is tracked in {existing.unit!r}, not {unit!r} — "
                f"use {existing.unit!r} consistently for this ingredient."
            )
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock, self._db() as conn:
            if existing:
                conn.execute(
                    "UPDATE inventory_items SET quantity_on_hand = quantity_on_hand + ?, "
                    "ingredient_name = ?, updated_at = ? "
                    "WHERE tenant_id = ? AND ingredient_key = ?",
                    (delta, ingredient_name.strip(), now, tenant_id, key),
                )
            else:
                conn.execute(
                    "INSERT INTO inventory_items "
                    "(inventory_item_id, tenant_id, ingredient_name, ingredient_key, unit, quantity_on_hand, par_level, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (f"inv_{secrets.token_hex(8)}", tenant_id, ingredient_name.strip(), key, (unit or "units").strip().lower(), delta, now),
                )
            conn.commit()
        return self.get(tenant_id, ingredient_name)  # type: ignore[return-value]

    def list_low_stock(self, tenant_id: str) -> list[InventoryItem]:
        """Only ingredients with a real par level configured (> 0) can
        ever be "low" — an ingredient nobody set a threshold for is
        never flagged, matching this codebase's rule of never inventing
        a judgment the owner hasn't actually configured."""
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM inventory_items WHERE tenant_id = ? AND par_level > 0 AND quantity_on_hand < par_level "
                "ORDER BY ingredient_name",
                (tenant_id,),
            ).fetchall()
            return [self._row_to_item(r) for r in rows]

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM inventory_items WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount
