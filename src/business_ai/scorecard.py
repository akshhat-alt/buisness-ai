"""Weekly Business Scorecard (Phase 13): pure data-assembly + render over
data every earlier phase already collects (tasks, feedback, financials,
dependency risk) — no new store, same "reads, never writes" shape as
digest.py, just a week-over-week lens instead of a rolling-window one.
"""

from __future__ import annotations

from html import escape

from pydantic import BaseModel

from business_ai.tenant import TenantConfig


class WeekWindow(BaseModel):
    tasks_completed: int
    dissatisfaction_rate_pct: float
    total_questions: int
    sales_inr: int
    expenses_inr: int
    collections_inr: int
    high_severity_risk_count: int


class ScorecardData(BaseModel):
    this_week: WeekWindow
    last_week: WeekWindow
    top_feedback_theme: str | None = None
    top_feedback_theme_count: int = 0


def _week_window(
    tenant_id: str, *, since_iso: str, until_iso: str | None, task_store, analytics_store, metric_store,
) -> WeekWindow:
    tasks_completed = len(
        [t for t in task_store.list_for_tenant(tenant_id, status="done") if since_iso <= t.updated_at < (until_iso or "9999")]
    )
    analytics = analytics_store.summary_for_tenant(tenant_id, since_iso=since_iso, until_iso=until_iso)
    rate = round(100 * analytics.dissatisfaction_count / analytics.total_questions, 1) if analytics.total_questions else 0.0
    summary_by_type = {s.metric_type: s for s in metric_store.summary_for_tenant(tenant_id, since_iso=since_iso, until_iso=until_iso)}
    return WeekWindow(
        tasks_completed=tasks_completed,
        dissatisfaction_rate_pct=rate,
        total_questions=analytics.total_questions,
        sales_inr=summary_by_type.get("sale").total_inr if "sale" in summary_by_type else 0,
        expenses_inr=summary_by_type.get("expense").total_inr if "expense" in summary_by_type else 0,
        collections_inr=summary_by_type.get("collection").total_inr if "collection" in summary_by_type else 0,
        high_severity_risk_count=0,  # filled in by the caller, which has dependency snapshot access
    )


def build_weekly_scorecard_data(
    tenant_id: str, *, one_week_ago_iso: str, two_weeks_ago_iso: str,
    task_store, analytics_store, metric_store, feedback_store, high_severity_risk_count: int,
) -> ScorecardData:
    # No upper bound on "this week" (until_iso=None): a row created in
    # the same second this function runs must still count — an
    # exclusive `created_at < now` bound would silently drop it.
    this_week = _week_window(
        tenant_id, since_iso=one_week_ago_iso, until_iso=None,
        task_store=task_store, analytics_store=analytics_store, metric_store=metric_store,
    )
    this_week = this_week.model_copy(update={"high_severity_risk_count": high_severity_risk_count})
    last_week = _week_window(
        tenant_id, since_iso=two_weeks_ago_iso, until_iso=one_week_ago_iso,
        task_store=task_store, analytics_store=analytics_store, metric_store=metric_store,
    )
    themes = feedback_store.summarize_by_theme(tenant_id, since_iso=one_week_ago_iso)
    top_theme = themes[0] if themes else None
    return ScorecardData(
        this_week=this_week, last_week=last_week,
        top_feedback_theme=top_theme.theme if top_theme else None,
        top_feedback_theme_count=top_theme.count if top_theme else 0,
    )


def _delta_arrow(this_value: float, last_value: float, *, higher_is_better: bool) -> str:
    if this_value == last_value:
        return "→"
    improved = (this_value > last_value) if higher_is_better else (this_value < last_value)
    return "▲" if improved else "▼"


def has_scorecard_content(data: ScorecardData) -> bool:
    w = data.this_week
    return w.total_questions > 0 or w.tasks_completed > 0 or w.sales_inr > 0 or w.expenses_inr > 0 or w.collections_inr > 0


def render_weekly_scorecard_whatsapp(tenant: TenantConfig, data: ScorecardData) -> str:
    w, p = data.this_week, data.last_week
    lines = [f"📊 {tenant.business_name} — weekly scorecard"]
    lines.append(f"Tasks completed: {w.tasks_completed} {_delta_arrow(w.tasks_completed, p.tasks_completed, higher_is_better=True)} (prior week: {p.tasks_completed})")
    lines.append(f"Customer questions: {w.total_questions}, dissatisfaction rate: {w.dissatisfaction_rate_pct}% {_delta_arrow(w.dissatisfaction_rate_pct, p.dissatisfaction_rate_pct, higher_is_better=False)}")
    if w.sales_inr or p.sales_inr:
        lines.append(f"Sales (manual entries): ₹{w.sales_inr} {_delta_arrow(w.sales_inr, p.sales_inr, higher_is_better=True)} (prior week: ₹{p.sales_inr})")
    if w.expenses_inr or p.expenses_inr:
        lines.append(f"Expenses (manual entries): ₹{w.expenses_inr}")
    if w.collections_inr or p.collections_inr:
        lines.append(f"Collections (manual entries): ₹{w.collections_inr}")
    if data.top_feedback_theme:
        lines.append(f"Top recurring team feedback: {data.top_feedback_theme.replace('_', ' ')} ({data.top_feedback_theme_count}×)")
    if w.high_severity_risk_count:
        lines.append(f"⚠️ {w.high_severity_risk_count} single-point-of-failure risk(s) — check your Business Map.")
    return "\n".join(lines)


def render_weekly_scorecard_email(tenant: TenantConfig, data: ScorecardData) -> tuple[str, str]:
    w, p = data.this_week, data.last_week
    subject = f"{tenant.business_name}: weekly scorecard — {w.tasks_completed} tasks completed, {w.total_questions} questions"
    rows = [
        ("Tasks completed", w.tasks_completed, p.tasks_completed),
        ("Customer questions", w.total_questions, p.total_questions),
        ("Dissatisfaction rate", f"{w.dissatisfaction_rate_pct}%", f"{p.dissatisfaction_rate_pct}%"),
        ("Sales (manual)", f"₹{w.sales_inr}", f"₹{p.sales_inr}"),
        ("Expenses (manual)", f"₹{w.expenses_inr}", f"₹{p.expenses_inr}"),
        ("Collections (manual)", f"₹{w.collections_inr}", f"₹{p.collections_inr}"),
    ]
    table_rows = "".join(
        f"<tr><td style='padding:6px 12px;'>{escape(str(label))}</td>"
        f"<td style='padding:6px 12px; text-align:right;'>{escape(str(this_v))}</td>"
        f"<td style='padding:6px 12px; text-align:right; color:#888;'>{escape(str(last_v))}</td></tr>"
        for label, this_v, last_v in rows
    )
    risk_note = (
        f"<p style='color:#B00;'>⚠️ {w.high_severity_risk_count} single-point-of-failure risk(s) — check your Business Map.</p>"
        if w.high_severity_risk_count else ""
    )
    theme_note = (
        f"<p>Top recurring team feedback: <b>{escape(data.top_feedback_theme.replace('_', ' '))}</b> ({data.top_feedback_theme_count}×)</p>"
        if data.top_feedback_theme else ""
    )
    html = f"""
    <div style="font-family:sans-serif; max-width:520px;">
      <h2>{escape(tenant.business_name)} — Weekly Scorecard</h2>
      <table style="border-collapse:collapse; width:100%;">
        <tr><th style='text-align:left; padding:6px 12px;'>Metric</th><th style='text-align:right; padding:6px 12px;'>This week</th><th style='text-align:right; padding:6px 12px;'>Prior week</th></tr>
        {table_rows}
      </table>
      {theme_note}
      {risk_note}
      <p style="color:#888; font-size:.85em;">Sales/expenses/collections are manual entries logged via WhatsApp — not a live sync.</p>
    </div>
    """
    return subject, html
