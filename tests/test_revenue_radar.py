"""Tests for the Revenue Leakage Radar's pure computation (Phase 15) —
real LeadStore/AnalyticsStore instances, no mocks.
"""

from __future__ import annotations

import pytest

from business_ai.analytics import AnalyticsStore
from business_ai.leads import LeadStore
from business_ai.revenue_radar import compute_revenue_leakage, has_leakage, render_revenue_radar_whatsapp

TENANT = "salon-a"


@pytest.fixture()
def stores(tmp_path):
    return {"lead_store": LeadStore(tmp_path / "leads.db"), "analytics_store": AnalyticsStore(tmp_path / "analytics.db")}


def test_empty_tenant_has_no_leakage(stores):
    report = compute_revenue_leakage(TENANT, **stores)
    assert not has_leakage(report)
    assert report.total_estimated_leakage_inr == 0


def test_missed_buying_intent_lead_detected(stores):
    lead = stores["lead_store"].create(tenant_id=TENANT, session_id="s1", phone="9876543210")
    stores["analytics_store"].log_turn(
        tenant_id=TENANT, session_id="s1", query="how much for a haircut", answer_status="answered", shows_buying_intent=True,
    )
    report = compute_revenue_leakage(TENANT, **stores)
    assert has_leakage(report)
    assert len(report.missed_buying_intent_leads) == 1
    assert report.missed_buying_intent_leads[0]["lead_id"] == lead.lead_id


def test_lead_with_appointment_is_not_flagged_as_missed(stores):
    lead = stores["lead_store"].create(tenant_id=TENANT, session_id="s1", phone="9876543210")
    stores["lead_store"].set_appointment(TENANT, lead.lead_id, "2027-01-01T10:00:00Z")
    stores["analytics_store"].log_turn(
        tenant_id=TENANT, session_id="s1", query="how much", answer_status="answered", shows_buying_intent=True,
    )
    report = compute_revenue_leakage(TENANT, **stores)
    assert report.missed_buying_intent_leads == []


def test_unpaid_deposit_past_grace_window_is_flagged(stores):
    import time
    lead = stores["lead_store"].create(tenant_id=TENANT, session_id="s1", phone="9876543210")
    old_sent_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 72 * 3600))
    with stores["lead_store"]._lock, stores["lead_store"]._db() as conn:
        conn.execute("UPDATE leads SET deposit_link_sent_at = ? WHERE lead_id = ?", (old_sent_at, lead.lead_id))
        conn.commit()

    report = compute_revenue_leakage(TENANT, deposit_amount_inr=500, **stores)
    assert len(report.unpaid_deposits) == 1
    assert report.total_estimated_leakage_inr == 500


def test_unpaid_deposit_within_grace_window_is_not_yet_flagged(stores):
    lead = stores["lead_store"].create(tenant_id=TENANT, session_id="s1", phone="9876543210")
    stores["lead_store"].mark_deposit_link_sent(TENANT, lead.lead_id)
    report = compute_revenue_leakage(TENANT, deposit_amount_inr=500, **stores)
    assert report.unpaid_deposits == []


def test_paid_deposit_is_never_flagged(stores):
    import time
    lead = stores["lead_store"].create(tenant_id=TENANT, session_id="s1", phone="9876543210")
    old_sent_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 72 * 3600))
    with stores["lead_store"]._lock, stores["lead_store"]._db() as conn:
        conn.execute("UPDATE leads SET deposit_link_sent_at = ? WHERE lead_id = ?", (old_sent_at, lead.lead_id))
        conn.commit()
    stores["lead_store"].mark_deposit_paid(TENANT, lead.lead_id, 500)
    report = compute_revenue_leakage(TENANT, deposit_amount_inr=500, **stores)
    assert report.unpaid_deposits == []


def test_no_show_appointment_is_flagged(stores):
    lead = stores["lead_store"].create(tenant_id=TENANT, session_id="s1", phone="9876543210")
    stores["lead_store"].record_appointment_outcome(TENANT, lead.lead_id, "no_show")
    report = compute_revenue_leakage(TENANT, **stores)
    assert len(report.no_show_appointments) == 1


def test_render_whatsapp_all_clear_message(stores):
    report = compute_revenue_leakage(TENANT, **stores)
    text = render_revenue_radar_whatsapp(report)
    assert "nothing leaking" in text


def test_render_whatsapp_summarizes_all_categories(stores):
    lead = stores["lead_store"].create(tenant_id=TENANT, session_id="s1", phone="9876543210")
    stores["lead_store"].record_appointment_outcome(TENANT, lead.lead_id, "no_show")
    report = compute_revenue_leakage(TENANT, **stores)
    text = render_revenue_radar_whatsapp(report)
    assert "no-show" in text
