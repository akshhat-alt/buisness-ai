"""Regression guard for the dashboard sidebar declutter pass: no duplicate
"Command Center" nav entry (it is a subsection of Overview, not its own
page), "Restaurant Intel" hidden from non-restaurant tenants until they
have menu items, and "Business Map" / "Self-Evolution" tucked behind a
collapsed "Advanced" group instead of sitting in the main list.

static/dashboard.html has no JS test runner, so this asserts on the
source text directly — same lightweight style as
test_dashboard_plan_gating_ui.py, the only other dashboard test here.
"""

from __future__ import annotations

from business_ai.constants import STATIC_DIR


def _dashboard_source() -> str:
    return (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")


def test_command_center_is_not_a_separate_sidebar_link():
    src = _dashboard_source()
    assert '<a class="dash-nav-link" href="#command-center"' not in src
    assert '<a href="#command-center">' not in src
    # the section itself still exists, folded into Overview
    assert 'id="command-center"' in src


def test_restaurant_intel_nav_hidden_by_default_and_revealed_by_js():
    src = _dashboard_source()
    assert 'class="dash-nav-link dash-restaurant-nav" href="#restaurant-intel" style="display:none;"' in src
    fn_start = src.index("async function applyRestaurantNavVisibility()")
    fn_body = src[fn_start : fn_start + 700]
    assert "/api/menu/items" in fn_body
    assert "dash-restaurant-nav" in fn_body


def test_business_map_and_evolution_are_in_a_collapsed_advanced_group():
    src = _dashboard_source()
    group_start = src.index('id="dash-advanced-group"')
    group_body = src[group_start : group_start + 900]
    assert 'href="#business-map"' in group_body
    assert 'href="#evolution"' in group_body
    assert "dash-nav-advanced-group" in src
    assert "dash-nav-advanced-group.open" in src


def test_advanced_toggle_persists_and_auto_expands_for_direct_links():
    src = _dashboard_source()
    fn_start = src.index("setupAdvancedNavGroup")
    fn_body = src[fn_start : fn_start + 900]
    assert "ba_advanced_nav_open" in fn_body
    assert "#business-map" in fn_body and "#evolution" in fn_body
