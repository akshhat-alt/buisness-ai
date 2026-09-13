"""Tests for Phase 22's Restaurant Profitability Intelligence — pure
derived computation over stores that already exist (menu, purchases,
metrics, inventory, automation runs), matching scorecard.py/
revenue_radar.py's own "stores as keyword args, no new SQLite table"
shape. Exercised against real store instances (temp SQLite), no mocks,
this codebase's established testing convention.
"""

from __future__ import annotations

from business_ai.automation import AutomationRunStore, RunStatus
from business_ai.inventory import InventoryStore
from business_ai.menu import MenuStore
from business_ai.menu_engineering import (
    build_menu_engineering_report,
    build_menu_recommendations,
    build_reorder_suggestions,
    simulate_menu_item_price,
)
from business_ai.metrics import BusinessMetricStore
from business_ai.purchases import PurchaseStore

TENANT = "trattoria-a"


# ------------------------------------------------------------------ food cost / profitability


def test_dish_with_no_recipe_reports_unknown_cost_and_profit(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    menu.create_item(tenant_id=TENANT, name="Lassi", price_inr=80)

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    dish = report.dishes[0]
    assert dish.has_recipe is False
    assert dish.recipe_cost_inr is None
    assert dish.food_cost_pct is None
    assert dish.profit_inr is None
    assert dish.classification is None  # no signal to classify from


def test_dish_with_recipe_but_no_purchase_history_reports_unknown_cost(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    item = menu.create_item(tenant_id=TENANT, name="Butter Chicken", price_inr=350)
    menu.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "Chicken", "quantity": 200, "unit": "g"}])
    # No purchase history logged for Chicken at all.

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    dish = report.dishes[0]
    assert dish.has_recipe is True
    assert dish.recipe_cost_inr is None  # can't price it — never silently ₹0
    assert dish.profit_inr is None


def test_dish_with_priced_recipe_computes_cost_and_profit(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    item = menu.create_item(tenant_id=TENANT, name="Butter Chicken", price_inr=350)
    menu.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "Chicken", "quantity": 200, "unit": "g"}])
    purchases.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1000, unit="g", amount_inr=500)  # ₹0.5/g
    metrics.record(tenant_id=TENANT, metric_type="sale", amount_inr=350, menu_item_id=item.menu_item_id, quantity=1)
    metrics.record(tenant_id=TENANT, metric_type="sale", amount_inr=350, menu_item_id=item.menu_item_id, quantity=1)

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    dish = report.dishes[0]
    assert dish.recipe_cost_inr == 100.0  # 200g * ₹0.5/g
    assert round(dish.food_cost_pct) == 29  # 100/350
    assert dish.quantity_sold == 2
    assert dish.revenue_inr == 700
    assert dish.profit_inr == 500.0  # 700 - 100*2
    assert dish.profit_margin_inr == 250.0  # per-unit: 350 price - 100 cost


def test_a_recipe_with_one_unpriceable_ingredient_makes_the_whole_dish_unknown(tmp_path):
    """A partially-priced recipe cost would be a silently WRONG total,
    not an honest estimate — the whole thing must report unknown."""
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    item = menu.create_item(tenant_id=TENANT, name="Butter Chicken", price_inr=350)
    menu.set_recipe(TENANT, item.menu_item_id, [
        {"ingredient_name": "Chicken", "quantity": 200, "unit": "g"},
        {"ingredient_name": "Butter", "quantity": 20, "unit": "g"},
    ])
    purchases.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1000, unit="g", amount_inr=500)
    # No purchase history for Butter at all.

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    assert report.dishes[0].recipe_cost_inr is None


def test_recipe_cost_uses_converted_total_for_mixed_compatible_units(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    item = menu.create_item(tenant_id=TENANT, name="Butter Chicken", price_inr=350)
    menu.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "Chicken", "quantity": 200, "unit": "g"}])
    # One purchase in kg, one in g — same real ingredient, different package sizes.
    purchases.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1, unit="kg", amount_inr=500)  # 1000g for ₹500
    purchases.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1000, unit="g", amount_inr=500)  # another 1000g for ₹500
    # Combined: 2000g for ₹1000 -> ₹0.5/g, same as the single-unit case above.

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    assert report.dishes[0].recipe_cost_inr == 100.0  # 200g * ₹0.5/g


def test_recipe_cost_is_unknown_when_purchase_units_are_incompatible(tmp_path):
    """Mixing "g" and "pieces" for the same ingredient can't be averaged
    honestly — the dish must report unknown cost, not a meaningless
    number, matching the "never invent a number" discipline."""
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    item = menu.create_item(tenant_id=TENANT, name="Butter Chicken", price_inr=350)
    menu.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "Chicken", "quantity": 200, "unit": "g"}])
    purchases.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1000, unit="g", amount_inr=500)
    purchases.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=4, unit="pieces", amount_inr=400)

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    assert report.dishes[0].recipe_cost_inr is None


def test_menu_engineering_classifies_all_four_quadrants(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    purchases.record(tenant_id=TENANT, ingredient_name="X", quantity=1000, unit="g", amount_inr=1000)  # ₹1/g

    def _dish(name, price, recipe_g, qty_sold):
        item = menu.create_item(tenant_id=TENANT, name=name, price_inr=price)
        menu.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "X", "quantity": recipe_g, "unit": "g"}])
        for _ in range(qty_sold):
            metrics.record(tenant_id=TENANT, metric_type="sale", amount_inr=price, menu_item_id=item.menu_item_id, quantity=1)
        return item

    # Cost is recipe_g * ₹1. Popular threshold ~ avg qty; profit threshold ~ avg profit.
    _dish("Star Dish", price=200, recipe_g=50, qty_sold=20)       # high qty, high profit (150/unit)
    _dish("Plowhorse Dish", price=110, recipe_g=100, qty_sold=20)  # high qty, low profit (10/unit)
    _dish("Puzzle Dish", price=200, recipe_g=50, qty_sold=2)       # low qty, high profit (150/unit)
    _dish("Dog Dish", price=110, recipe_g=100, qty_sold=2)         # low qty, low profit (10/unit)

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    by_name = {d.name: d for d in report.dishes}
    assert by_name["Star Dish"].classification == "star"
    assert by_name["Plowhorse Dish"].classification == "plowhorse"
    assert by_name["Puzzle Dish"].classification == "puzzle"
    assert by_name["Dog Dish"].classification == "dog"


def test_unsold_dish_is_not_classified(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    item = menu.create_item(tenant_id=TENANT, name="Untried Special", price_inr=500)
    menu.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "X", "quantity": 10, "unit": "g"}])
    purchases.record(tenant_id=TENANT, ingredient_name="X", quantity=100, unit="g", amount_inr=100)

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    assert report.dishes[0].quantity_sold == 0
    assert report.dishes[0].classification is None


def test_inactive_menu_items_are_excluded(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    item = menu.create_item(tenant_id=TENANT, name="Discontinued", price_inr=100)
    menu.update_item(TENANT, item.menu_item_id, active=False)

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    assert report.dishes == []


def test_demand_forecast_baseline_is_a_simple_trailing_average(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    item = menu.create_item(tenant_id=TENANT, name="Lassi", price_inr=80)
    for _ in range(30):
        metrics.record(tenant_id=TENANT, metric_type="sale", amount_inr=80, menu_item_id=item.menu_item_id, quantity=1)

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics, window_days=30)
    dish = report.dishes[0]
    assert dish.avg_daily_quantity == 1.0  # 30 sold / 30 days
    assert dish.projected_next_7_days_quantity == 7.0


def test_report_is_empty_for_a_tenant_with_no_menu(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    assert report.dishes == []
    assert report.avg_profit_inr is None


# ------------------------------------------------------------------ reorder suggestions


def test_reorder_suggestion_fires_after_min_trigger_count(tmp_path):
    inv = InventoryStore(tmp_path / "inventory.db")
    runs = AutomationRunStore(tmp_path / "runs.db")
    inv.adjust_quantity(TENANT, "Paneer", delta=50, unit="g")
    inv.set_par_level(TENANT, "Paneer", par_level=1000, unit="g")
    for _ in range(2):
        runs.record(
            tenant_id=TENANT, rule_id="r1", trigger_type="low_stock", target_type="inventory_item",
            target_id="inventory:paneer", action_type="notify_owner", status=RunStatus.SUCCESS,
        )

    report = build_reorder_suggestions(TENANT, inventory_store=inv, automation_run_store=runs)
    assert len(report.suggestions) == 1
    s = report.suggestions[0]
    assert s.ingredient_name == "Paneer"
    assert s.low_stock_trigger_count == 2
    assert s.suggested_par_level == 1250.0  # +25%


def test_reorder_suggestion_does_not_fire_below_min_trigger_count(tmp_path):
    inv = InventoryStore(tmp_path / "inventory.db")
    runs = AutomationRunStore(tmp_path / "runs.db")
    inv.adjust_quantity(TENANT, "Paneer", delta=50, unit="g")
    inv.set_par_level(TENANT, "Paneer", par_level=1000, unit="g")
    runs.record(
        tenant_id=TENANT, rule_id="r1", trigger_type="low_stock", target_type="inventory_item",
        target_id="inventory:paneer", action_type="notify_owner", status=RunStatus.SUCCESS,
    )
    report = build_reorder_suggestions(TENANT, inventory_store=inv, automation_run_store=runs)
    assert report.suggestions == []


def test_reorder_suggestion_counts_failed_runs_too(tmp_path):
    """A failed WhatsApp send doesn't mean the underlying stock problem
    wasn't real — every recorded run counts, not just successes."""
    inv = InventoryStore(tmp_path / "inventory.db")
    runs = AutomationRunStore(tmp_path / "runs.db")
    inv.adjust_quantity(TENANT, "Paneer", delta=50, unit="g")
    inv.set_par_level(TENANT, "Paneer", par_level=1000, unit="g")
    runs.record(
        tenant_id=TENANT, rule_id="r1", trigger_type="low_stock", target_type="inventory_item",
        target_id="inventory:paneer", action_type="notify_owner", status=RunStatus.FAILED, error="no whatsapp",
    )
    runs.record(
        tenant_id=TENANT, rule_id="r1", trigger_type="low_stock", target_type="inventory_item",
        target_id="inventory:paneer", action_type="notify_owner", status=RunStatus.SUCCESS,
    )
    report = build_reorder_suggestions(TENANT, inventory_store=inv, automation_run_store=runs)
    assert len(report.suggestions) == 1
    assert report.suggestions[0].low_stock_trigger_count == 2


def test_reorder_suggestion_ignores_unrelated_trigger_types(tmp_path):
    inv = InventoryStore(tmp_path / "inventory.db")
    runs = AutomationRunStore(tmp_path / "runs.db")
    inv.adjust_quantity(TENANT, "Paneer", delta=50, unit="g")
    inv.set_par_level(TENANT, "Paneer", par_level=1000, unit="g")
    for _ in range(3):
        runs.record(
            tenant_id=TENANT, rule_id="r1", trigger_type="task_overdue", target_type="task",
            target_id="task_1", action_type="notify_owner", status=RunStatus.SUCCESS,
        )
    report = build_reorder_suggestions(TENANT, inventory_store=inv, automation_run_store=runs)
    assert report.suggestions == []


def test_reorder_suggestions_sorted_by_trigger_count_descending(tmp_path):
    inv = InventoryStore(tmp_path / "inventory.db")
    runs = AutomationRunStore(tmp_path / "runs.db")
    inv.adjust_quantity(TENANT, "Paneer", delta=50, unit="g")
    inv.set_par_level(TENANT, "Paneer", par_level=1000, unit="g")
    inv.adjust_quantity(TENANT, "Chicken", delta=50, unit="g")
    inv.set_par_level(TENANT, "Chicken", par_level=1000, unit="g")
    for _ in range(2):
        runs.record(
            tenant_id=TENANT, rule_id="r1", trigger_type="low_stock", target_type="inventory_item",
            target_id="inventory:paneer", action_type="notify_owner", status=RunStatus.SUCCESS,
        )
    for _ in range(5):
        runs.record(
            tenant_id=TENANT, rule_id="r1", trigger_type="low_stock", target_type="inventory_item",
            target_id="inventory:chicken", action_type="notify_owner", status=RunStatus.SUCCESS,
        )
    report = build_reorder_suggestions(TENANT, inventory_store=inv, automation_run_store=runs)
    assert [s.ingredient_name for s in report.suggestions] == ["Chicken", "Paneer"]


# ------------------------------------------------------------------ menu recommendations (Phase 24 — Restaurant Autopilot)


def _classified_report(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    purchases.record(tenant_id=TENANT, ingredient_name="X", quantity=1000, unit="g", amount_inr=1000)  # ₹1/g

    def _dish(name, price, recipe_g, qty_sold):
        item = menu.create_item(tenant_id=TENANT, name=name, price_inr=price)
        menu.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "X", "quantity": recipe_g, "unit": "g"}])
        for _ in range(qty_sold):
            metrics.record(tenant_id=TENANT, metric_type="sale", amount_inr=price, menu_item_id=item.menu_item_id, quantity=1)
        return item

    _dish("Star Dish", price=200, recipe_g=50, qty_sold=20)
    _dish("Plowhorse Dish", price=110, recipe_g=100, qty_sold=20)
    _dish("Puzzle Dish", price=200, recipe_g=50, qty_sold=2)
    _dish("Dog Dish", price=110, recipe_g=100, qty_sold=2)
    return menu, purchases, metrics, build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)


def test_recommendations_flag_dog_for_rework_or_removal(tmp_path):
    _, _, _, report = _classified_report(tmp_path)
    recs = {r.name: r for r in build_menu_recommendations(report)}
    assert "reworking the recipe" in recs["Dog Dish"].recommendation or "removing" in recs["Dog Dish"].recommendation
    assert recs["Dog Dish"].suggested_price_inr is None


def test_recommendations_suggest_a_bounded_price_nudge_for_plowhorse(tmp_path):
    _, _, _, report = _classified_report(tmp_path)
    recs = {r.name: r for r in build_menu_recommendations(report)}
    plowhorse = recs["Plowhorse Dish"]
    assert plowhorse.suggested_price_inr == round(110 * 1.05)  # +5%, PLOWHORSE_PRICE_INCREASE_PCT
    assert "price increase" in plowhorse.recommendation


def test_recommendations_suggest_featuring_puzzle_more_prominently(tmp_path):
    _, _, _, report = _classified_report(tmp_path)
    recs = {r.name: r for r in build_menu_recommendations(report)}
    assert "featuring" in recs["Puzzle Dish"].recommendation
    assert recs["Puzzle Dish"].suggested_price_inr is None


def test_recommendations_have_nothing_to_say_about_a_healthy_star(tmp_path):
    _, _, _, report = _classified_report(tmp_path)
    names = {r.name for r in build_menu_recommendations(report)}
    assert "Star Dish" not in names


def test_recommendations_flag_high_food_cost_pct_regardless_of_classification(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    purchases.record(tenant_id=TENANT, ingredient_name="X", quantity=1000, unit="g", amount_inr=1000)  # ₹1/g
    item = menu.create_item(tenant_id=TENANT, name="Overpriced Recipe", price_inr=100)
    menu.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "X", "quantity": 40, "unit": "g"}])  # 40% food cost
    metrics.record(tenant_id=TENANT, metric_type="sale", amount_inr=100, menu_item_id=item.menu_item_id, quantity=1)

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    recs = build_menu_recommendations(report)
    assert any("caution line" in r.recommendation for r in recs if r.name == "Overpriced Recipe")


def test_no_recommendation_for_an_unsold_or_unpriceable_dish(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    metrics = BusinessMetricStore(tmp_path / "metrics.db")
    menu.create_item(tenant_id=TENANT, name="Untried Special", price_inr=500)

    report = build_menu_engineering_report(TENANT, menu_store=menu, purchase_store=purchases, metric_store=metrics)
    assert build_menu_recommendations(report) == []


# ------------------------------------------------------------------ price simulation / Business Twin (Phase 24)


def test_simulate_price_recomputes_food_cost_pct_and_margin(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    item = menu.create_item(tenant_id=TENANT, name="Butter Chicken", price_inr=350)
    menu.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "Chicken", "quantity": 200, "unit": "g"}])
    purchases.record(tenant_id=TENANT, ingredient_name="Chicken", quantity=1000, unit="g", amount_inr=500)  # ₹0.5/g -> ₹100 cost

    result = simulate_menu_item_price(TENANT, item.menu_item_id, 400, menu_store=menu, purchase_store=purchases)
    assert result.current_price_inr == 350
    assert result.hypothetical_price_inr == 400
    assert result.recipe_cost_inr == 100.0
    assert round(result.current_food_cost_pct) == 29  # 100/350
    assert round(result.hypothetical_food_cost_pct) == 25  # 100/400
    assert result.current_profit_margin_inr == 250.0
    assert result.hypothetical_profit_margin_inr == 300.0


def test_simulate_price_reports_unknown_cost_when_recipe_unpriceable(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    item = menu.create_item(tenant_id=TENANT, name="Lassi", price_inr=80)
    # No recipe at all.

    result = simulate_menu_item_price(TENANT, item.menu_item_id, 100, menu_store=menu, purchase_store=purchases)
    assert result.recipe_cost_inr is None
    assert result.current_food_cost_pct is None
    assert result.hypothetical_food_cost_pct is None


def test_simulate_price_returns_none_for_a_missing_menu_item(tmp_path):
    menu = MenuStore(tmp_path / "menu.db")
    purchases = PurchaseStore(tmp_path / "purchases.db")
    assert simulate_menu_item_price(TENANT, "does-not-exist", 100, menu_store=menu, purchase_store=purchases) is None
