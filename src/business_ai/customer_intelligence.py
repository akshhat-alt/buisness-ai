"""Repeat-customer intelligence (Phase 23 — Restaurant Operations
Intelligence): pure derived computation over LeadStore data that already
exists — no new store, no new logging mechanism, matching scorecard.py/
revenue_radar.py/menu_engineering.py's own shape.

A phone number is the customer identity key, not session_id — a WhatsApp
session_id is stable per phone forever (see leads.py's own comment), but
a reservation taken by staff (Phase 23's "reserve" command) creates its
own synthetic session_id, so phone is the one field that reliably
identifies the SAME real customer across both organic WhatsApp leads and
staff-logged reservations. Leads with no phone are excluded — there's no
honest way to group them.

Same "never invent a number" discipline as every other derived-
intelligence module: a customer's "visits" only ever counts a CONFIRMED
appointment_outcome of "completed" — never a booking that was merely
made, cancelled, or a no-show, and revenue only ever counts an owner-
confirmed deposit payment, never an estimate.
"""

from __future__ import annotations

from pydantic import BaseModel


class RepeatCustomer(BaseModel):
    phone: str
    name: str | None = None
    visit_count: int  # count of DISTINCT leads with appointment_outcome == "completed"
    confirmed_revenue_inr: int  # sum of deposit_paid_amount_inr across all of this phone's leads
    first_seen_at: str
    last_visit_at: str | None = None  # most recent appointment_outcome_at among their completed visits


class RepeatCustomerReport(BaseModel):
    repeat_customers: list[RepeatCustomer]  # 2+ completed visits, sorted by visit_count descending
    total_customers_with_a_completed_visit: int


REPEAT_CUSTOMER_MIN_VISITS = 2


def build_repeat_customer_report(
    tenant_id: str, *, lead_store, min_visits: int = REPEAT_CUSTOMER_MIN_VISITS,
) -> RepeatCustomerReport:
    leads = lead_store.list_for_tenant(tenant_id, limit=100000)
    by_phone: dict[str, list] = {}
    for lead in leads:
        if not lead.phone:
            continue
        by_phone.setdefault(lead.phone, []).append(lead)

    customers_with_a_visit = 0
    repeat_customers: list[RepeatCustomer] = []
    for phone, phone_leads in by_phone.items():
        completed = [l for l in phone_leads if l.appointment_outcome == "completed"]
        if not completed:
            continue
        customers_with_a_visit += 1
        if len(completed) < min_visits:
            continue
        name = next((l.name for l in phone_leads if l.name), None)
        confirmed_revenue = sum(l.deposit_paid_amount_inr or 0 for l in phone_leads)
        last_visit_at = max((l.appointment_outcome_at for l in completed if l.appointment_outcome_at), default=None)
        first_seen_at = min(l.created_at for l in phone_leads)
        repeat_customers.append(RepeatCustomer(
            phone=phone, name=name, visit_count=len(completed), confirmed_revenue_inr=confirmed_revenue,
            first_seen_at=first_seen_at, last_visit_at=last_visit_at,
        ))

    repeat_customers.sort(key=lambda c: c.visit_count, reverse=True)
    return RepeatCustomerReport(
        repeat_customers=repeat_customers, total_customers_with_a_completed_visit=customers_with_a_visit,
    )


def render_repeat_customers_whatsapp(report: RepeatCustomerReport) -> str:
    if not report.repeat_customers:
        return "No repeat customers yet — a customer needs 2+ visits confirmed as completed."
    lines = [f"🔁 {len(report.repeat_customers)} repeat customer(s):"]
    for c in report.repeat_customers[:10]:
        revenue_note = f", ₹{c.confirmed_revenue_inr} confirmed" if c.confirmed_revenue_inr else ""
        lines.append(f"• {c.name or c.phone}: {c.visit_count} visits{revenue_note}")
    return "\n".join(lines)
