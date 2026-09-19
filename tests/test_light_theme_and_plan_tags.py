"""Guards two presentation fixes: no near-black panels on auth pages or the
dashboard Staff Assistant (owner asked for light colours), and honest plan
tags on the marketing Operations tiles (Radar/food-cost/reviews/shifts are
Growth-gated in tenant.py, so the page must not present them as universal).
"""

from __future__ import annotations

from business_ai.constants import STATIC_DIR
from business_ai.tenant import GROWTH_ACTIONS, STARTER_ACTIONS, TenantAction

DARK_HEX = ("#0F1613", "#12201B", "#182119")


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_no_near_black_panels_remain():
    for name in ("login.html", "forgot-password.html", "reset-password.html", "dashboard.html"):
        src = _read(name)
        for hex_ in DARK_HEX:
            assert hex_ not in src, f"{hex_} still in {name}"


def test_operations_tiles_carry_plan_tags():
    src = _read("index.html")
    ops = src[src.index('id="operations"') : src.index('id="assistants"')]
    assert ops.count("bm-plan-tag") == 5
    assert "Tasks: Basic" in ops and "Shifts: Pro" in ops


def test_tags_match_real_gating():
    # shifts + financials are Growth-only; tasks are in Starter
    assert TenantAction.MANAGE_SHIFTS not in STARTER_ACTIONS and TenantAction.MANAGE_SHIFTS in GROWTH_ACTIONS
    assert TenantAction.VIEW_FINANCIALS not in STARTER_ACTIONS and TenantAction.VIEW_FINANCIALS in GROWTH_ACTIONS
    assert TenantAction.ASSIGN_TASK in STARTER_ACTIONS
