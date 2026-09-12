"""Tests for Business Dependency Intelligence (Phase 10) — the pure
computation in dependency_graph.py, exercised against real store
instances (temp SQLite, no mocks), matching this codebase's established
testing convention.
"""

from __future__ import annotations

import pytest

from business_ai.dependency_graph import (
    MIN_ROSTER_SIZE_FOR_CONCENTRATION_RISK,
    compute_dependency_snapshot,
    simulate_employee_unavailable,
)
from business_ai.employees import EmployeeStore
from business_ai.leads import LeadStore
from business_ai.memory import SopStore
from business_ai.tasks import TaskStore


TENANT = "salon-a"


@pytest.fixture()
def stores(tmp_path):
    employee_store = EmployeeStore(tmp_path / "employees.db")
    task_store = TaskStore(tmp_path / "tasks.db")
    sop_store = SopStore(tmp_path / "sops.db")
    lead_store = LeadStore(tmp_path / "leads.db")
    return employee_store, task_store, sop_store, lead_store


def _snapshot(stores, **kwargs):
    employee_store, task_store, sop_store, lead_store = stores
    return compute_dependency_snapshot(
        TENANT, employee_store=employee_store, task_store=task_store,
        sop_store=sop_store, lead_store=lead_store, **kwargs
    )


def _simulate(stores, employee_id):
    employee_store, task_store, sop_store, lead_store = stores
    return simulate_employee_unavailable(
        TENANT, employee_id, employee_store=employee_store, task_store=task_store,
        sop_store=sop_store, lead_store=lead_store,
    )


def test_empty_tenant_has_no_processes_or_risks(stores):
    snap = _snapshot(stores)
    assert snap["processes"] == []
    assert snap["risks"] == []
    assert snap["workload_concentration"] is None
    assert snap["roster_size"] == 0


def test_one_off_task_is_not_a_process(stores):
    employee_store, task_store, _, _ = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    task_store.create(tenant_id=TENANT, title="one-off special job", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id)
    snap = _snapshot(stores)
    assert snap["processes"] == []


def test_recurring_task_done_only_by_one_person_is_a_bus_factor_one_process(stores):
    employee_store, task_store, _, _ = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    for _ in range(3):
        t = task_store.create(tenant_id=TENANT, title="Restock shelf 3", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id)
        task_store.update_status(TENANT, t.task_id, "done")

    snap = _snapshot(stores)
    assert len(snap["processes"]) == 1
    process = snap["processes"][0]
    assert process["title"] == "Restock shelf 3"
    assert process["bus_factor"] == 1
    assert process["instance_count"] == 3
    assert process["dependency_score_pct"] == 100
    assert len(snap["single_point_of_failure_processes"]) == 1
    assert any(r["type"] == "single_point_of_failure_process" for r in snap["risks"])


def test_recurring_task_done_by_two_people_has_bus_factor_two(stores):
    employee_store, task_store, _, _ = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    priya = employee_store.add(tenant_id=TENANT, whatsapp_number="912", name="Priya")
    for emp in (ravi, ravi, priya):
        t = task_store.create(tenant_id=TENANT, title="Close register", assigned_to_employee_id=emp.employee_id, assigned_by_employee_id=emp.employee_id)
        task_store.update_status(TENANT, t.task_id, "done")

    snap = _snapshot(stores)
    process = snap["processes"][0]
    assert process["bus_factor"] == 2
    assert process["top_employee_name"] == "Ravi"
    assert process["dependency_score_pct"] == 67  # 2 of 3
    assert snap["single_point_of_failure_processes"] == []


def test_process_with_no_completions_yet_falls_back_to_assignees(stores):
    """A brand-new recurring task type with nothing finished yet must
    still report a real bus factor from who it's been ASSIGNED to, not a
    false zero."""
    employee_store, task_store, _, _ = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    for _ in range(2):
        task_store.create(tenant_id=TENANT, title="New weekly audit", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id)

    snap = _snapshot(stores)
    process = snap["processes"][0]
    assert process["done_count"] == 0
    assert process["bus_factor"] == 1


def test_workload_concentration_flagged_only_with_enough_roster(stores):
    employee_store, task_store, _, _ = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    priya = employee_store.add(tenant_id=TENANT, whatsapp_number="912", name="Priya")
    # Only 2 employees — below MIN_ROSTER_SIZE_FOR_CONCENTRATION_RISK.
    for _ in range(5):
        task_store.create(tenant_id=TENANT, title="task", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id)
    task_store.create(tenant_id=TENANT, title="task2", assigned_to_employee_id=priya.employee_id, assigned_by_employee_id=priya.employee_id)

    snap = _snapshot(stores)
    assert snap["workload_concentration"]["share_pct"] > 60
    assert not any(r["type"] == "workload_concentration" for r in snap["risks"])
    assert len(employee_store.list_for_tenant(TENANT)) < MIN_ROSTER_SIZE_FOR_CONCENTRATION_RISK


def test_workload_concentration_flagged_with_enough_roster(stores):
    employee_store, task_store, _, _ = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    for name, num in [("Priya", "912"), ("Asha", "913")]:
        employee_store.add(tenant_id=TENANT, whatsapp_number=num, name=name)
    for i in range(8):
        task_store.create(tenant_id=TENANT, title=f"task-{i}", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id)

    snap = _snapshot(stores)
    assert any(r["type"] == "workload_concentration" for r in snap["risks"])


def test_knowledge_concentration_from_sop_authorship(stores):
    employee_store, _, sop_store, _ = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    employee_store.add(tenant_id=TENANT, whatsapp_number="912", name="Priya")
    employee_store.add(tenant_id=TENANT, whatsapp_number="913", name="Asha")
    for theme in ("equipment_or_supplies", "scheduling_or_shifts", "training_or_process"):
        sop_store.approve(tenant_id=TENANT, theme=theme, text="Do X", approved_by_employee_id=ravi.employee_id)

    snap = _snapshot(stores)
    assert snap["knowledge_concentration"]["employee_name"] == "Ravi"
    assert snap["knowledge_concentration"]["share_pct"] == 100
    assert any(r["type"] == "knowledge_concentration" for r in snap["risks"])


def test_sole_contact_customer_detected(stores):
    employee_store, task_store, _, lead_store = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    lead = lead_store.create(tenant_id=TENANT, session_id="sess1", phone="9990001111", name="Kabir")
    task_store.create(
        tenant_id=TENANT, title="Follow up with Kabir", assigned_to_employee_id=ravi.employee_id,
        assigned_by_employee_id=ravi.employee_id, customer_facing_lead_id=lead.lead_id,
    )

    snap = _snapshot(stores)
    assert len(snap["sole_contact_leads"]) == 1
    assert snap["sole_contact_leads"][0]["employee_name"] == "Ravi"
    assert any(r["type"] == "customer_concentration" for r in snap["risks"])


def test_customer_with_multiple_employees_is_not_sole_contact(stores):
    employee_store, task_store, _, lead_store = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    priya = employee_store.add(tenant_id=TENANT, whatsapp_number="912", name="Priya")
    lead = lead_store.create(tenant_id=TENANT, session_id="sess1", phone="9990001111", name="Kabir")
    task_store.create(tenant_id=TENANT, title="task a", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id, customer_facing_lead_id=lead.lead_id)
    task_store.create(tenant_id=TENANT, title="task b", assigned_to_employee_id=priya.employee_id, assigned_by_employee_id=priya.employee_id, customer_facing_lead_id=lead.lead_id)

    snap = _snapshot(stores)
    assert snap["sole_contact_leads"] == []


def test_risks_are_severity_ordered(stores):
    employee_store, task_store, sop_store, lead_store = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    employee_store.add(tenant_id=TENANT, whatsapp_number="912", name="Priya")
    employee_store.add(tenant_id=TENANT, whatsapp_number="913", name="Asha")
    for _ in range(2):
        t = task_store.create(tenant_id=TENANT, title="Restock", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id)
        task_store.update_status(TENANT, t.task_id, "done")
    for theme in ("equipment_or_supplies", "scheduling_or_shifts"):
        sop_store.approve(tenant_id=TENANT, theme=theme, text="Do X", approved_by_employee_id=ravi.employee_id)

    snap = _snapshot(stores)
    severities = [r["severity"] for r in snap["risks"]]
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])


# ------------------------------------------------------------------ simulate_employee_unavailable


def test_simulate_unavailable_reports_orphaned_open_tasks(stores):
    employee_store, task_store, _, _ = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    task_store.create(tenant_id=TENANT, title="Pending job", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id)

    result = _simulate(stores, ravi.employee_id)
    assert len(result["orphaned_open_tasks"]) == 1
    assert result["is_currently_a_risk"] is True


def test_simulate_unavailable_reports_orphaned_processes(stores):
    employee_store, task_store, _, _ = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    for _ in range(2):
        t = task_store.create(tenant_id=TENANT, title="Weekly report", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id)
        task_store.update_status(TENANT, t.task_id, "done")

    result = _simulate(stores, ravi.employee_id)
    assert result["orphaned_processes"] == [{"title": "Weekly report", "instance_count": 2}]


def test_simulate_unavailable_reports_sole_contact_leads(stores):
    employee_store, task_store, _, lead_store = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    lead = lead_store.create(tenant_id=TENANT, session_id="sess1", phone="9990001111", name="Kabir")
    task_store.create(tenant_id=TENANT, title="Follow up", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id, customer_facing_lead_id=lead.lead_id)

    result = _simulate(stores, ravi.employee_id)
    assert result["sole_contact_leads"] == [{"lead_id": lead.lead_id, "lead_name": "Kabir"}]


def test_simulate_unavailable_reports_authored_sop_themes(stores):
    employee_store, _, sop_store, _ = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    sop_store.approve(tenant_id=TENANT, theme="training_or_process", text="Do X", approved_by_employee_id=ravi.employee_id)

    result = _simulate(stores, ravi.employee_id)
    assert result["authored_sop_themes"] == ["training_or_process"]


def test_simulate_unavailable_for_employee_with_nothing_is_not_a_risk(stores):
    employee_store, _, _, _ = stores
    priya = employee_store.add(tenant_id=TENANT, whatsapp_number="912", name="Priya")

    result = _simulate(stores, priya.employee_id)
    assert result["is_currently_a_risk"] is False
    assert result["orphaned_open_tasks"] == []
    assert result["orphaned_processes"] == []
    assert result["sole_contact_leads"] == []


def test_simulate_unavailable_for_unknown_employee_id_does_not_crash(stores):
    result = _simulate(stores, "emp_does_not_exist")
    assert result["employee_name"] == "(former employee)"
    assert result["is_currently_a_risk"] is False


def test_snapshot_is_tenant_isolated(stores):
    employee_store, task_store, _, _ = stores
    ravi = employee_store.add(tenant_id=TENANT, whatsapp_number="911", name="Ravi")
    for _ in range(3):
        t = task_store.create(tenant_id=TENANT, title="Restock", assigned_to_employee_id=ravi.employee_id, assigned_by_employee_id=ravi.employee_id)
        task_store.update_status(TENANT, t.task_id, "done")

    other_snap = compute_dependency_snapshot(
        "salon-b", employee_store=employee_store, task_store=task_store,
        sop_store=stores[2], lead_store=stores[3],
    )
    assert other_snap["processes"] == []
    assert other_snap["roster_size"] == 0
