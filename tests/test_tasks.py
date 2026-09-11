"""Unit tests for TaskStore: assignment, status lifecycle, overdue
detection, and approval flow that the admin bot's WhatsApp commands
drive (see app.py's _handle_admin_bot_message)."""

from __future__ import annotations

from pathlib import Path

import pytest

from business_ai.tasks import TaskStore


@pytest.fixture()
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.db")


def test_create_and_get(store):
    task = store.create(
        tenant_id="t1", title="restock shelf 3", assigned_to_employee_id="emp_ravi", assigned_by_employee_id="emp_owner"
    )
    assert task.status == "open"
    fetched = store.get("t1", task.task_id)
    assert fetched.title == "restock shelf 3"


def test_get_is_tenant_scoped(store):
    task = store.create(tenant_id="t1", title="x", assigned_to_employee_id="e1", assigned_by_employee_id="e2")
    assert store.get("t2", task.task_id) is None


def test_list_open_for_tenant_excludes_terminal_statuses(store):
    t1 = store.create(tenant_id="t1", title="a", assigned_to_employee_id="e1", assigned_by_employee_id="e2")
    t2 = store.create(tenant_id="t1", title="b", assigned_to_employee_id="e1", assigned_by_employee_id="e2")
    store.update_status("t1", t2.task_id, "done")
    open_tasks = store.list_open_for_tenant("t1")
    assert [t.task_id for t in open_tasks] == [t1.task_id]


def test_list_overdue_only_open_tasks_past_due(store):
    store.create(
        tenant_id="t1", title="past due", assigned_to_employee_id="e1", assigned_by_employee_id="e2",
        due_at="2020-01-01T00:00:00Z",
    )
    future = store.create(
        tenant_id="t1", title="future", assigned_to_employee_id="e1", assigned_by_employee_id="e2",
        due_at="2099-01-01T00:00:00Z",
    )
    done_but_overdue = store.create(
        tenant_id="t1", title="done", assigned_to_employee_id="e1", assigned_by_employee_id="e2",
        due_at="2020-01-01T00:00:00Z",
    )
    store.update_status("t1", done_but_overdue.task_id, "done")

    overdue = store.list_overdue("t1")
    assert len(overdue) == 1
    assert overdue[0].title == "past due"
    assert future.task_id not in [t.task_id for t in overdue]


def test_update_status_rejects_invalid_status(store):
    task = store.create(tenant_id="t1", title="a", assigned_to_employee_id="e1", assigned_by_employee_id="e2")
    with pytest.raises(ValueError):
        store.update_status("t1", task.task_id, "not-a-status")


def test_blocked_status_records_reason(store):
    task = store.create(tenant_id="t1", title="a", assigned_to_employee_id="e1", assigned_by_employee_id="e2")
    store.update_status("t1", task.task_id, "blocked", block_reason="waiting on delivery")
    assert store.get("t1", task.task_id).block_reason == "waiting on delivery"


def test_reassign(store):
    task = store.create(tenant_id="t1", title="a", assigned_to_employee_id="e1", assigned_by_employee_id="e2")
    store.reassign("t1", task.task_id, "e3")
    assert store.get("t1", task.task_id).assigned_to_employee_id == "e3"


def test_approve_marks_done_with_approver(store):
    task = store.create(
        tenant_id="t1", title="a", assigned_to_employee_id="e1", assigned_by_employee_id="e2", approval_required=True
    )
    store.update_status("t1", task.task_id, "awaiting_approval")
    store.approve("t1", task.task_id, "e2")
    updated = store.get("t1", task.task_id)
    assert updated.status == "done"
    assert updated.approved_by_employee_id == "e2"
    assert updated.approved_at is not None


def test_reject_sends_back_to_in_progress(store):
    task = store.create(
        tenant_id="t1", title="a", assigned_to_employee_id="e1", assigned_by_employee_id="e2", approval_required=True
    )
    store.update_status("t1", task.task_id, "awaiting_approval")
    store.reject("t1", task.task_id, reason="needs the receipt")
    updated = store.get("t1", task.task_id)
    assert updated.status == "in_progress"
    assert updated.block_reason == "needs the receipt"


def test_count_open_for_tenant(store):
    store.create(tenant_id="t1", title="a", assigned_to_employee_id="e1", assigned_by_employee_id="e2")
    t2 = store.create(tenant_id="t1", title="b", assigned_to_employee_id="e1", assigned_by_employee_id="e2")
    store.update_status("t1", t2.task_id, "cancelled")
    assert store.count_open_for_tenant("t1") == 1


def test_list_for_tenant_filters_by_assignee(store):
    store.create(tenant_id="t1", title="a", assigned_to_employee_id="e1", assigned_by_employee_id="e2")
    store.create(tenant_id="t1", title="b", assigned_to_employee_id="e3", assigned_by_employee_id="e2")
    mine = store.list_for_tenant("t1", assigned_to_employee_id="e1")
    assert len(mine) == 1
    assert mine[0].title == "a"
