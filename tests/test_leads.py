"""Unit tests for LeadStore's outcome-confirmation methods and the
deterministic lead_stage() funnel — the "request-and-confirm, no
webhook" honesty pattern applied to customer revenue (see leads.py's
docstrings for why sent-a-link/set-an-appointment never counts as
converted on its own)."""

from __future__ import annotations

from pathlib import Path

import pytest

from business_ai.leads import LeadStore, lead_stage


@pytest.fixture()
def store(tmp_path: Path) -> LeadStore:
    return LeadStore(tmp_path / "leads.db")


def _lead(store, tenant_id="t1", **overrides):
    defaults = dict(tenant_id=tenant_id, session_id="s1", phone="919876543210")
    defaults.update(overrides)
    return store.create(**defaults)


def test_new_lead_stage_is_new(store):
    lead = _lead(store)
    assert lead_stage(lead) == "new"


def test_engaged_stage_once_appointment_set(store):
    lead = _lead(store)
    updated = store.set_appointment("t1", lead.lead_id, "2027-01-01T10:00:00Z")
    assert lead_stage(updated) == "engaged"


def test_awaiting_payment_stage_once_deposit_link_sent(store):
    lead = _lead(store)
    store.mark_deposit_link_sent("t1", lead.lead_id)
    updated = store.get("t1", lead.lead_id)
    assert lead_stage(updated) == "awaiting_payment"


def test_converted_stage_once_deposit_confirmed_paid(store):
    lead = _lead(store)
    store.mark_deposit_link_sent("t1", lead.lead_id)
    updated = store.mark_deposit_paid("t1", lead.lead_id, 500)
    assert lead_stage(updated) == "converted"
    assert updated.deposit_paid_amount_inr == 500


def test_converted_stage_once_appointment_completed(store):
    lead = _lead(store)
    store.set_appointment("t1", lead.lead_id, "2027-01-01T10:00:00Z")
    updated = store.record_appointment_outcome("t1", lead.lead_id, "completed")
    assert lead_stage(updated) == "converted"


def test_lost_stage_on_no_show_or_cancelled(store):
    lead = _lead(store)
    updated = store.record_appointment_outcome("t1", lead.lead_id, "no_show")
    assert lead_stage(updated) == "lost"


def test_reengaged_stage(store):
    lead = _lead(store)
    store.mark_reengaged("t1", lead.lead_id)
    updated = store.get("t1", lead.lead_id)
    assert lead_stage(updated) == "reengaged"


def test_invalid_outcome_rejected(store):
    lead = _lead(store)
    with pytest.raises(ValueError):
        store.record_appointment_outcome("t1", lead.lead_id, "maybe")


def test_invalid_deposit_amount_rejected(store):
    lead = _lead(store)
    with pytest.raises(ValueError):
        store.mark_deposit_paid("t1", lead.lead_id, 0)
    with pytest.raises(ValueError):
        store.mark_deposit_paid("t1", lead.lead_id, -100)


def test_mark_deposit_paid_is_tenant_scoped(store):
    lead = _lead(store, tenant_id="t1")
    assert store.mark_deposit_paid("t2", lead.lead_id, 500) is None
    assert store.get("t1", lead.lead_id).deposit_paid_at is None


def test_sum_confirmed_revenue_only_counts_confirmed_payments(store):
    a = _lead(store, tenant_id="t1", session_id="s1")
    b = _lead(store, tenant_id="t1", session_id="s2")
    _lead(store, tenant_id="t1", session_id="s3")  # never paid — must not count
    store.mark_deposit_paid("t1", a.lead_id, 500)
    store.mark_deposit_paid("t1", b.lead_id, 750)
    assert store.sum_confirmed_revenue("t1") == 1250


def test_sum_confirmed_revenue_is_tenant_scoped(store):
    a = _lead(store, tenant_id="t1", session_id="s1")
    b = _lead(store, tenant_id="t2", session_id="s2")
    store.mark_deposit_paid("t1", a.lead_id, 500)
    store.mark_deposit_paid("t2", b.lead_id, 999)
    assert store.sum_confirmed_revenue("t1") == 500


def test_sum_confirmed_revenue_windowed_by_confirmation_time(store):
    a = _lead(store, tenant_id="t1", session_id="s1")
    store.mark_deposit_paid("t1", a.lead_id, 500)
    future_cutoff = "2099-01-01T00:00:00Z"
    assert store.sum_confirmed_revenue("t1", since_iso=future_cutoff) == 0
    assert store.sum_confirmed_revenue("t1", since_iso="2000-01-01T00:00:00Z") == 500


def test_count_converted_counts_either_outcome_once(store):
    a = _lead(store, tenant_id="t1", session_id="s1")
    b = _lead(store, tenant_id="t1", session_id="s2")
    _lead(store, tenant_id="t1", session_id="s3")  # not converted
    store.mark_deposit_paid("t1", a.lead_id, 500)
    store.record_appointment_outcome("t1", b.lead_id, "completed")
    assert store.count_converted("t1") == 2
