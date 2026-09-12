"""Revenue Leakage Radar (Phase 15): a single, consolidated, owner-facing
view of concrete missed-revenue situations already representable in
existing data — no new store, no inference, no LLM. Every existing
automation trigger (missed-lead re-engagement, deposit reminders) ACTS
on one condition at a time; this is the complementary READ-ONLY report
that answers "where is money actually being left on the table right
now," reusing the exact same fields those automations already key off.

Deliberately NOT wired into a new proactive WhatsApp push: Business AI
already has four owner-facing proactive channels (daily digest, weekly
scorecard, dependency-risk scan, evolution-monitor rollback alerts) —
a fifth unconditional push risks the alert fatigue that would make an
owner start ignoring all of them. This is a pull report (WhatsApp
command + API route), not another push.
"""

from __future__ import annotations

import time

from pydantic import BaseModel

from business_ai.leads import Lead, lead_stage


class RevenueLeakageReport(BaseModel):
    missed_buying_intent_leads: list[dict]
    unpaid_deposits: list[dict]
    no_show_appointments: list[dict]
    total_estimated_leakage_inr: int
    window_days: int


def _lead_summary(lead: Lead) -> dict:
    return {
        "lead_id": lead.lead_id, "name": lead.name, "phone": lead.phone, "email": lead.email,
        "stage": lead_stage(lead), "created_at": lead.created_at,
    }


def compute_revenue_leakage(
    tenant_id: str, *, lead_store, analytics_store, deposit_amount_inr: int | None = None,
    window_days: int = 14, deposit_grace_hours: int = 48,
) -> RevenueLeakageReport:
    now = time.time()
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - window_days * 86400))
    deposit_cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - deposit_grace_hours * 3600))

    leads = lead_store.list_for_tenant(tenant_id, since_iso=since_iso, limit=5000)
    buying_intent_sessions = analytics_store.session_ids_with_buying_intent(tenant_id, since_iso=since_iso)

    missed_buying_intent = [
        lead for lead in leads
        if lead.session_id in buying_intent_sessions and not lead.appointment_at and not lead.deposit_paid_at
    ]
    unpaid_deposits = [
        lead for lead in leads
        if lead.deposit_link_sent_at and not lead.deposit_paid_at and lead.deposit_link_sent_at < deposit_cutoff_iso
    ]
    no_shows = [lead for lead in leads if lead.appointment_outcome == "no_show"]

    # The only concrete number honestly computable without a live payment
    # webhook: the tenant's own configured deposit amount times how many
    # deposit links have gone unpaid past the grace window. Never
    # presented as exact lost revenue — see the WhatsApp/dashboard render
    # for the "estimated" framing.
    total_estimated = (deposit_amount_inr or 0) * len(unpaid_deposits)

    return RevenueLeakageReport(
        missed_buying_intent_leads=[_lead_summary(l) for l in missed_buying_intent],
        unpaid_deposits=[_lead_summary(l) for l in unpaid_deposits],
        no_show_appointments=[_lead_summary(l) for l in no_shows],
        total_estimated_leakage_inr=total_estimated,
        window_days=window_days,
    )


def has_leakage(report: RevenueLeakageReport) -> bool:
    return bool(report.missed_buying_intent_leads or report.unpaid_deposits or report.no_show_appointments)


def render_revenue_radar_whatsapp(report: RevenueLeakageReport) -> str:
    if not has_leakage(report):
        return f"💰 Revenue Radar (last {report.window_days} days): nothing leaking right now — nice."
    lines = [f"💰 Revenue Radar (last {report.window_days} days):"]
    if report.missed_buying_intent_leads:
        lines.append(f"• {len(report.missed_buying_intent_leads)} lead(s) showed buying interest but never booked/paid.")
    if report.unpaid_deposits:
        lines.append(f"• {len(report.unpaid_deposits)} deposit link(s) sent and still unpaid.")
    if report.no_show_appointments:
        lines.append(f"• {len(report.no_show_appointments)} appointment(s) marked no-show.")
    if report.total_estimated_leakage_inr:
        lines.append(f"Estimated unpaid deposits: ~₹{report.total_estimated_leakage_inr} (based on your configured deposit amount).")
    return "\n".join(lines)
