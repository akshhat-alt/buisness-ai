"""Unit tests for EmployeeStore: the tenant-scoped WhatsApp roster the
admin bot's webhook routing depends on for identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from business_ai.employees import EmployeeStore, normalize_whatsapp_number


@pytest.fixture()
def store(tmp_path: Path) -> EmployeeStore:
    return EmployeeStore(tmp_path / "employees.db")


def test_normalize_whatsapp_number_strips_non_digits():
    assert normalize_whatsapp_number("+91 98765 43210") == "919876543210"
    assert normalize_whatsapp_number("919876543210") == "919876543210"
    assert normalize_whatsapp_number(None) == ""


def test_add_and_find_by_whatsapp(store):
    store.add(tenant_id="t1", whatsapp_number="+91 98765 43210", name="Ravi", role="staff")
    found = store.find_by_whatsapp("t1", "919876543210")
    assert found is not None
    assert found.name == "Ravi"
    assert found.role == "staff"


def test_find_by_whatsapp_is_tenant_scoped(store):
    store.add(tenant_id="t1", whatsapp_number="919876543210", name="Ravi", role="staff")
    assert store.find_by_whatsapp("t2", "919876543210") is None


def test_re_adding_same_number_updates_in_place(store):
    first = store.add(tenant_id="t1", whatsapp_number="919876543210", name="Ravi", role="staff")
    second = store.add(tenant_id="t1", whatsapp_number="919876543210", name="Ravi K.", role="manager")
    assert second.employee_id == first.employee_id
    assert second.name == "Ravi K."
    assert second.role == "manager"
    assert len(store.list_for_tenant("t1")) == 1


def test_invalid_role_rejected(store):
    with pytest.raises(ValueError):
        store.add(tenant_id="t1", whatsapp_number="919876543210", name="Ravi", role="platform_admin")
    with pytest.raises(ValueError):
        store.add(tenant_id="t1", whatsapp_number="919876543210", name="Ravi", role="not-a-role")


def test_deactivate_removes_from_active_lookup(store):
    emp = store.add(tenant_id="t1", whatsapp_number="919876543210", name="Ravi", role="staff")
    store.deactivate("t1", emp.employee_id)
    assert store.find_by_whatsapp("t1", "919876543210") is None
    assert store.list_for_tenant("t1", active_only=True) == []
    assert len(store.list_for_tenant("t1", active_only=False)) == 1


def test_set_role(store):
    emp = store.add(tenant_id="t1", whatsapp_number="919876543210", name="Ravi", role="staff")
    updated = store.set_role("t1", emp.employee_id, "manager")
    assert updated.role == "manager"


def test_ensure_owner_bootstrap_is_idempotent_and_tenant_scoped(store):
    store.ensure_owner_bootstrap("t1", "919999888877", name="Owner")
    store.ensure_owner_bootstrap("t1", "919999888877", name="Owner")  # second call is a no-op
    roster = store.list_for_tenant("t1")
    assert len(roster) == 1
    assert roster[0].role == "owner"
    assert store.find_by_whatsapp("t2", "919999888877") is None


def test_ensure_owner_bootstrap_noop_when_no_number_configured(store):
    store.ensure_owner_bootstrap("t1", None)
    assert store.list_for_tenant("t1") == []
