"""Guards the guided-automation UX: ready-made one-click recipes and plain
language in both the dashboard and the onboarding wizard. Every recipe
must use a real TriggerType/ActionType (a typo would only fail at click
time in a browser, which no other test exercises), and the recipe that
messages a customer must only use placeholders the engine actually
supplies for its trigger.
"""

from __future__ import annotations

import re

from business_ai.automation import ActionType, TriggerType
from business_ai.constants import STATIC_DIR


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _recipe_block(src: str, const_name: str) -> str:
    start = src.index(f"const {const_name}")
    return src[start : src.index("];", start)]


def test_dashboard_recipes_only_use_real_trigger_and_action_types():
    block = _recipe_block(_read("dashboard.html"), "AUTOMATION_RECIPES")
    triggers = set(re.findall(r"trigger_type: '([a-z_]+)'", block))
    actions = set(re.findall(r"action_type: '([a-z_]+)'", block))
    assert triggers and actions
    assert triggers <= {t.value for t in TriggerType}
    assert actions <= {a.value for a in ActionType}


def test_wizard_recipes_only_use_real_types_and_never_message_a_customer():
    block = _recipe_block(_read("onboarding.html"), "WIZ_RECIPES")
    triggers = set(re.findall(r"trigger_type: '([a-z_]+)'", block))
    actions = set(re.findall(r"action_type: '([a-z_]+)'", block))
    assert triggers <= {t.value for t in TriggerType}
    assert actions <= {a.value for a in ActionType}
    assert "message_lead" not in actions


def test_customer_messaging_recipe_only_uses_supplied_placeholder():
    block = _recipe_block(_read("dashboard.html"), "AUTOMATION_RECIPES")
    message = re.search(r"message: '([^']+)'", block).group(1)
    # deposit trigger's metadata is {name, appointment_at} (admin_bot.py)
    assert set(re.findall(r"\{(\w+)\}", message)) <= {"name", "appointment_at"}


def test_dashboard_automation_form_uses_plain_language():
    src = _read("dashboard.html")
    assert "par level" not in src.split('id="automation"')[1].split('id="evolution"')[0]
    assert "When this happens" in src and "Then Bizistic should" in src
    assert "renderAutomationRecipes();" in src


def test_every_recipe_payload_is_accepted_by_the_real_api(client, owner_session):
    headers, tenant_id = owner_session
    recipes = [
        ("task_overdue", {"hours": 24}, "notify_owner", {}, 48),
        ("negative_feedback_unresolved", {"hours": 12}, "notify_owner", {}, 24),
        ("recurring_feedback_theme", {"min_count": 3}, "create_task", {}, None),
        ("deposit_unpaid_after_appointment", {"hours": 24}, "message_lead",
         {"message": "Hi {name}, gentle reminder about your deposit."}, None),
        ("low_stock", {}, "notify_owner", {}, 24),
    ]
    for i, (trig, tparams, act, aparams, esc) in enumerate(recipes):
        r = client.post(
            f"/api/automation/rules?tenant_id={tenant_id}", headers=headers,
            json={"name": f"recipe {i}", "trigger_type": trig, "trigger_params": tparams,
                  "action_type": act, "action_params": aparams, "escalate_after_hours": esc},
        )
        assert r.status_code == 200, (trig, r.text)
