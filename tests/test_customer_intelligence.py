"""Tests for Phase 23's repeat-customer intelligence — pure derived
computation over LeadStore data, matching scorecard.py/revenue_radar.py's
own testing shape.
"""

from __future__ import annotations

from business_ai.customer_intelligence import build_repeat_customer_report
from business_ai.leads import LeadStore

TENANT = "trattoria-a"


def test_customer_with_two_completed_visits_is_a_repeat_customer(tmp_path):
    leads = LeadStore(tmp_path / "leads.db")
    l1 = leads.create(tenant_id=TENANT, session_id="s1", name="John", phone="9876543210")
    leads.record_appointment_outcome(TENANT, l1.lead_id, "completed")
    l2 = leads.create(tenant_id=TENANT, session_id="s2", name="John", phone="9876543210")
    leads.record_appointment_outcome(TENANT, l2.lead_id, "completed")

    report = build_repeat_customer_report(TENANT, lead_store=leads)
    assert len(report.repeat_customers) == 1
    assert report.repeat_customers[0].phone == "9876543210"
    assert report.repeat_customers[0].visit_count == 2


def test_customer_with_one_completed_visit_is_not_a_repeat_customer(tmp_path):
    leads = LeadStore(tmp_path / "leads.db")
    l1 = leads.create(tenant_id=TENANT, session_id="s1", name="John", phone="9876543210")
    leads.record_appointment_outcome(TENANT, l1.lead_id, "completed")

    report = build_repeat_customer_report(TENANT, lead_store=leads)
    assert report.repeat_customers == []
    assert report.total_customers_with_a_completed_visit == 1


def test_no_show_or_cancelled_visits_never_count(tmp_path):
    leads = LeadStore(tmp_path / "leads.db")
    l1 = leads.create(tenant_id=TENANT, session_id="s1", name="Jane", phone="9999999999")
    leads.record_appointment_outcome(TENANT, l1.lead_id, "no_show")
    l2 = leads.create(tenant_id=TENANT, session_id="s2", name="Jane", phone="9999999999")
    leads.record_appointment_outcome(TENANT, l2.lead_id, "cancelled")

    report = build_repeat_customer_report(TENANT, lead_store=leads)
    assert report.repeat_customers == []
    assert report.total_customers_with_a_completed_visit == 0


def test_leads_with_no_phone_are_excluded(tmp_path):
    leads = LeadStore(tmp_path / "leads.db")
    l1 = leads.create(tenant_id=TENANT, session_id="s1", name="Anon", email="a@example.com")
    leads.record_appointment_outcome(TENANT, l1.lead_id, "completed")
    l2 = leads.create(tenant_id=TENANT, session_id="s2", name="Anon", email="a@example.com")
    leads.record_appointment_outcome(TENANT, l2.lead_id, "completed")

    report = build_repeat_customer_report(TENANT, lead_store=leads)
    assert report.repeat_customers == []


def test_confirmed_revenue_sums_only_owner_confirmed_deposits(tmp_path):
    leads = LeadStore(tmp_path / "leads.db")
    l1 = leads.create(tenant_id=TENANT, session_id="s1", name="John", phone="9876543210")
    leads.record_appointment_outcome(TENANT, l1.lead_id, "completed")
    leads.mark_deposit_paid(TENANT, l1.lead_id, amount_inr=300)
    l2 = leads.create(tenant_id=TENANT, session_id="s2", name="John", phone="9876543210")
    leads.record_appointment_outcome(TENANT, l2.lead_id, "completed")
    leads.mark_deposit_paid(TENANT, l2.lead_id, amount_inr=200)

    report = build_repeat_customer_report(TENANT, lead_store=leads)
    assert report.repeat_customers[0].confirmed_revenue_inr == 500


def test_repeat_customers_sorted_by_visit_count_descending(tmp_path):
    leads = LeadStore(tmp_path / "leads.db")
    for i in range(2):
        lead = leads.create(tenant_id=TENANT, session_id=f"a{i}", name="Alice", phone="1111111111")
        leads.record_appointment_outcome(TENANT, lead.lead_id, "completed")
    for i in range(4):
        lead = leads.create(tenant_id=TENANT, session_id=f"b{i}", name="Bob", phone="2222222222")
        leads.record_appointment_outcome(TENANT, lead.lead_id, "completed")

    report = build_repeat_customer_report(TENANT, lead_store=leads)
    assert [c.name for c in report.repeat_customers] == ["Bob", "Alice"]


def test_report_is_empty_for_a_tenant_with_no_leads(tmp_path):
    leads = LeadStore(tmp_path / "leads.db")
    report = build_repeat_customer_report(TENANT, lead_store=leads)
    assert report.repeat_customers == []
    assert report.total_customers_with_a_completed_visit == 0
