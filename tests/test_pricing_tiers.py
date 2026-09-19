"""Regression guard for the 3-tier pricing rename (Basic/Pro/Proest at
₹2,000/₹5,000/₹20,000), replacing the old single "One plan" marketing
copy. Basic maps to the existing self-serve platform price (unchanged
plumbing); Pro/Proest are admin-assisted upgrades via the existing
plan-upgrade-request flow — no new billing infrastructure.

Static HTML/JS has no test runner here, so this asserts on source text
directly, same lightweight style as the other dashboard/marketing
content tests in this suite.
"""

from __future__ import annotations

from business_ai.constants import STATIC_DIR


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_marketing_pricing_section_has_three_tiers_with_correct_prices():
    src = _read("index.html")
    pricing_start = src.index('id="pricing"')
    pricing_body = src[pricing_start : pricing_start + 6000]
    assert "Basic" in pricing_body and "₹2,000" in pricing_body
    assert "Pro" in pricing_body and "₹5,000" in pricing_body
    assert "Proest" in pricing_body and "₹20,000" in pricing_body


def test_marketing_pricing_no_longer_claims_a_single_flat_plan():
    src = _read("index.html")
    assert "One plan. Everything included." not in src


def test_dashboard_plan_display_map_matches_marketing_prices():
    src = _read("dashboard.html")
    map_start = src.index("const PLAN_DISPLAY")
    map_body = src[map_start : map_start + 400]
    assert "starter" in map_body and "'Basic'" in map_body and "2000" in map_body
    assert "growth" in map_body and "'Pro'" in map_body and "5000" in map_body
    assert "scale" in map_body and "'Proest'" in map_body and "20000" in map_body


def test_dashboard_upgrade_card_shows_plan_price_not_bare_plan_name():
    src = _read("dashboard.html")
    fn_start = src.index("function renderPlanGatedEmptyState(")
    fn_body = src[fn_start : fn_start + 900]
    assert "planPrice" in fn_body
    assert "PLAN_DISPLAY[requiredPlan]" in fn_body
