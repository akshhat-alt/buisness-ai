"""HTTP-level tests for Phase 11's Self-Evolution owner controls: RBAC
(owner-only, no manager/staff access at all), tenant isolation, gated
promotion (approval refused until sandbox evaluation passes), manual
rollback, and the kill switch.
"""

from __future__ import annotations

from business_ai.auth import Principal, create_access_token


def _create_pending_review_proposal(services, tenant_id: str):
    """Bypasses the scan cron to set up a proposal already at
    pending_owner_review, for tests that only care about the
    approve/reject/rollback routes themselves."""
    candidate = services.evolution_versions.create(
        tenant_id=tenant_id, config_type="assistant_tone", payload={"tone_instructions": "Be more concise."},
        created_by="self_evolution_engine", rationale="test setup",
    )
    services.evolution_versions.set_status(tenant_id, candidate.version_id, "shadow_tested_pass")
    proposal = services.evolution_proposals.create(
        tenant_id=tenant_id, trigger_reason="elevated_dissatisfaction_rate", candidate_version_id=candidate.version_id,
        status="pending_owner_review",
    )
    return proposal, candidate


def test_evolution_routes_require_owner_not_manager_or_staff(client, owner_session, services):
    headers, tenant_id = owner_session
    staff_token = create_access_token(Principal.staff("staff_x", tenant_id), services.settings)
    manager_token = create_access_token(Principal.manager("mgr_1", tenant_id), services.settings)

    for role_headers in ({"Authorization": f"Bearer {staff_token}"}, {"Authorization": f"Bearer {manager_token}"}):
        r = client.get(f"/api/evolution/versions?tenant_id={tenant_id}", headers=role_headers)
        assert r.status_code == 403
        r = client.get(f"/api/evolution/proposals?tenant_id={tenant_id}", headers=role_headers)
        assert r.status_code == 403

    r = client.get(f"/api/evolution/versions?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text


def test_evolution_routes_are_tenant_isolated(client, owner_session):
    headers_a, tenant_a = owner_session
    signup_b = client.post(
        "/api/auth/signup",
        json={"email": "evo-other@example.com", "password": "secret123", "name": "Bob", "business_name": "Other Biz"},
    )
    headers_b = {"Authorization": f"Bearer {signup_b.json()['access_token']}"}

    r = client.get(f"/api/evolution/versions?tenant_id={tenant_a}", headers=headers_b)
    assert r.status_code == 403


def test_approve_is_refused_before_sandbox_evaluation_passes(client, owner_session, services):
    headers, tenant_id = owner_session
    candidate = services.evolution_versions.create(
        tenant_id=tenant_id, config_type="assistant_tone", payload={"tone_instructions": "Be nicer."},
        created_by="self_evolution_engine",
    )
    proposal = services.evolution_proposals.create(
        tenant_id=tenant_id, trigger_reason="elevated_dissatisfaction_rate", candidate_version_id=candidate.version_id,
        status="pending_sandbox",
    )
    r = client.post(f"/api/evolution/proposals/{proposal.proposal_id}/approve?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 400
    assert services.evolution_versions.get_active(tenant_id, "assistant_tone") is None


def test_approve_activates_candidate_version_and_writes_audit_log(client, owner_session, services):
    headers, tenant_id = owner_session
    proposal, candidate = _create_pending_review_proposal(services, tenant_id)

    r = client.post(f"/api/evolution/proposals/{proposal.proposal_id}/approve?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["active_version_id"] == candidate.version_id

    active = services.evolution_versions.get_active(tenant_id, "assistant_tone")
    assert active.version_id == candidate.version_id
    entries = services.audit_log.list_for_tenant(tenant_id, action="evolution_version_approved")
    assert len(entries) == 1


def test_reject_marks_proposal_and_version_rejected(client, owner_session, services):
    headers, tenant_id = owner_session
    proposal, candidate = _create_pending_review_proposal(services, tenant_id)

    r = client.post(f"/api/evolution/proposals/{proposal.proposal_id}/reject?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert services.evolution_proposals.get(tenant_id, proposal.proposal_id).status == "rejected"
    assert services.evolution_versions.get(tenant_id, candidate.version_id).status == "rejected"
    assert services.evolution_versions.get_active(tenant_id, "assistant_tone") is None


def test_manual_rollback_restores_prior_version(client, owner_session, services):
    headers, tenant_id = owner_session
    v1 = services.evolution_versions.create(
        tenant_id=tenant_id, config_type="assistant_tone", payload={"tone_instructions": "v1"}, created_by="owner",
    )
    services.evolution_versions.activate(tenant_id, v1.version_id)
    v2 = services.evolution_versions.create(
        tenant_id=tenant_id, config_type="assistant_tone", payload={"tone_instructions": "v2"}, created_by="owner",
        parent_version_id=v1.version_id,
    )
    services.evolution_versions.activate(tenant_id, v2.version_id)

    r = client.post(f"/api/evolution/versions/{v1.version_id}/rollback?tenant_id={tenant_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["active_version_id"] == v1.version_id
    assert services.evolution_versions.get_active(tenant_id, "assistant_tone").version_id == v1.version_id
    entries = services.audit_log.list_for_tenant(tenant_id, action="evolution_version_rolled_back_manual")
    assert len(entries) == 1


def test_evolution_kill_switch_defaults_off_and_can_be_toggled(client, owner_session, services):
    headers, tenant_id = owner_session
    assert services.tenant_registry.get_config(tenant_id).evolution_enabled is False

    r = client.post(f"/api/tenant/evolution-toggle?tenant_id={tenant_id}", json={"enabled": True}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["evolution_enabled"] is True
    assert services.tenant_registry.get_config(tenant_id).evolution_enabled is True

    r = client.post(f"/api/tenant/evolution-toggle?tenant_id={tenant_id}", json={"enabled": False}, headers=headers)
    assert r.json()["evolution_enabled"] is False


def test_active_tone_version_reaches_the_real_answer_pipeline(client, owner_session, activate_tenant, services):
    """End-to-end proof that approving a version actually changes what
    build_system_prompt receives for real customer traffic — not just
    that the version row flips to 'active' in isolation."""
    headers, tenant_id = owner_session
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    activate_tenant(tenant_id)

    proposal, candidate = _create_pending_review_proposal(services, tenant_id)
    client.post(f"/api/evolution/proposals/{proposal.proposal_id}/approve?tenant_id={tenant_id}", headers=headers)

    captured = {}
    from business_ai.routers import admin_bot as admin_bot_module

    original = admin_bot_module.build_system_prompt

    def _spy(*args, **kwargs):
        captured["tone_instructions"] = kwargs.get("tone_instructions")
        return original(*args, **kwargs)

    admin_bot_module.build_system_prompt = _spy
    try:
        r = client.post(f"/api/ask?tenant_id={tenant_id}", json={"session_id": "s1", "query": "what is this domain for"})
        assert r.status_code == 200, r.text
    finally:
        admin_bot_module.build_system_prompt = original

    assert captured.get("tone_instructions") == "Be more concise."
