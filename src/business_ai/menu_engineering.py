"""Restaurant Profitability Intelligence (Phase 22): pure derived
computation over stores that already exist — no new logging mechanism,
no new SQLite table. Mirrors scorecard.py/revenue_radar.py's own shape
exactly: stores passed as keyword function args, Pydantic models only
for shaping OUTPUT, render_*_whatsapp() functions living in this same
module for the matching admin-bot command.

Two independent reports:
  - build_menu_engineering_report(): per-dish food cost %, profit, and
    the classic four-quadrant menu-engineering classification (Star /
    Plowhorse / Puzzle / Dog), plus a simple demand-forecast baseline
    (a trailing daily average, never a real forecasting model — "baseline"
    is the honest word for it).
  - build_reorder_suggestions(): ingredients that have genuinely
    triggered a low_stock automation alert repeatedly, each with a
    conservative, bounded suggested par_level bump for the owner to
    review and apply — never auto-applied.

Same "never invent a number" discipline as wastage.py's cost estimate
and purchases.py's average-price calculation: a dish with no recipe, or
a recipe with an ingredient that has no purchase history, reports its
food cost / profit as None (unknown) rather than a silently wrong ₹0.
"""

from __future__ import annotations

import time

from pydantic import BaseModel

from business_ai.constants import (
    MENU_ENGINEERING_WINDOW_DAYS,
    REORDER_SUGGESTION_INCREASE_PCT,
    REORDER_SUGGESTION_MIN_TRIGGER_COUNT,
)
from business_ai.purchases import PurchaseUnitMismatchError


class DishProfitability(BaseModel):
    menu_item_id: str
    name: str
    price_inr: int
    has_recipe: bool
    recipe_cost_inr: float | None = None  # None = unknown (no recipe, or an ingredient with no purchase history)
    food_cost_pct: float | None = None
    quantity_sold: int
    revenue_inr: int
    profit_inr: float | None = None  # TOTAL profit over the window (revenue - cost * quantity_sold) — for the P&L view
    profit_margin_inr: float | None = None  # PER-UNIT contribution margin (price - cost) — drives menu-engineering classification
    avg_daily_quantity: float = 0.0
    projected_next_7_days_quantity: float = 0.0
    classification: str | None = None  # "star" | "plowhorse" | "puzzle" | "dog" | None (not enough data)


class MenuEngineeringReport(BaseModel):
    window_days: int
    dishes: list[DishProfitability]
    avg_quantity_sold: float  # the popularity threshold actually used
    avg_profit_inr: float | None  # the average PER-UNIT margin threshold actually used for classification; None if no dish has a known margin


CLASSIFICATION_LABELS = {
    "star": "⭐ Star — popular and profitable",
    "plowhorse": "🐴 Plowhorse — popular, thin margin",
    "puzzle": "🧩 Puzzle — profitable, rarely ordered",
    "dog": "🐕 Dog — low demand and low margin",
}


def _recipe_cost(tenant_id: str, menu_item_id: str, *, menu_store, purchase_store) -> float | None:
    """Sums quantity * (average price per unit from purchase history) for
    every recipe line — the exact technique wastage.py's cost estimate
    already uses. Returns None (not 0) when the recipe is empty, ANY
    ingredient has no purchase history yet, or ANY ingredient's purchase
    history mixes genuinely incompatible units (PurchaseUnitMismatchError)
    — matches purchases.py/wastage.py's own "never invent a cost"
    discipline: a partially-priced recipe cost would be a silently wrong
    total, not an honest estimate."""
    lines = menu_store.get_recipe(tenant_id, menu_item_id)
    if not lines:
        return None
    total = 0.0
    for line in lines:
        try:
            summary = purchase_store.sum_for_ingredient(tenant_id, line.ingredient_name)
        except PurchaseUnitMismatchError:
            return None  # inconsistent purchase data makes this ingredient's price unknown, not guessable
        if summary["total_quantity"] <= 0:
            return None  # any unpriceable ingredient makes the whole recipe cost unknown, not partially wrong
        cost_per_unit = summary["total_amount_inr"] / summary["total_quantity"]
        total += cost_per_unit * line.quantity
    return total


def build_menu_engineering_report(
    tenant_id: str, *, menu_store, purchase_store, metric_store,
    window_days: int = MENU_ENGINEERING_WINDOW_DAYS,
) -> MenuEngineeringReport:
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - window_days * 86400))
    dishes: list[DishProfitability] = []
    for item in menu_store.list_for_tenant(tenant_id, active_only=True):
        sales = metric_store.list_dish_sales(tenant_id, item.menu_item_id, since_iso=since_iso)
        quantity_sold = sum(int(s.quantity or 0) for s in sales)
        revenue_inr = sum(s.amount_inr for s in sales)
        recipe_lines = menu_store.get_recipe(tenant_id, item.menu_item_id)
        recipe_cost_inr = _recipe_cost(tenant_id, item.menu_item_id, menu_store=menu_store, purchase_store=purchase_store)
        food_cost_pct = (recipe_cost_inr / item.price_inr * 100) if recipe_cost_inr is not None and item.price_inr > 0 else None
        profit_inr = (revenue_inr - recipe_cost_inr * quantity_sold) if recipe_cost_inr is not None else None
        profit_margin_inr = (item.price_inr - recipe_cost_inr) if recipe_cost_inr is not None else None
        avg_daily_quantity = quantity_sold / window_days if window_days > 0 else 0.0
        dishes.append(DishProfitability(
            menu_item_id=item.menu_item_id, name=item.name, price_inr=item.price_inr,
            has_recipe=bool(recipe_lines), recipe_cost_inr=recipe_cost_inr, food_cost_pct=food_cost_pct,
            quantity_sold=quantity_sold, revenue_inr=revenue_inr, profit_inr=profit_inr,
            profit_margin_inr=profit_margin_inr,
            avg_daily_quantity=round(avg_daily_quantity, 2),
            projected_next_7_days_quantity=round(avg_daily_quantity * 7, 1),
        ))

    sold_dishes = [d for d in dishes if d.quantity_sold > 0]
    avg_quantity_sold = sum(d.quantity_sold for d in sold_dishes) / len(sold_dishes) if sold_dishes else 0.0
    # Classification compares PER-UNIT contribution margin, not total
    # profit dollars — a dish ordered rarely but with a fat per-item
    # margin (a real "Puzzle") must never be lumped in with a genuinely
    # thin-margin "Dog" just because it sold few units.
    margined_dishes = [d for d in dishes if d.profit_margin_inr is not None and d.quantity_sold > 0]
    avg_profit_inr = sum(d.profit_margin_inr for d in margined_dishes) / len(margined_dishes) if margined_dishes else None

    for dish in dishes:
        if dish.quantity_sold == 0 or dish.profit_margin_inr is None or avg_profit_inr is None:
            continue  # not enough data to classify honestly
        popular = dish.quantity_sold >= avg_quantity_sold
        profitable = dish.profit_margin_inr >= avg_profit_inr
        if popular and profitable:
            dish.classification = "star"
        elif popular and not profitable:
            dish.classification = "plowhorse"
        elif not popular and profitable:
            dish.classification = "puzzle"
        else:
            dish.classification = "dog"

    return MenuEngineeringReport(
        window_days=window_days, dishes=dishes,
        avg_quantity_sold=round(avg_quantity_sold, 2),
        avg_profit_inr=round(avg_profit_inr, 2) if avg_profit_inr is not None else None,
    )


def render_menu_engineering_whatsapp(report: MenuEngineeringReport) -> str:
    if not report.dishes:
        return "No active menu items yet — add one from the dashboard's Restaurant section first."
    lines = [f"📊 Menu engineering (last {report.window_days} days):"]
    for dish in sorted(report.dishes, key=lambda d: d.quantity_sold, reverse=True)[:10]:
        cost_note = f"{dish.food_cost_pct:.0f}% food cost" if dish.food_cost_pct is not None else "food cost unknown — no priced recipe"
        label = CLASSIFICATION_LABELS.get(dish.classification, "not enough data yet")
        lines.append(f"• {dish.name}: {dish.quantity_sold} sold, {cost_note} — {label}")
    return "\n".join(lines)


class ReorderSuggestion(BaseModel):
    ingredient_name: str
    ingredient_key: str
    unit: str
    quantity_on_hand: float
    par_level: float
    low_stock_trigger_count: int
    suggested_par_level: float


class ReorderSuggestionsReport(BaseModel):
    suggestions: list[ReorderSuggestion]


def build_reorder_suggestions(
    tenant_id: str, *, inventory_store, automation_run_store,
    min_trigger_count: int = REORDER_SUGGESTION_MIN_TRIGGER_COUNT,
    increase_pct: float = REORDER_SUGGESTION_INCREASE_PCT,
) -> ReorderSuggestionsReport:
    """Reuses Phase 19's own automation-run history — an ingredient can
    only ever have low_stock run rows if it already has a real,
    owner-configured par_level > 0 (list_low_stock's own filter), so this
    never invents a suggestion for an ingredient nobody has set a
    threshold for. Counts every recorded run for that (trigger_type,
    target_id) pair regardless of success/failure — a failed WhatsApp
    send doesn't mean the underlying stock problem wasn't real."""
    runs = automation_run_store.list_for_tenant(tenant_id, limit=1000)
    counts: dict[str, int] = {}
    for run in runs:
        if run.trigger_type == "low_stock" and run.target_id.startswith("inventory:"):
            counts[run.target_id] = counts.get(run.target_id, 0) + 1

    suggestions: list[ReorderSuggestion] = []
    for item in inventory_store.list_for_tenant(tenant_id):
        count = counts.get(f"inventory:{item.ingredient_key}", 0)
        if count < min_trigger_count or item.par_level <= 0:
            continue
        suggestions.append(ReorderSuggestion(
            ingredient_name=item.ingredient_name, ingredient_key=item.ingredient_key, unit=item.unit,
            quantity_on_hand=item.quantity_on_hand, par_level=item.par_level,
            low_stock_trigger_count=count,
            suggested_par_level=round(item.par_level * (1 + increase_pct), 2),
        ))
    suggestions.sort(key=lambda s: s.low_stock_trigger_count, reverse=True)
    return ReorderSuggestionsReport(suggestions=suggestions)


def render_reorder_suggestions_whatsapp(report: ReorderSuggestionsReport) -> str:
    if not report.suggestions:
        return "No reorder suggestions right now — nothing has run low repeatedly."
    lines = ["📦 Reorder suggestions (ingredients running low repeatedly):"]
    for s in report.suggestions[:10]:
        lines.append(
            f"• {s.ingredient_name}: par level {s.par_level:g}{s.unit} → suggest {s.suggested_par_level:g}{s.unit} "
            f"(triggered {s.low_stock_trigger_count}x)"
        )
    return "\n".join(lines)
