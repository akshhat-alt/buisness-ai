"""Unit tests for the single authorization chokepoint: authorize().

This is the actual security-critical function in the whole app — every
route funnels through it. Covers: unauthenticated denial, cross-tenant
denial, role-based action gating, and non-ACTIVE lifecycle gating."""

from __future__ import annotations

from pathlib import Path

import pytest

from business_ai.auth import Principal
from business_ai.tenant import (
    TenantAction,
    TenantConfig,
    TenantRegistry,
    TenantStatus,
    UnauthorizedError,
    authorize,
)


@pytest.fixture()
def registry(tmp_path: Path) -> TenantRegistry:
    reg = TenantRegistry(tmp_path / "tenants.db")
    reg.register(TenantConfig(tenant_id="salon-a", business_name="Salon A", owner_email="a@x.com", status=TenantStatus.ACTIVE))
    reg.register(TenantConfig(tenant_id="salon-b", business_name="Salon B", owner_email="b@x.com", status=TenantStatus.PROVISIONING))
    return reg


def test_unauthenticated_principal_is_rejected_for_non_public_actions(registry):
    with pytest.raises(UnauthorizedError):
        authorize(None, TenantAction.VIEW_LEADS, target_tenant_id="salon-a", registry=registry)


def test_anonymous_customer_can_query_an_active_tenant(registry):
    """A real customer using the embedded chat widget has no Business AI
    account at all — this must work with no principal, as long as the
    business is ACTIVE."""
    config = authorize(None, TenantAction.QUERY_ASSISTANT, target_tenant_id="salon-a", registry=registry)
    assert config.tenant_id == "salon-a"


def test_anonymous_customer_cannot_query_a_non_active_tenant(registry):
    with pytest.raises(UnauthorizedError):
        authorize(None, TenantAction.QUERY_ASSISTANT, target_tenant_id="salon-b", registry=registry)


def test_anonymous_visitor_can_read_public_tenant_info(registry):
    config = authorize(None, TenantAction.VIEW_PUBLIC_INFO, target_tenant_id="salon-a", registry=registry)
    assert config.tenant_id == "salon-a"


def test_owner_cannot_act_on_a_different_tenant(registry):
    owner = Principal.owner("user_1", "salon-a")
    with pytest.raises(UnauthorizedError):
        authorize(owner, TenantAction.VIEW_LEADS, target_tenant_id="salon-b", registry=registry)


def test_platform_admin_can_act_on_any_tenant(registry):
    admin = Principal.platform_admin("admin_1")
    config = authorize(admin, TenantAction.INSPECT_ALL_TENANTS, target_tenant_id="salon-b", registry=registry)
    assert config.tenant_id == "salon-b"


def test_anonymous_and_cross_tenant_queries_still_blocked_while_provisioning(registry):
    """salon-b is PROVISIONING: a real customer (no principal) or a
    principal from a DIFFERENT tenant must still be rejected — only the
    tenant's own owner/manager gets the Phase 8 preview exception below."""
    with pytest.raises(UnauthorizedError):
        authorize(None, TenantAction.QUERY_ASSISTANT, target_tenant_id="salon-b", registry=registry)
    owner_a = Principal.owner("user_1", "salon-a")
    with pytest.raises(UnauthorizedError):
        authorize(owner_a, TenantAction.QUERY_ASSISTANT, target_tenant_id="salon-b", registry=registry)


def test_owner_and_manager_can_preview_their_own_provisioning_assistant(registry):
    """Phase 8: the whole point of a "test before you activate" onboarding
    step — the tenant's own owner/manager may query their own assistant
    while still PROVISIONING, so the wizard's test step is a real
    capability rather than a silent 400."""
    owner_b = Principal.owner("user_2", "salon-b")
    config = authorize(owner_b, TenantAction.QUERY_ASSISTANT, target_tenant_id="salon-b", registry=registry)
    assert config.tenant_id == "salon-b"

    manager_b = Principal.manager("user_4", "salon-b")
    config = authorize(manager_b, TenantAction.QUERY_ASSISTANT, target_tenant_id="salon-b", registry=registry)
    assert config.tenant_id == "salon-b"


def test_staff_cannot_preview_a_provisioning_assistant(registry):
    """Scoped narrowly to owner/manager — staff logins aren't part of the
    onboarding flow, so the exception doesn't extend to them."""
    staff_b = Principal.staff("user_5", "salon-b")
    with pytest.raises(UnauthorizedError):
        authorize(staff_b, TenantAction.QUERY_ASSISTANT, target_tenant_id="salon-b", registry=registry)


def test_suspended_tenant_owner_cannot_preview(registry):
    """SUSPENDED is never exempted, even for the tenant's own owner —
    that status means something is deliberately wrong."""
    registry.register(
        TenantConfig(tenant_id="salon-c", business_name="Salon C", owner_email="c@x.com", status=TenantStatus.SUSPENDED),
        override_existing=True,
    )
    owner_c = Principal.owner("user_6", "salon-c")
    with pytest.raises(UnauthorizedError):
        authorize(owner_c, TenantAction.QUERY_ASSISTANT, target_tenant_id="salon-c", registry=registry)


def test_owner_dashboard_actions_work_while_provisioning(registry):
    """Regression test: an owner must be able to view their own leads,
    analytics, and knowledge sources while still setting up (PROVISIONING),
    before a platform admin has activated them. A prior bug gated these
    behind ACTIVE status and broke "see what I've uploaded so far"."""
    owner_b = Principal.owner("user_2", "salon-b")
    for action in (TenantAction.VIEW_LEADS, TenantAction.VIEW_ANALYTICS, TenantAction.INGEST_KNOWLEDGE, TenantAction.MANAGE_ASSISTANT):
        config = authorize(owner_b, action, target_tenant_id="salon-b", registry=registry)
        assert config.tenant_id == "salon-b"


def test_staff_cannot_manage_assistant_settings(registry):
    staff = Principal.staff("user_3", "salon-a")
    with pytest.raises(UnauthorizedError):
        authorize(staff, TenantAction.MANAGE_ASSISTANT, target_tenant_id="salon-a", registry=registry)


def test_staff_can_query_and_view(registry):
    staff = Principal.staff("user_3", "salon-a")
    for action in (TenantAction.QUERY_ASSISTANT, TenantAction.VIEW_LEADS, TenantAction.VIEW_ANALYTICS):
        authorize(staff, action, target_tenant_id="salon-a", registry=registry)


def test_unrecognized_role_gets_no_access_not_staff_fallback(registry):
    """Fail-closed regression test: authorize()'s role lookup used to be
    `OWNER_ACTIONS if role == "owner" else STAFF_ACTIONS`, which silently
    granted staff-level access to ANY unrecognized role string. It must
    now deny everything for a role that isn't in ROLE_ACTIONS."""
    from business_ai.auth import Principal as _Principal

    bogus = _Principal(principal_id="user_9", tenant_id="salon-a", role="totally-not-a-real-role")
    for action in (TenantAction.QUERY_ASSISTANT, TenantAction.VIEW_LEADS, TenantAction.VIEW_ANALYTICS):
        with pytest.raises(UnauthorizedError):
            authorize(bogus, action, target_tenant_id="salon-a", registry=registry)


def test_manager_has_owner_powers_minus_knowledge_and_roster(registry):
    manager = Principal.manager("user_4", "salon-a")
    for action in (
        TenantAction.QUERY_ASSISTANT,
        TenantAction.VIEW_LEADS,
        TenantAction.VIEW_ANALYTICS,
        TenantAction.ASSIGN_TASK,
        TenantAction.VIEW_TASKS,
        TenantAction.UPDATE_TASK_STATUS,
    ):
        authorize(manager, action, target_tenant_id="salon-a", registry=registry)
    for action in (TenantAction.INGEST_KNOWLEDGE, TenantAction.MANAGE_ASSISTANT, TenantAction.MANAGE_EMPLOYEES):
        with pytest.raises(UnauthorizedError):
            authorize(manager, action, target_tenant_id="salon-a", registry=registry)


def test_staff_can_view_and_update_own_tasks_but_not_assign(registry):
    staff = Principal.staff("user_5", "salon-a")
    for action in (TenantAction.VIEW_TASKS, TenantAction.UPDATE_TASK_STATUS):
        authorize(staff, action, target_tenant_id="salon-a", registry=registry)
    with pytest.raises(UnauthorizedError):
        authorize(staff, TenantAction.ASSIGN_TASK, target_tenant_id="salon-a", registry=registry)
    with pytest.raises(UnauthorizedError):
        authorize(staff, TenantAction.MANAGE_EMPLOYEES, target_tenant_id="salon-a", registry=registry)


def test_manager_can_view_feedback_but_staff_cannot(registry):
    manager = Principal.manager("user_8", "salon-a")
    authorize(manager, TenantAction.VIEW_FEEDBACK, target_tenant_id="salon-a", registry=registry)
    staff = Principal.staff("user_9", "salon-a")
    with pytest.raises(UnauthorizedError):
        authorize(staff, TenantAction.VIEW_FEEDBACK, target_tenant_id="salon-a", registry=registry)


def test_only_owner_can_manage_employees(registry):
    owner = Principal.owner("user_6", "salon-a")
    manager = Principal.manager("user_7", "salon-a")
    authorize(owner, TenantAction.MANAGE_EMPLOYEES, target_tenant_id="salon-a", registry=registry)
    with pytest.raises(UnauthorizedError):
        authorize(manager, TenantAction.MANAGE_EMPLOYEES, target_tenant_id="salon-a", registry=registry)


def test_manager_can_view_automation_but_not_manage_it(registry):
    manager = Principal.manager("user_10", "salon-a")
    authorize(manager, TenantAction.VIEW_AUTOMATION, target_tenant_id="salon-a", registry=registry)
    with pytest.raises(UnauthorizedError):
        authorize(manager, TenantAction.MANAGE_AUTOMATION, target_tenant_id="salon-a", registry=registry)


def test_only_owner_can_manage_automation(registry):
    owner = Principal.owner("user_11", "salon-a")
    staff = Principal.staff("user_12", "salon-a")
    authorize(owner, TenantAction.MANAGE_AUTOMATION, target_tenant_id="salon-a", registry=registry)
    with pytest.raises(UnauthorizedError):
        authorize(staff, TenantAction.MANAGE_AUTOMATION, target_tenant_id="salon-a", registry=registry)
    with pytest.raises(UnauthorizedError):
        authorize(staff, TenantAction.VIEW_AUTOMATION, target_tenant_id="salon-a", registry=registry)
