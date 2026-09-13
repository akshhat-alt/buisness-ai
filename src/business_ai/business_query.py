"""Ask Your Business Anything (Phase 26): a natural-language query
answerer over structured data this app already computes — NEVER a new
computation. Reuses generation.py's classify_employee_message() (its
report_request intent + EMPLOYEE_REPORT_TYPES taxonomy) — the SAME
classifier and the SAME taxonomy admin_bot.py's WhatsApp NL fallback
already uses, so a question means the same thing on both channels.

Every report_type here maps to an ALREADY-EXISTING builder+render pair
from Phase 12-25's own modules; this file adds no new store, no new
metric, no new number. A handful of report types (today/tasks/overdue/
sales/inventory/shifts_today) have no exported pure render_*_whatsapp()
function to reuse (their WhatsApp rendering is inlined directly in
admin_bot.py's command handlers) — those honestly point the dashboard
caller at the existing section that already shows this live, rather
than duplicating ~10 lines of inline formatting logic in a second place
that could drift out of sync with the WhatsApp version.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from business_ai.customer_intelligence import build_repeat_customer_report, render_repeat_customers_whatsapp
from business_ai.digital_gm import build_digital_gm_briefing, render_digital_gm_whatsapp
from business_ai.menu_engineering import (
    build_menu_engineering_report,
    build_menu_recommendations,
    build_reorder_suggestions,
    render_menu_engineering_whatsapp,
    render_menu_recommendations_whatsapp,
    render_reorder_suggestions_whatsapp,
)
from business_ai.revenue_radar import compute_revenue_leakage, render_revenue_radar_whatsapp
from business_ai.reviews import render_reviews_whatsapp
from business_ai.supplier_intelligence import build_supplier_intelligence_report, render_supplier_intelligence_whatsapp
from business_ai.tenant import TenantAction


def _answer_food_cost(tenant_id: str, svc) -> str:
    report = build_menu_engineering_report(
        tenant_id, menu_store=svc.menu_store, purchase_store=svc.purchase_store, metric_store=svc.metric_store,
    )
    return render_menu_engineering_whatsapp(report)


def _answer_reorder(tenant_id: str, svc) -> str:
    report = build_reorder_suggestions(tenant_id, inventory_store=svc.inventory_store, automation_run_store=svc.automation_run_store)
    return render_reorder_suggestions_whatsapp(report)


def _answer_menu_recommendations(tenant_id: str, svc) -> str:
    report = build_menu_engineering_report(
        tenant_id, menu_store=svc.menu_store, purchase_store=svc.purchase_store, metric_store=svc.metric_store,
    )
    return render_menu_recommendations_whatsapp(build_menu_recommendations(report))


def _answer_gm_report(tenant_id: str, svc) -> str:
    lines = build_digital_gm_briefing(
        tenant_id, menu_store=svc.menu_store, purchase_store=svc.purchase_store, metric_store=svc.metric_store,
        inventory_store=svc.inventory_store, automation_run_store=svc.automation_run_store,
        lead_store=svc.lead_store, shift_store=svc.shift_store,
    )
    return render_digital_gm_whatsapp(lines)


def _answer_revenue_leakage(tenant_id: str, svc) -> str:
    tenant = svc.tenant_registry.get_config(tenant_id)
    report = compute_revenue_leakage(
        tenant_id, lead_store=svc.lead_store, analytics_store=svc.analytics_store, deposit_amount_inr=tenant.deposit_amount_inr,
    )
    return render_revenue_radar_whatsapp(report)


def _answer_repeat_customers(tenant_id: str, svc) -> str:
    return render_repeat_customers_whatsapp(build_repeat_customer_report(tenant_id, lead_store=svc.lead_store))


def _answer_supplier_spend(tenant_id: str, svc) -> str:
    report = build_supplier_intelligence_report(tenant_id, supplier_store=svc.supplier_store, purchase_store=svc.purchase_store)
    return render_supplier_intelligence_whatsapp(report)


def _answer_reviews(tenant_id: str, svc) -> str:
    return render_reviews_whatsapp(svc.review_store.latest_by_platform(tenant_id))


@dataclass(frozen=True)
class ReportTypeSpec:
    action: TenantAction
    answer: Callable[[str, object], str]


# report_type (generation.EMPLOYEE_REPORT_TYPES) -> how to answer it here.
# Gate matches the equivalent GET route's own authorize() call exactly —
# never a looser dashboard-only permission for the same data.
REPORT_TYPE_SPECS: dict[str, ReportTypeSpec] = {
    "food_cost": ReportTypeSpec(TenantAction.VIEW_INVENTORY, _answer_food_cost),
    "reorder": ReportTypeSpec(TenantAction.VIEW_INVENTORY, _answer_reorder),
    "menu_recommendations": ReportTypeSpec(TenantAction.VIEW_INVENTORY, _answer_menu_recommendations),
    "supplier_spend": ReportTypeSpec(TenantAction.VIEW_INVENTORY, _answer_supplier_spend),
    "gm_report": ReportTypeSpec(TenantAction.VIEW_INVENTORY, _answer_gm_report),
    "revenue_leakage": ReportTypeSpec(TenantAction.VIEW_FEEDBACK, _answer_revenue_leakage),
    "repeat_customers": ReportTypeSpec(TenantAction.VIEW_LEADS, _answer_repeat_customers),
    "reviews": ReportTypeSpec(TenantAction.VIEW_FINANCIALS, _answer_reviews),
}

# Report types this app can answer on WhatsApp (see admin_bot.py's
# _REPORT_TYPE_TO_COMMAND) but that have no standalone render_*_whatsapp()
# function to reuse here — their WhatsApp rendering is inlined directly
# in a command handler. Rather than duplicate that formatting logic in a
# second place that could silently drift out of sync, this dashboard
# path honestly points the caller at the section that already shows it.
NO_STANDALONE_RENDERER_HINTS: dict[str, str] = {
    "today": "See the Team & Tasks section for today's open work by owner.",
    "tasks": "See the Team & Tasks section for the full task list.",
    "overdue": "See the Team & Tasks section for overdue tasks.",
    "sales": "See the Business Intelligence section's Financials card for manual sales/expense/collection totals.",
    "inventory": "See the Restaurant Intelligence section for ingredients below par level.",
    "shifts_today": "See the Team & Tasks section's Shifts card for today's schedule.",
    "reservations": "See the Restaurant Intelligence section's Reservations card for upcoming bookings.",
    "feedback_themes": "See the Business Intelligence section for recurring feedback themes.",
    "scorecard": "See the Business Intelligence section's Weekly Scorecard card for this week vs. last week.",
}
