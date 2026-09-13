"""Tests for Phase 24's Digital GM briefing — pure aggregation over
Phase 22/23's existing reports, no new store. On-demand only; there is
no cron/scheduler behavior to test here.
"""

from __future__ import annotations

import time

from business_ai.automation import AutomationRunStore, RunStatus
from business_ai.digital_gm import build_digital_gm_briefing
from business_ai.inventory import InventoryStore
from business_ai.leads import LeadStore
from business_ai.menu import MenuStore
from business_ai.metrics import BusinessMetricStore
from business_ai.purchases import PurchaseStore
from business_ai.shifts import ShiftStore

TENANT = "trattoria-a"


def _stores(tmp_path):
    return dict(
        menu_store=MenuStore(tmp_path / "menu.db"),
        purchase_store=PurchaseStore(tmp_path / "purchases.db"),
        metric_store=BusinessMetricStore(tmp_path / "metrics.db"),
        inventory_store=InventoryStore(tmp_path / "inventory.db"),
        automation_run_store=AutomationRunStore(tmp_path / "runs.db"),
        lead_store=LeadStore(tmp_path / "leads.db"),
        shift_store=ShiftStore(tmp_path / "shifts.db"),
    )


def test_briefing_is_all_clear_when_nothing_is_going_on(tmp_path):
    lines = build_digital_gm_briefing(TENANT, **_stores(tmp_path))
    assert lines == ["✅ All clear — nothing urgent right now."]


def test_briefing_surfaces_an_upcoming_reservation(tmp_path):
    stores = _stores(tmp_path)
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    lead = stores["lead_store"].create(tenant_id=TENANT, session_id="s1", phone="9876543210", name="Jane")
    stores["lead_store"].set_appointment(TENANT, lead.lead_id, future, party_size=2)

    lines = build_digital_gm_briefing(TENANT, **stores)
    assert any("reservation" in line for line in lines)


def test_briefing_surfaces_todays_shifts(tmp_path):
    stores = _stores(tmp_path)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    stores["shift_store"].create(tenant_id=TENANT, employee_id="emp1", shift_date=today, start_time="09:00", end_time="17:00")

    lines = build_digital_gm_briefing(TENANT, **stores)
    assert any("shift" in line for line in lines)


def test_briefing_surfaces_a_reorder_suggestion(tmp_path):
    stores = _stores(tmp_path)
    stores["inventory_store"].adjust_quantity(TENANT, "Paneer", delta=50, unit="g")
    stores["inventory_store"].set_par_level(TENANT, "Paneer", par_level=1000, unit="g")
    for _ in range(2):
        stores["automation_run_store"].record(
            tenant_id=TENANT, rule_id="r1", trigger_type="low_stock", target_type="inventory_item",
            target_id="inventory:paneer", action_type="notify_owner", status=RunStatus.SUCCESS,
        )

    lines = build_digital_gm_briefing(TENANT, **stores)
    assert any("reorder suggestion" in line and "Paneer" in line for line in lines)


def test_briefing_surfaces_repeat_customers(tmp_path):
    stores = _stores(tmp_path)
    for i in range(2):
        lead = stores["lead_store"].create(tenant_id=TENANT, session_id=f"s{i}", phone="9876543210", name="John")
        stores["lead_store"].record_appointment_outcome(TENANT, lead.lead_id, "completed")

    lines = build_digital_gm_briefing(TENANT, **stores)
    assert any("repeat customer" in line for line in lines)


def test_briefing_surfaces_a_menu_recommendation(tmp_path):
    stores = _stores(tmp_path)
    menu, purchases, metrics = stores["menu_store"], stores["purchase_store"], stores["metric_store"]
    purchases.record(tenant_id=TENANT, ingredient_name="X", quantity=1000, unit="g", amount_inr=1000)  # ₹1/g

    def _dish(name, price, recipe_g, qty_sold):
        item = menu.create_item(tenant_id=TENANT, name=name, price_inr=price)
        menu.set_recipe(TENANT, item.menu_item_id, [{"ingredient_name": "X", "quantity": recipe_g, "unit": "g"}])
        for _ in range(qty_sold):
            metrics.record(tenant_id=TENANT, metric_type="sale", amount_inr=price, menu_item_id=item.menu_item_id, quantity=1)

    _dish("Star Dish", price=200, recipe_g=50, qty_sold=20)  # high qty, high margin
    _dish("Dog Dish", price=110, recipe_g=100, qty_sold=2)  # low qty, low margin

    lines = build_digital_gm_briefing(TENANT, **stores)
    assert any("menu recommendation" in line for line in lines)
