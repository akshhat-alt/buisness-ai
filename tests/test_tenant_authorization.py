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


def test_query_assistant_requires_active_status(registry):
    owner_b = Principal.owner("user_2", "salon-b")  # salon-b is PROVISIONING
    with pytest.raises(UnauthorizedError):
        authorize(owner_b, TenantAction.QUERY_ASSISTANT, target_tenant_id="salon-b", registry=registry)


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
