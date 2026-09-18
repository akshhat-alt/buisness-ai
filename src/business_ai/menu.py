"""Restaurant Foundation (Phase 17): the menu and its recipes.

A `MenuItem` and its `RecipeLine`s are one aggregate — a menu item without
a recipe is incomplete for anything this store's data feeds (stock
depletion, food-cost %, later menu engineering), and a recipe line has no
independent lifecycle outside its menu item. One store, two related
tables, exactly the same `SqliteStore` pattern as every other store in
this codebase.

Ingredient identity is a normalized name (case/whitespace-insensitive),
reusing the EXACT idiom `dependency_graph._normalize_title` already
established for task-title process grouping — never a new invented
normalization scheme. `InventoryStore` (inventory.py) keys stock rows by
this same normalized name so a recipe line always resolves to the right
stock row regardless of how an owner capitalized/spaced the ingredient
when they typed it.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from pydantic import BaseModel

from business_ai.storage import SqliteStore


def normalize_ingredient_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


class MenuItem(BaseModel):
    menu_item_id: str
    tenant_id: str
    name: str
    price_inr: int
    category: str = ""
    active: bool = True
    created_at: str


class RecipeLine(BaseModel):
    recipe_line_id: str
    tenant_id: str
    menu_item_id: str
    ingredient_name: str  # display form, as the owner typed it
    ingredient_key: str  # normalize_ingredient_name(ingredient_name) — matches InventoryStore
    quantity: float
    unit: str


class MenuStore(SqliteStore):
    def __init__(self, db_path: Path | str = "data/menu.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS menu_items (
                    menu_item_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    price_inr INTEGER NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recipe_lines (
                    recipe_line_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    menu_item_id TEXT NOT NULL,
                    ingredient_name TEXT NOT NULL,
                    ingredient_key TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    unit TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_menu_items_tenant ON menu_items(tenant_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_recipe_lines_tenant_item ON recipe_lines(tenant_id, menu_item_id)")
            conn.commit()

    # ---------------------------------------------------------- menu items

    def create_item(self, *, tenant_id: str, name: str, price_inr: int, category: str = "") -> MenuItem:
        if not name.strip():
            raise ValueError("Menu item name is required.")
        if price_inr <= 0:
            raise ValueError("price_inr must be a positive whole number.")
        item = MenuItem(
            menu_item_id=f"dish_{secrets.token_hex(8)}", tenant_id=tenant_id, name=name.strip(),
            price_inr=price_inr, category=category.strip(), created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO menu_items (menu_item_id, tenant_id, name, price_inr, category, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item.menu_item_id, item.tenant_id, item.name, item.price_inr, item.category, int(item.active), item.created_at),
            )
            conn.commit()
        return item

    def get_item(self, tenant_id: str, menu_item_id: str) -> MenuItem | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM menu_items WHERE tenant_id = ? AND menu_item_id = ?", (tenant_id, menu_item_id),
            ).fetchone()
            return self._row_to_item(row) if row else None

    def find_by_name(self, tenant_id: str, name: str) -> MenuItem | None:
        """Case/whitespace-insensitive lookup — the WhatsApp `log sale
        <dish>` command matches on exactly this, since a busy employee
        typing a dish name will never match the stored case exactly."""
        target = normalize_ingredient_name(name)
        with self._lock, self._db() as conn:
            rows = conn.execute("SELECT * FROM menu_items WHERE tenant_id = ? AND active = 1", (tenant_id,)).fetchall()
        for row in rows:
            if normalize_ingredient_name(row["name"]) == target:
                return self._row_to_item(row)
        return None

    def list_for_tenant(self, tenant_id: str, *, active_only: bool = False) -> list[MenuItem]:
        clause = "tenant_id = ?" + (" AND active = 1" if active_only else "")
        with self._lock, self._db() as conn:
            rows = conn.execute(f"SELECT * FROM menu_items WHERE {clause} ORDER BY category, name", (tenant_id,)).fetchall()
            return [self._row_to_item(r) for r in rows]

    def update_item(self, tenant_id: str, menu_item_id: str, **fields) -> MenuItem | None:
        if not fields:
            return self.get_item(tenant_id, menu_item_id)
        allowed = {"name", "price_inr", "category", "active"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Cannot update field(s): {sorted(unknown)}")
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        with self._lock, self._db() as conn:
            conn.execute(
                f"UPDATE menu_items SET {set_clause} WHERE tenant_id = ? AND menu_item_id = ?",
                (*values, tenant_id, menu_item_id),
            )
            conn.commit()
        return self.get_item(tenant_id, menu_item_id)

    def _row_to_item(self, row) -> MenuItem:
        data = dict(row)
        data["active"] = bool(data["active"])
        return MenuItem(**data)

    # ---------------------------------------------------------- recipes

    def set_recipe(self, tenant_id: str, menu_item_id: str, lines: list[dict]) -> list[RecipeLine]:
        """Replaces the entire recipe for a menu item — same
        "caller sends the whole desired state, we replace" shape as
        automation-rule updates elsewhere in this codebase. Each line
        dict needs ingredient_name/quantity/unit."""
        recipe_lines = []
        for line in lines:
            quantity = float(line["quantity"])
            if quantity <= 0:
                raise ValueError(f"Recipe quantity must be positive: {line!r}")
            recipe_lines.append(RecipeLine(
                recipe_line_id=f"rline_{secrets.token_hex(8)}", tenant_id=tenant_id, menu_item_id=menu_item_id,
                ingredient_name=line["ingredient_name"].strip(),
                ingredient_key=normalize_ingredient_name(line["ingredient_name"]),
                quantity=quantity, unit=line["unit"].strip().lower(),
            ))
        with self._lock, self._db() as conn:
            conn.execute("DELETE FROM recipe_lines WHERE tenant_id = ? AND menu_item_id = ?", (tenant_id, menu_item_id))
            for rl in recipe_lines:
                conn.execute(
                    "INSERT INTO recipe_lines (recipe_line_id, tenant_id, menu_item_id, ingredient_name, ingredient_key, quantity, unit) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (rl.recipe_line_id, rl.tenant_id, rl.menu_item_id, rl.ingredient_name, rl.ingredient_key, rl.quantity, rl.unit),
                )
            conn.commit()
        return recipe_lines

    def get_recipe(self, tenant_id: str, menu_item_id: str) -> list[RecipeLine]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM recipe_lines WHERE tenant_id = ? AND menu_item_id = ?", (tenant_id, menu_item_id),
            ).fetchall()
            return [RecipeLine(**dict(r)) for r in rows]

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur1 = conn.execute("DELETE FROM recipe_lines WHERE tenant_id = ?", (tenant_id,))
            cur2 = conn.execute("DELETE FROM menu_items WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur1.rowcount + cur2.rowcount


def live_menu_evidence_items(*, tenant_id: str, query: str, menu_store: "MenuStore") -> list:
    """Phase 3 — closes the "stale menu price" gap: a price change in
    MenuStore (the source of truth food-cost/menu-engineering already
    reads) previously had no way to reach the customer-facing assistant
    except via a full knowledge-base re-index, so a changed price could
    sit stale in the RAG index indefinitely. Rather than adding LLM tool
    calling (a real new architecture surface, not justified for this one
    gap), this does a simple deterministic substring match: any active
    menu item whose name appears in the customer's query gets injected
    as a synthetic, maximum-confidence EvidenceItem carrying its CURRENT
    price — same trust tier as this app's other deterministic-data
    answers (financials, etc.), flowing through the exact same
    abstention/citation/generation pipeline as any other evidence, not a
    side channel. Returns [] for any tenant with no menu items (i.e.
    every non-restaurant tenant) or no name match — a total no-op for
    the common case, by construction.

    Local import of retrieval.py to avoid a circular import (retrieval.py
    does not import menu.py)."""
    from business_ai.retrieval import Citation, EvidenceItem

    if not query:
        return []
    items = menu_store.list_for_tenant(tenant_id, active_only=True)
    if not items:
        return []
    query_lower = query.lower()
    evidence: list = []
    for item in items:
        if item.name.strip().lower() in query_lower:
            evidence.append(
                EvidenceItem(
                    segment_id=f"live_menu_{item.menu_item_id}",
                    source_id="live_menu",
                    text=f"{item.name}: ₹{item.price_inr}",
                    confidence=1.0,
                    citation=Citation(label="Your current menu"),
                    tenant_id=tenant_id,
                )
            )
    return evidence
