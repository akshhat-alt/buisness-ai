"""Tests for Phase 17's WhatsApp admin-bot restaurant commands: dish
sales (with recipe-based stock depletion), purchases (receiving stock),
wastage (depleting stock, honest cost estimation), and the inventory/
low-stock view command.
"""

from __future__ import annotations

from tests.test_admin_bot import OWNER_WA, RAVI_WA, _add_employee, _send
from tests.test_whatsapp import _activate_with_whatsapp, _signup, client_wa, services_wa

__all__ = ["client_wa", "services_wa"]


def test_log_dish_sale_auto_prices_and_depletes_recipe_stock(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    services_wa.menu_store.create_item(tenant_id=tenant_id, name="Butter Chicken", price_inr=350)
    item = services_wa.menu_store.find_by_name(tenant_id, "Butter Chicken")
    services_wa.menu_store.set_recipe(tenant_id, item.menu_item_id, [
        {"ingredient_name": "Chicken", "quantity": 200, "unit": "g"},
        {"ingredient_name": "Butter", "quantity": 20, "unit": "g"},
    ])
    services_wa.inventory_store.adjust_quantity(tenant_id, "Chicken", delta=1000, unit="g")
    services_wa.inventory_store.adjust_quantity(tenant_id, "Butter", delta=100, unit="g")

    _send(client_wa, wa_id=OWNER_WA, text="log sale butter chicken x2", message_id="wamid.dish1")

    metrics = services_wa.metric_store.list_for_tenant(tenant_id)
    assert len(metrics) == 1
    assert metrics[0].amount_inr == 700  # 350 * 2
    assert metrics[0].menu_item_id == item.menu_item_id
    assert metrics[0].quantity == 2

    chicken = services_wa.inventory_store.get(tenant_id, "Chicken")
    butter = services_wa.inventory_store.get(tenant_id, "Butter")
    assert chicken.quantity_on_hand == 1000 - 400  # 200g x2
    assert butter.quantity_on_hand == 100 - 40  # 20g x2

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "700" in reply
    assert "Stock updated" in reply


def test_log_dish_sale_defaults_to_quantity_one(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    services_wa.menu_store.create_item(tenant_id=tenant_id, name="Lassi", price_inr=80)

    _send(client_wa, wa_id=OWNER_WA, text="log sale lassi", message_id="wamid.dish2")

    metrics = services_wa.metric_store.list_for_tenant(tenant_id)
    assert metrics[0].amount_inr == 80
    assert metrics[0].quantity == 1


def test_log_dish_sale_unknown_dish_gives_a_helpful_reply(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    _send(client_wa, wa_id=OWNER_WA, text="log sale nonexistent dish", message_id="wamid.dish3")

    assert services_wa.metric_store.list_for_tenant(tenant_id) == []
    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Couldn't find" in reply


def test_log_dish_sale_handles_a_digit_leading_menu_item_name(client_wa, services_wa):
    """Regression test for a real bug caught during Phase 17's own live
    verification: a menu item name that starts with a digit (e.g. a
    real "7 Up" or "2 Piece Chicken") must never be silently misread by
    the generic numeric "log sale <amount> [note]" grammar."""
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    services_wa.menu_store.create_item(tenant_id=tenant_id, name="7 Up", price_inr=40)

    _send(client_wa, wa_id=OWNER_WA, text="log sale 7 up x2", message_id="wamid.digit1")

    metrics = services_wa.metric_store.list_for_tenant(tenant_id)
    assert len(metrics) == 1
    assert metrics[0].amount_inr == 80  # 40 * 2, NOT misread as ₹7
    assert metrics[0].quantity == 2


def test_numeric_log_sale_still_falls_back_correctly_when_no_menu_item_matches(client_wa, services_wa):
    """The same "7 up"-shaped text must still fall through correctly to
    the generic numeric grammar when NO menu item exists at all —
    confirms the fallback path, not just the dish-match path."""
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    _send(client_wa, wa_id=OWNER_WA, text="log sale 7 up", message_id="wamid.digit2")

    metrics = services_wa.metric_store.list_for_tenant(tenant_id)
    assert len(metrics) == 1
    assert metrics[0].amount_inr == 7  # no menu item named "7 up" exists -> generic amount=7, note="up"
    assert metrics[0].menu_item_id is None


def test_numeric_log_sale_still_works_unchanged(client_wa, services_wa):
    """Phase 12's original "log sale <amount> [note]" grammar must keep
    working byte-for-byte after Phase 17's dish-sale regex is added."""
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    _send(client_wa, wa_id=OWNER_WA, text="log sale 1500 haircut", message_id="wamid.numeric1")

    metrics = services_wa.metric_store.list_for_tenant(tenant_id)
    assert len(metrics) == 1
    assert metrics[0].amount_inr == 1500
    assert metrics[0].menu_item_id is None


def test_log_purchase_receives_stock_and_records_supplier(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    services_wa.supplier_store.create(tenant_id=tenant_id, name="Ramesh")

    _send(client_wa, wa_id=OWNER_WA, text="log purchase 10 kg chicken ₹4200 from Ramesh", message_id="wamid.pur1")

    purchases = services_wa.purchase_store.list_for_tenant(tenant_id)
    assert len(purchases) == 1
    assert purchases[0].quantity == 10
    assert purchases[0].amount_inr == 4200
    assert purchases[0].supplier_id is not None

    stock = services_wa.inventory_store.get(tenant_id, "chicken")
    assert stock.quantity_on_hand == 10


def test_log_purchase_rejects_mismatched_unit_and_records_nothing(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    services_wa.inventory_store.adjust_quantity(tenant_id, "Chicken", delta=5, unit="kg")

    _send(client_wa, wa_id=OWNER_WA, text="log purchase 200 g chicken ₹100", message_id="wamid.mismatch1")

    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "kg" in reply
    # Neither the purchase nor the inventory change should have applied.
    assert services_wa.purchase_store.list_for_tenant(tenant_id) == []
    assert services_wa.inventory_store.get(tenant_id, "Chicken").quantity_on_hand == 5


def test_log_waste_rejects_mismatched_unit_and_records_nothing(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    services_wa.inventory_store.adjust_quantity(tenant_id, "Paneer", delta=5, unit="kg")

    _send(client_wa, wa_id=OWNER_WA, text="log waste 200 g paneer: spoiled", message_id="wamid.mismatch2")

    assert services_wa.wastage_store.list_for_tenant(tenant_id) == []
    assert services_wa.inventory_store.get(tenant_id, "Paneer").quantity_on_hand == 5


def test_log_purchase_without_known_supplier_still_succeeds(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    _send(client_wa, wa_id=OWNER_WA, text="log purchase 5 kg rice ₹500", message_id="wamid.pur2")

    purchases = services_wa.purchase_store.list_for_tenant(tenant_id)
    assert len(purchases) == 1
    assert purchases[0].supplier_id is None


def test_log_waste_depletes_stock_and_estimates_cost_from_purchase_history(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    services_wa.purchase_store.record(tenant_id=tenant_id, ingredient_name="Paneer", quantity=10, unit="kg", amount_inr=3000)
    services_wa.inventory_store.adjust_quantity(tenant_id, "Paneer", delta=10, unit="kg")

    _send(client_wa, wa_id=OWNER_WA, text="log waste 2 kg paneer: spoiled", message_id="wamid.waste1")

    entries = services_wa.wastage_store.list_for_tenant(tenant_id)
    assert len(entries) == 1
    assert entries[0].reason == "spoiled"
    assert entries[0].estimated_cost_inr == 600  # (3000/10) * 2

    stock = services_wa.inventory_store.get(tenant_id, "Paneer")
    assert stock.quantity_on_hand == 8


def test_log_waste_without_purchase_history_costs_zero_honestly(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    _send(client_wa, wa_id=OWNER_WA, text="log waste 1 kg mystery ingredient", message_id="wamid.waste2")

    entries = services_wa.wastage_store.list_for_tenant(tenant_id)
    assert entries[0].estimated_cost_inr == 0
    assert entries[0].reason == "other"


def test_restaurant_log_commands_open_to_any_roster_member(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    _add_employee(client_wa, headers, tenant_id, whatsapp_number=RAVI_WA, name="Ravi")

    _send(client_wa, wa_id=RAVI_WA, text="log purchase 1 kg salt ₹20", message_id="wamid.staff1")

    assert len(services_wa.purchase_store.list_for_tenant(tenant_id)) == 1


def test_inventory_command_is_owner_manager_gated(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)
    _add_employee(client_wa, headers, tenant_id, whatsapp_number=RAVI_WA, name="Ravi")
    services_wa.inventory_store.set_par_level(tenant_id, "Rice", par_level=10, unit="kg")
    services_wa.inventory_store.adjust_quantity(tenant_id, "Rice", delta=2, unit="kg")

    _send(client_wa, wa_id=RAVI_WA, text="inventory", message_id="wamid.inv1")
    staff_reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == RAVI_WA][-1]["body"]
    assert "Only an owner or manager" in staff_reply

    _send(client_wa, wa_id=OWNER_WA, text="stock", message_id="wamid.inv2")
    owner_reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Rice" in owner_reply


def test_inventory_command_all_clear_when_nothing_low(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    _send(client_wa, wa_id=OWNER_WA, text="inventory", message_id="wamid.inv3")
    reply = [m for m in services_wa.fake_whatsapp_client.sent if m["to"] == OWNER_WA][-1]["body"]
    assert "Nothing below par level" in reply


# ------------------------------------------------------------------ Phase 19: event-driven LOW_STOCK firing


def test_dish_sale_depletion_that_crosses_par_level_fires_low_stock_immediately(client_wa, services_wa):
    """No cron tick needed: a real WhatsApp sale that depletes an
    ingredient below its par level should fire a LOW_STOCK automation
    rule right away, using the exact same _fire_automation_rule/dedup
    path the cron endpoint uses."""
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    services_wa.menu_store.create_item(tenant_id=tenant_id, name="Butter Chicken", price_inr=350)
    item = services_wa.menu_store.find_by_name(tenant_id, "Butter Chicken")
    services_wa.menu_store.set_recipe(tenant_id, item.menu_item_id, [
        {"ingredient_name": "Chicken", "quantity": 200, "unit": "g"},
    ])
    services_wa.inventory_store.adjust_quantity(tenant_id, "Chicken", delta=500, unit="g")
    services_wa.inventory_store.set_par_level(tenant_id, "Chicken", par_level=400, unit="g")
    services_wa.automation_rule_store.create(
        tenant_id=tenant_id, name="Chase low stock", trigger_type="low_stock", trigger_params={},
        action_type="notify_owner", action_params={},
    )

    _send(client_wa, wa_id=OWNER_WA, text="log sale butter chicken", message_id="wamid.evt1")

    # 500g - 200g = 300g, below the 400g par level -> should have fired
    # in the very same request, with no cron tick in between.
    alert = [m for m in services_wa.fake_whatsapp_client.sent if "low on stock" in m["body"]]
    assert len(alert) == 1
    assert "Chicken" in alert[0]["body"]
    runs = services_wa.automation_run_store.list_for_tenant(tenant_id)
    assert any(r.trigger_type == "low_stock" and r.status == "success" for r in runs)


def test_wastage_that_crosses_par_level_fires_low_stock_immediately(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    services_wa.inventory_store.adjust_quantity(tenant_id, "Paneer", delta=500, unit="g")
    services_wa.inventory_store.set_par_level(tenant_id, "Paneer", par_level=400, unit="g")
    services_wa.automation_rule_store.create(
        tenant_id=tenant_id, name="Chase low stock", trigger_type="low_stock", trigger_params={},
        action_type="notify_owner", action_params={},
    )

    _send(client_wa, wa_id=OWNER_WA, text="log waste 200 g paneer: spoiled", message_id="wamid.evt2")

    alert = [m for m in services_wa.fake_whatsapp_client.sent if "low on stock" in m["body"]]
    assert len(alert) == 1
    assert "paneer" in alert[0]["body"].lower()


def test_dish_sale_depletion_that_stays_above_par_level_does_not_fire(client_wa, services_wa):
    headers, tenant_id = _signup(client_wa)
    _activate_with_whatsapp(client_wa, headers, tenant_id, services_wa.settings.admin_secret, phone_number_id="PNID_1")
    services_wa.employee_store.ensure_owner_bootstrap(tenant_id, OWNER_WA)

    services_wa.menu_store.create_item(tenant_id=tenant_id, name="Lassi", price_inr=80)
    item = services_wa.menu_store.find_by_name(tenant_id, "Lassi")
    services_wa.menu_store.set_recipe(tenant_id, item.menu_item_id, [
        {"ingredient_name": "Yogurt", "quantity": 100, "unit": "g"},
    ])
    services_wa.inventory_store.adjust_quantity(tenant_id, "Yogurt", delta=5000, unit="g")
    services_wa.inventory_store.set_par_level(tenant_id, "Yogurt", par_level=100, unit="g")
    services_wa.automation_rule_store.create(
        tenant_id=tenant_id, name="Chase low stock", trigger_type="low_stock", trigger_params={},
        action_type="notify_owner", action_params={},
    )

    _send(client_wa, wa_id=OWNER_WA, text="log sale lassi", message_id="wamid.evt3")

    assert not any("low on stock" in m["body"] for m in services_wa.fake_whatsapp_client.sent)
