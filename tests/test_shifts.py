"""Tests for Phase 23's ShiftStore — tenant isolation, CRUD, and the
list_for_tenant filters, matching tasks.py's own test shape.
"""

from __future__ import annotations

import pytest

from business_ai.shifts import ShiftStore

TENANT = "trattoria-a"
OTHER_TENANT = "trattoria-b"


def test_create_and_get_shift(tmp_path):
    store = ShiftStore(tmp_path / "shifts.db")
    shift = store.create(
        tenant_id=TENANT, employee_id="emp_1", shift_date="2026-09-20",
        start_time="09:00", end_time="17:00", role_label="kitchen",
    )
    fetched = store.get(TENANT, shift.shift_id)
    assert fetched is not None
    assert fetched.employee_id == "emp_1"
    assert fetched.role_label == "kitchen"


def test_create_requires_all_fields(tmp_path):
    store = ShiftStore(tmp_path / "shifts.db")
    with pytest.raises(ValueError):
        store.create(tenant_id=TENANT, employee_id="", shift_date="2026-09-20", start_time="09:00", end_time="17:00")


def test_list_for_tenant_filters_by_employee_and_date(tmp_path):
    store = ShiftStore(tmp_path / "shifts.db")
    store.create(tenant_id=TENANT, employee_id="emp_1", shift_date="2026-09-20", start_time="09:00", end_time="17:00")
    store.create(tenant_id=TENANT, employee_id="emp_2", shift_date="2026-09-20", start_time="10:00", end_time="18:00")
    store.create(tenant_id=TENANT, employee_id="emp_1", shift_date="2026-09-21", start_time="09:00", end_time="17:00")

    all_shifts = store.list_for_tenant(TENANT)
    assert len(all_shifts) == 3

    emp1_shifts = store.list_for_tenant(TENANT, employee_id="emp_1")
    assert len(emp1_shifts) == 2

    date_shifts = store.list_for_tenant(TENANT, shift_date="2026-09-20")
    assert len(date_shifts) == 2

    both = store.list_for_tenant(TENANT, employee_id="emp_1", shift_date="2026-09-20")
    assert len(both) == 1


def test_list_for_tenant_orders_by_date_then_start_time(tmp_path):
    store = ShiftStore(tmp_path / "shifts.db")
    store.create(tenant_id=TENANT, employee_id="emp_1", shift_date="2026-09-21", start_time="09:00", end_time="17:00")
    store.create(tenant_id=TENANT, employee_id="emp_2", shift_date="2026-09-20", start_time="14:00", end_time="22:00")
    store.create(tenant_id=TENANT, employee_id="emp_3", shift_date="2026-09-20", start_time="06:00", end_time="14:00")

    shifts = store.list_for_tenant(TENANT)
    assert [s.employee_id for s in shifts] == ["emp_3", "emp_2", "emp_1"]


def test_delete_removes_only_the_target_shift(tmp_path):
    store = ShiftStore(tmp_path / "shifts.db")
    s1 = store.create(tenant_id=TENANT, employee_id="emp_1", shift_date="2026-09-20", start_time="09:00", end_time="17:00")
    s2 = store.create(tenant_id=TENANT, employee_id="emp_2", shift_date="2026-09-20", start_time="10:00", end_time="18:00")

    assert store.delete(TENANT, s1.shift_id) is True
    assert store.get(TENANT, s1.shift_id) is None
    assert store.get(TENANT, s2.shift_id) is not None


def test_delete_unknown_shift_returns_false(tmp_path):
    store = ShiftStore(tmp_path / "shifts.db")
    assert store.delete(TENANT, "does-not-exist") is False


def test_shifts_are_tenant_isolated(tmp_path):
    store = ShiftStore(tmp_path / "shifts.db")
    store.create(tenant_id=TENANT, employee_id="emp_1", shift_date="2026-09-20", start_time="09:00", end_time="17:00")
    assert store.list_for_tenant(OTHER_TENANT) == []


def test_delete_for_tenant_removes_all_rows(tmp_path):
    store = ShiftStore(tmp_path / "shifts.db")
    store.create(tenant_id=TENANT, employee_id="emp_1", shift_date="2026-09-20", start_time="09:00", end_time="17:00")
    store.create(tenant_id=TENANT, employee_id="emp_2", shift_date="2026-09-21", start_time="09:00", end_time="17:00")
    assert store.delete_for_tenant(TENANT) == 2
    assert store.list_for_tenant(TENANT) == []
