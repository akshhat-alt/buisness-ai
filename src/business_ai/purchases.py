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

# Conversion factors into one canonical base unit per physical quantity —
# the only units this codebase will ever silently reconcile. An owner who
# buys 5kg chicken one week and 500g the next is a completely normal
# purchasing pattern, not a data-entry error, so purchases within the same
# family (mass or volume) are converted to a common base before summing.
# A unit outside these two families (e.g. "pieces", "dozen", "box") is
# never guessed at — see PurchaseUnitMismatchError.
_MASS_TO_GRAMS: dict[str, float] = {
    "g": 1.0, "gm": 1.0, "gms": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kgs": 1000.0, "kilo": 1000.0, "kilos": 1000.0,
    "kilogram": 1000.0, "kilograms": 1000.0,
}
_VOLUME_TO_ML: dict[str, float] = {
    "ml": 1.0, "mls": 1.0, "millilitre": 1.0, "milliliter": 1.0,
    "millilitres": 1.0, "milliliters": 1.0,
    "l": 1000.0, "ltr": 1000.0, "ltrs": 1000.0, "litre": 1000.0,
    "liter": 1000.0, "litres": 1000.0, "liters": 1000.0,
}


class PurchaseUnitMismatchError(ValueError):
    """Raised by sum_for_ingredient when one ingredient's purchase
    history mixes units that aren't safely convertible into each other
    (e.g. "kg" and "pieces"). Unlike InventoryStore.adjust_quantity —
    which has no conversion table and refuses ANY unit disagreement —
    this store DOES reconcile common mass (g/kg) and volume (ml/L)
    units, since owners routinely buy the same ingredient in different
    package sizes over time. This error only fires for a genuine
    incompatibility, where averaging would produce a meaningless
    number; the fix is correcting the purchase record's unit, not
    guessing a conversion that doesn't exist."""


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
        up would be an incomplete store.

        Purchases logged in different but compatible units (g vs kg,
        ml vs L) are converted to one canonical unit (g or ml) before
        summing, so total_quantity/total_amount_inr always describe the
        same physical unit and an average price-per-unit is meaningful.
        Purchases already sharing one literal unit are summed directly
        in that unit, unchanged from before, so an ingredient nobody
        has ever mixed units for keeps reporting in whatever unit it
        was actually logged in. Raises PurchaseUnitMismatchError if the
        ingredient's purchases mix genuinely incompatible units (e.g.
        "kg" and "pieces") — see that class's docstring.

        The returned "unit" is the unit total_quantity is expressed in
        (None when there's no purchase history at all)."""
        key = normalize_ingredient_name(ingredient_name)
        clause = "tenant_id = ?" + (" AND created_at >= ?" if since_iso else "")
        params: tuple = (tenant_id, since_iso) if since_iso else (tenant_id,)
        with self._lock, self._db() as conn:
            rows = conn.execute(f"SELECT ingredient_name, quantity, unit, amount_inr FROM purchases WHERE {clause}", params).fetchall()
        matching = [r for r in rows if normalize_ingredient_name(r["ingredient_name"]) == key]
        if not matching:
            return {
                "ingredient_name": ingredient_name, "total_quantity": 0.0,
                "total_amount_inr": 0, "purchase_count": 0, "unit": None,
            }

        families: dict[str, list[tuple]] = {}
        for r in matching:
            unit = r["unit"].strip().lower()
            if unit in _MASS_TO_GRAMS:
                family = "mass"
            elif unit in _VOLUME_TO_ML:
                family = "volume"
            else:
                family = f"literal:{unit}"
            families.setdefault(family, []).append(r)

        if len(families) > 1:
            used_units = sorted({r["unit"].strip().lower() for r in matching})
            raise PurchaseUnitMismatchError(
                f"{ingredient_name!r} has purchases logged in incompatible units ({', '.join(used_units)}) — "
                f"fix the purchase records to use compatible units (e.g. g/kg or ml/L) before a total can be computed."
            )

        (family,) = families.keys()
        literal_units = {r["unit"].strip().lower() for r in matching}
        if family in ("mass", "volume") and len(literal_units) > 1:
            table = _MASS_TO_GRAMS if family == "mass" else _VOLUME_TO_ML
            unit = "g" if family == "mass" else "ml"
            total_quantity = sum(r["quantity"] * table[r["unit"].strip().lower()] for r in matching)
        else:
            unit = matching[0]["unit"]
            total_quantity = sum(r["quantity"] for r in matching)

        return {
            "ingredient_name": ingredient_name, "total_quantity": total_quantity,
            "total_amount_inr": sum(r["amount_inr"] for r in matching), "purchase_count": len(matching),
            "unit": unit,
        }

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM purchases WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount
