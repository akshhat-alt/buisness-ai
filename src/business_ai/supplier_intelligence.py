"""Supplier intelligence (Phase 23 — Restaurant Operations Intelligence):
pure derived computation over SupplierStore + PurchaseStore data that
already exists — no new store, matching scorecard.py/revenue_radar.py/
menu_engineering.py/customer_intelligence.py's own shape.

Deliberately narrow scope: total spend, purchase count, and distinct
ingredients bought, per supplier. This module does NOT attempt price-
trend-per-ingredient or delivery-reliability scoring — this app has no
delivery-date/promised-date field on a Purchase record at all, so
"on-time delivery" isn't a real, derivable number yet; inventing one
would violate this codebase's own "never invent a number" discipline.
Purchases with no supplier_id (an unmatched/never-set supplier name,
see admin_bot.py's own "from <supplier>" handling) are grouped
separately as "unattributed" rather than silently dropped, since that
total is itself a useful signal that supplier-matching is failing.
"""

from __future__ import annotations

from pydantic import BaseModel


class SupplierSpend(BaseModel):
    supplier_id: str
    name: str
    total_spend_inr: int
    purchase_count: int
    distinct_ingredients: int
    last_purchase_at: str | None = None


class SupplierIntelligenceReport(BaseModel):
    suppliers: list[SupplierSpend]  # sorted by total_spend_inr descending
    unattributed_spend_inr: int  # purchases with no matched supplier_id
    unattributed_purchase_count: int


def build_supplier_intelligence_report(tenant_id: str, *, supplier_store, purchase_store) -> SupplierIntelligenceReport:
    suppliers_by_id = {s.supplier_id: s for s in supplier_store.list_for_tenant(tenant_id, active_only=False)}
    purchases = purchase_store.list_for_tenant(tenant_id, limit=100000)

    by_supplier: dict[str, list] = {}
    unattributed_spend = 0
    unattributed_count = 0
    for purchase in purchases:
        if not purchase.supplier_id or purchase.supplier_id not in suppliers_by_id:
            unattributed_spend += purchase.amount_inr
            unattributed_count += 1
            continue
        by_supplier.setdefault(purchase.supplier_id, []).append(purchase)

    results: list[SupplierSpend] = []
    for supplier_id, supplier_purchases in by_supplier.items():
        supplier = suppliers_by_id[supplier_id]
        results.append(SupplierSpend(
            supplier_id=supplier_id, name=supplier.name,
            total_spend_inr=sum(p.amount_inr for p in supplier_purchases),
            purchase_count=len(supplier_purchases),
            distinct_ingredients=len({p.ingredient_name.strip().lower() for p in supplier_purchases}),
            last_purchase_at=max(p.created_at for p in supplier_purchases),
        ))
    results.sort(key=lambda s: s.total_spend_inr, reverse=True)
    return SupplierIntelligenceReport(
        suppliers=results, unattributed_spend_inr=unattributed_spend, unattributed_purchase_count=unattributed_count,
    )


def render_supplier_intelligence_whatsapp(report: SupplierIntelligenceReport) -> str:
    if not report.suppliers and not report.unattributed_purchase_count:
        return "No purchases logged yet."
    lines = ["🚚 Supplier spend:"]
    for s in report.suppliers[:10]:
        lines.append(f"• {s.name}: ₹{s.total_spend_inr} across {s.purchase_count} purchase(s), {s.distinct_ingredients} ingredient(s)")
    if report.unattributed_purchase_count:
        lines.append(
            f"⚠️ ₹{report.unattributed_spend_inr} across {report.unattributed_purchase_count} purchase(s) "
            f"had no matching supplier on file — add them under Suppliers so they're counted here."
        )
    return "\n".join(lines)
