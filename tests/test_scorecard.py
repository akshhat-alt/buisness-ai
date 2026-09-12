"""Tests for the Weekly Business Scorecard's pure data-assembly/render
logic (Phase 13) — real store instances, no mocks.
"""

from __future__ import annotations

import time

import pytest

from business_ai.analytics import AnalyticsStore
from business_ai.feedback import FeedbackStore
from business_ai.metrics import BusinessMetricStore
from business_ai.scorecard import build_weekly_scorecard_data, has_scorecard_content, render_weekly_scorecard_whatsapp
from business_ai.tasks import TaskStore
from business_ai.tenant import TenantConfig

TENANT = "salon-a"


@pytest.fixture()
def stores(tmp_path):
    return {
        "task_store": TaskStore(tmp_path / "tasks.db"),
        "analytics_store": AnalyticsStore(tmp_path / "analytics.db"),
        "metric_store": BusinessMetricStore(tmp_path / "metrics.db"),
        "feedback_store": FeedbackStore(tmp_path / "feedback.db"),
    }


def _windows():
    now = time.time()
    return (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7 * 86400)),
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 14 * 86400)),
    )


def _build(stores, **kwargs):
    _now_iso, one_week_ago_iso, two_weeks_ago_iso = _windows()
    return build_weekly_scorecard_data(
        TENANT, one_week_ago_iso=one_week_ago_iso, two_weeks_ago_iso=two_weeks_ago_iso,
        task_store=stores["task_store"], analytics_store=stores["analytics_store"],
        metric_store=stores["metric_store"], feedback_store=stores["feedback_store"],
        high_severity_risk_count=kwargs.pop("high_severity_risk_count", 0),
    )


def test_empty_tenant_has_no_scorecard_content(stores):
    data = _build(stores)
    assert not has_scorecard_content(data)
    assert data.this_week.tasks_completed == 0


def test_financial_totals_reflected_in_this_week(stores):
    stores["metric_store"].record(tenant_id=TENANT, metric_type="sale", amount_inr=5000)
    stores["metric_store"].record(tenant_id=TENANT, metric_type="expense", amount_inr=1000)
    data = _build(stores)
    assert has_scorecard_content(data)
    assert data.this_week.sales_inr == 5000
    assert data.this_week.expenses_inr == 1000
    assert data.last_week.sales_inr == 0


def test_dissatisfaction_rate_computed_for_this_week(stores):
    for i in range(4):
        stores["analytics_store"].log_turn(
            tenant_id=TENANT, session_id=f"s{i}", query=f"q{i}", answer_status="answered", shows_dissatisfaction=(i == 0),
        )
    data = _build(stores)
    assert data.this_week.total_questions == 4
    assert data.this_week.dissatisfaction_rate_pct == 25.0


def test_top_feedback_theme_surfaced(stores):
    stores["feedback_store"].record(
        tenant_id=TENANT, employee_id="emp1", raw_text="billing keeps logging out", sentiment="negative",
        theme="software_or_tools", urgency="medium", root_cause_hint="x", suggested_action="y",
    )
    stores["feedback_store"].record(
        tenant_id=TENANT, employee_id="emp1", raw_text="billing keeps logging out again", sentiment="negative",
        theme="software_or_tools", urgency="medium", root_cause_hint="x", suggested_action="y",
    )
    data = _build(stores)
    assert data.top_feedback_theme == "software_or_tools"
    assert data.top_feedback_theme_count == 2


def test_high_severity_risk_count_passed_through(stores):
    data = _build(stores, high_severity_risk_count=3)
    assert data.this_week.high_severity_risk_count == 3


def test_render_whatsapp_includes_key_figures(stores):
    stores["metric_store"].record(tenant_id=TENANT, metric_type="sale", amount_inr=2500)
    data = _build(stores)
    tenant = TenantConfig(tenant_id=TENANT, business_name="Priya Salon", owner_email="p@example.com")
    text = render_weekly_scorecard_whatsapp(tenant, data)
    assert "Priya Salon" in text
    assert "2500" in text
