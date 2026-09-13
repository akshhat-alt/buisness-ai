"""Digital GM (Phase 24 — Restaurant Autopilot): a synthesized daily
briefing pulling the single most important line from each of Phase
22/23's existing reports into one on-demand view.

Deliberately ON-DEMAND PULL ONLY — this is not a new scheduled cron job.
Every other periodic job in this codebase is its own external-cron-invoked
endpoint (digest, reminders, ops alerts); duplicating that infrastructure
just to re-push the same underlying data on a timer would be exactly the
"unnecessary duplicate architecture" this project has consistently
avoided. An owner asks for this briefing ("gm report") the same way they
already ask for "today" or "food cost."

Pure aggregation — no new store, no new logging mechanism, same
stores-as-keyword-args shape as menu_engineering.py/scorecard.py.
"""

from __future__ import annotations

import time

from business_ai.customer_intelligence import build_repeat_customer_report
from business_ai.menu_engineering import build_menu_engineering_report, build_menu_recommendations, build_reorder_suggestions


def build_digital_gm_briefing(
    tenant_id: str, *, menu_store, purchase_store, metric_store, inventory_store,
    automation_run_store, lead_store, shift_store,
) -> list[str]:
    lines: list[str] = []

    upcoming = lead_store.list_upcoming_appointments(tenant_id, within_hours=24)
    if upcoming:
        lines.append(f"📅 {len(upcoming)} reservation(s) in the next 24h.")

    today = time.strftime("%Y-%m-%d", time.gmtime())
    shifts_today = shift_store.list_for_tenant(tenant_id, shift_date=today)
    if shifts_today:
        lines.append(f"👥 {len(shifts_today)} shift(s) scheduled today.")

    menu_report = build_menu_engineering_report(tenant_id, menu_store=menu_store, purchase_store=purchase_store, metric_store=metric_store)
    recommendations = build_menu_recommendations(menu_report)
    if recommendations:
        top = recommendations[0]
        lines.append(f"🍽️ {len(recommendations)} menu recommendation(s) — top: {top.name}: {top.recommendation}")

    reorder = build_reorder_suggestions(tenant_id, inventory_store=inventory_store, automation_run_store=automation_run_store)
    if reorder.suggestions:
        top_reorder = reorder.suggestions[0]
        lines.append(
            f"📦 {len(reorder.suggestions)} reorder suggestion(s) — top: {top_reorder.ingredient_name} "
            f"(triggered {top_reorder.low_stock_trigger_count}x)."
        )

    repeat_report = build_repeat_customer_report(tenant_id, lead_store=lead_store)
    if repeat_report.repeat_customers:
        lines.append(f"🔁 {len(repeat_report.repeat_customers)} repeat customer(s) tracked.")

    if not lines:
        lines.append("✅ All clear — nothing urgent right now.")
    return lines


def render_digital_gm_whatsapp(lines: list[str]) -> str:
    return "🧑‍💼 Digital GM briefing:\n" + "\n".join(lines)
