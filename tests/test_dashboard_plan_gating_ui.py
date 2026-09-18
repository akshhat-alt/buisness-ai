"""Regression guard for the Overview "Command Center" / "Business Health"
perpetual-Loading bug: a Starter-plan tenant's dashboard used to leave
"Approvals Waiting on You" and "Needs Your Immediate Attention" stuck on
"Loading…" forever, because loadCommandCenter() bailed out on the expected
403 from a Growth/Scale-gated endpoint without ever touching those elements.

static/dashboard.html has no JS test runner, so this asserts on the source
text directly — the same lightweight style used elsewhere for this file
(there is no other dashboard test in this suite). It exists purely to
catch a regression back to the old "fetch, then silently return on !res.ok"
pattern for these two sections.
"""

from __future__ import annotations

from business_ai.constants import STATIC_DIR


def _dashboard_source() -> str:
    return (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")


def test_load_command_center_renders_upgrade_state_for_starter_plan():
    src = _dashboard_source()
    fn_start = src.index("async function loadCommandCenter()")
    fn_body = src[fn_start : fn_start + 600]
    assert "currentPlan === 'starter'" in fn_body
    assert "renderCommandCenterUpgradeState()" in fn_body


def test_command_center_upgrade_state_clears_the_loading_placeholders():
    src = _dashboard_source()
    fn_start = src.index("function renderCommandCenterUpgradeState()")
    fn_body = src[fn_start : fn_start + 1200]
    assert "renderPlanGatedEmptyState(" in fn_body
    assert "cc-approvals-list" in fn_body
    assert "cc-attention-card" in fn_body


def test_load_command_center_handles_403_even_without_upfront_plan_check():
    src = _dashboard_source()
    fn_start = src.index("async function loadCommandCenter()")
    fn_body = src[fn_start : fn_start + 900]
    assert "res.status === 403" in fn_body
    assert "renderCommandCenterUpgradeState()" in fn_body


def test_load_health_renders_upgrade_state_for_starter_plan_instead_of_dashes():
    src = _dashboard_source()
    fn_start = src.index("async function loadHealth()")
    fn_body = src[fn_start : fn_start + 900]
    assert "currentPlan === 'starter'" in fn_body
    assert "renderPlanGatedEmptyState(" in fn_body
    assert "health-body" in fn_body


def test_load_health_handles_403_from_business_health_endpoint():
    src = _dashboard_source()
    fn_start = src.index("async function loadHealth()")
    fn_body = src[fn_start : fn_start + 1400]
    assert "res.status === 403" in fn_body
