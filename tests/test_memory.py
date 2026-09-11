"""Unit tests for SopStore: owner-approved workaround notes for recurring
feedback themes (see app.py's "approve sop" command and _record_feedback)."""

from __future__ import annotations

from pathlib import Path

import pytest

from business_ai.memory import SopStore


@pytest.fixture()
def store(tmp_path: Path) -> SopStore:
    return SopStore(tmp_path / "sops.db")


def test_approve_and_get(store):
    note = store.approve(
        tenant_id="t1", theme="software_or_tools", text="re-login every 2 hours", approved_by_employee_id="emp_1"
    )
    fetched = store.get_for_theme("t1", "software_or_tools")
    assert fetched.note_id == note.note_id
    assert fetched.text == "re-login every 2 hours"


def test_get_for_theme_is_tenant_scoped(store):
    store.approve(tenant_id="t1", theme="software_or_tools", text="fix A", approved_by_employee_id="emp_1")
    assert store.get_for_theme("t2", "software_or_tools") is None


def test_re_approving_same_theme_updates_in_place(store):
    first = store.approve(tenant_id="t1", theme="software_or_tools", text="fix A", approved_by_employee_id="emp_1")
    second = store.approve(tenant_id="t1", theme="software_or_tools", text="fix B", approved_by_employee_id="emp_2")
    assert second.note_id == first.note_id
    assert second.text == "fix B"
    assert len(store.list_for_tenant("t1")) == 1


def test_empty_text_rejected(store):
    with pytest.raises(ValueError):
        store.approve(tenant_id="t1", theme="software_or_tools", text="  ", approved_by_employee_id="emp_1")


def test_list_for_tenant(store):
    store.approve(tenant_id="t1", theme="software_or_tools", text="fix A", approved_by_employee_id="emp_1")
    store.approve(tenant_id="t1", theme="scheduling_or_shifts", text="fix B", approved_by_employee_id="emp_1")
    store.approve(tenant_id="t2", theme="software_or_tools", text="fix C", approved_by_employee_id="emp_1")
    assert len(store.list_for_tenant("t1")) == 2
    assert len(store.list_for_tenant("t2")) == 1
