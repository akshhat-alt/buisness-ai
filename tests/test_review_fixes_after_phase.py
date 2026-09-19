"""Guards three defects found in review of the launch-fixes phase:
(1) the restaurant nav guard read a non-existent tenant.vertical and was
scale-only, so Restaurant Costs & Menu could never appear; (2) an unpaid
tenant's checklist offered "Activate Now", which can only fail — it must
show the price and route to payment; (3) marketing showed an invented
"clocked in" feature."""

from __future__ import annotations

from business_ai.constants import STATIC_DIR


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_restaurant_nav_guard_uses_real_signals_only():
    src = _read("dashboard.html")
    fn = src[src.index("async function applyRestaurantNavVisibility()") :][:900]
    assert "currentTenant.vertical" not in fn
    assert "currentPlan !== 'scale'" not in fn
    assert "currentPlan === 'starter'" in fn


def test_unpaid_checklist_item_shows_price_and_links_to_payment():
    src = _read("dashboard.html")
    assert "to go live" in src and 'href="/onboarding"' in src
    assert "platformPriceInr" in src


def test_no_invented_clock_in_feature_on_marketing():
    assert "clocked in" not in _read("index.html")
