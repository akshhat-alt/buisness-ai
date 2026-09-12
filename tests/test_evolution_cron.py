"""Tests for Phase 11's two proactive cron endpoints:
POST /api/v1/admin/evolution-scan/run (observe -> failure detection ->
proposal -> sandbox evaluation) and
POST /api/v1/admin/evolution-monitor/run (post-promotion monitoring ->
automatic rollback on regression).
"""

from __future__ import annotations

import time


def _log_turns(client, headers, tenant_id, *, total, dissatisfied):
    """Drives real turns through /api/ask so answer_status is genuinely
    'answered' (FakeGenerator's default status), then flips a subset to
    dissatisfied by writing directly to AnalyticsStore — /api/ask itself
    has no way to force shows_dissatisfaction=True deterministically
    without wiring FakeGenerator's dissatisfaction override through the
    abstention path, and these turns must NOT abstain."""
    for i in range(total):
        # "what is this domain for" is the established query in this
        # suite that reliably scores above the evidence-gate confidence
        # threshold against example.com's real ingested content under
        # the word-overlap HashEmbeddingProvider fake (see
        # test_app_e2e.py) — anything less specific abstains instead of
        # answering, which would defeat these tests' whole setup.
        r = client.post(
            f"/api/ask?tenant_id={tenant_id}", json={"session_id": f"s{i}", "query": "what is this domain for"},
            headers=headers,
        )
        assert r.status_code == 200, r.text


def _setup_active_tenant_with_turns(client, owner_session, activate_tenant, services, *, total, dissatisfied):
    headers, tenant_id = owner_session
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    activate_tenant(tenant_id)
    _log_turns(client, headers, tenant_id, total=total, dissatisfied=dissatisfied)
    if dissatisfied:
        with services.analytics_store._lock, services.analytics_store._db() as conn:
            conn.execute(
                "UPDATE turns SET shows_dissatisfaction = 1 WHERE tenant_id = ? AND session_id IN "
                f"({','.join('?' * dissatisfied)})",
                (tenant_id, *[f"s{i}" for i in range(dissatisfied)]),
            )
            conn.commit()
    return headers, tenant_id


def test_evolution_scan_skips_tenant_with_kill_switch_off(client, owner_session, activate_tenant, services, admin_headers):
    headers, tenant_id = _setup_active_tenant_with_turns(
        client, owner_session, activate_tenant, services, total=20, dissatisfied=10,
    )
    r = client.post("/api/v1/admin/evolution-scan/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert any(s["tenant_id"] == tenant_id and "kill switch" in s["reason"] for s in r.json()["skipped"])


def test_evolution_scan_creates_and_sandbox_evaluates_a_proposal(client, owner_session, activate_tenant, services, admin_headers):
    headers, tenant_id = _setup_active_tenant_with_turns(
        client, owner_session, activate_tenant, services, total=20, dissatisfied=10,
    )
    client.post(f"/api/tenant/evolution-toggle?tenant_id={tenant_id}", json={"enabled": True}, headers=headers)

    r = client.post("/api/v1/admin/evolution-scan/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    proposed = [p for p in r.json()["proposed"] if p["tenant_id"] == tenant_id]
    assert len(proposed) == 1
    # FakeGenerator's generate() output doesn't vary with the system
    # prompt, so baseline and candidate answers are identical -> pass.
    assert proposed[0]["verdict"] == "pass"

    proposals = services.evolution_proposals.list_for_tenant(tenant_id)
    assert len(proposals) == 1
    assert proposals[0].status == "pending_owner_review"
    evaluation = services.evolution_evaluations.latest_for_version(tenant_id, proposals[0].candidate_version_id)
    assert evaluation is not None
    assert evaluation.evaluation_type == "sandbox"
    assert evaluation.metrics["sample_size"] > 0


def test_evolution_scan_does_not_duplicate_an_in_flight_proposal(client, owner_session, activate_tenant, services, admin_headers):
    headers, tenant_id = _setup_active_tenant_with_turns(
        client, owner_session, activate_tenant, services, total=20, dissatisfied=10,
    )
    client.post(f"/api/tenant/evolution-toggle?tenant_id={tenant_id}", json={"enabled": True}, headers=headers)

    client.post("/api/v1/admin/evolution-scan/run", headers=admin_headers)
    r2 = client.post("/api/v1/admin/evolution-scan/run", headers=admin_headers)
    assert not any(p["tenant_id"] == tenant_id for p in r2.json()["proposed"])
    assert any(
        s["tenant_id"] == tenant_id and "already awaiting" in s["reason"] for s in r2.json()["skipped"]
    )
    assert len(services.evolution_proposals.list_for_tenant(tenant_id)) == 1


def test_evolution_scan_skips_tenant_without_failure_signal(client, owner_session, activate_tenant, services, admin_headers):
    headers, tenant_id = _setup_active_tenant_with_turns(
        client, owner_session, activate_tenant, services, total=20, dissatisfied=0,
    )
    client.post(f"/api/tenant/evolution-toggle?tenant_id={tenant_id}", json={"enabled": True}, headers=headers)

    r = client.post("/api/v1/admin/evolution-scan/run", headers=admin_headers)
    assert not any(p["tenant_id"] == tenant_id for p in r.json()["proposed"])
    assert any(s["tenant_id"] == tenant_id and s["reason"] == "no failure signal detected" for s in r.json()["skipped"])


def test_evolution_scan_requires_platform_admin(client, owner_session):
    headers, _ = owner_session
    r = client.post("/api/v1/admin/evolution-scan/run", headers=headers)
    assert r.status_code == 403


def test_evolution_monitor_rolls_back_on_regression(client, owner_session, activate_tenant, services, admin_headers):
    headers, tenant_id = owner_session
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    activate_tenant(tenant_id)

    v1 = services.evolution_versions.create(
        tenant_id=tenant_id, config_type="assistant_tone", payload={"tone_instructions": "v1"}, created_by="owner",
    )
    services.evolution_versions.activate(tenant_id, v1.version_id)
    v2 = services.evolution_versions.create(
        tenant_id=tenant_id, config_type="assistant_tone", payload={"tone_instructions": "v2"}, created_by="owner",
        parent_version_id=v1.version_id,
    )
    services.evolution_versions.activate(tenant_id, v2.version_id)
    activated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 48 * 3600))
    with services.evolution_versions._lock, services.evolution_versions._db() as conn:
        conn.execute("UPDATE evolution_versions SET activated_at = ? WHERE version_id = ?", (activated_at, v2.version_id))
        conn.commit()

    # Baseline window (before v2's activation): calm.
    for i in range(10):
        services.analytics_store.log_turn(
            tenant_id=tenant_id, session_id=f"pre{i}", query=f"pre {i}", answer_status="answered", shows_dissatisfaction=False,
        )
    with services.analytics_store._lock, services.analytics_store._db() as conn:
        pre_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 72 * 3600))
        conn.execute("UPDATE turns SET created_at = ? WHERE session_id LIKE 'pre%'", (pre_at,))
        conn.commit()
    # Post-activation window: regressed.
    for i in range(10):
        services.analytics_store.log_turn(
            tenant_id=tenant_id, session_id=f"post{i}", query=f"post {i}", answer_status="answered", shows_dissatisfaction=True,
        )

    r = client.post("/api/v1/admin/evolution-monitor/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    rolled_back = [x for x in r.json()["rolled_back"] if x["tenant_id"] == tenant_id]
    assert len(rolled_back) == 1

    assert services.evolution_versions.get_active(tenant_id, "assistant_tone").version_id == v1.version_id
    entries = services.audit_log.list_for_tenant(tenant_id, action="evolution_version_rolled_back_auto")
    assert len(entries) == 1


def test_evolution_monitor_requires_platform_admin(client, owner_session):
    headers, _ = owner_session
    r = client.post("/api/v1/admin/evolution-monitor/run", headers=headers)
    assert r.status_code == 403


def test_evolution_monitor_isolates_a_broken_tenant_from_the_rest(client, owner_session, activate_tenant, services, admin_headers):
    """A live smoke test surfaced this: if one tenant's stored
    parent_version_id somehow doesn't resolve to a real row (never
    possible through the normal proposal->approve flow, but a real
    defensive requirement for a cron that runs unattended), the rollback
    for THAT tenant must fail gracefully — never take down the whole
    cron run before every other tenant gets checked."""
    headers, tenant_id = owner_session
    client.post(f"/api/knowledge/website?tenant_id={tenant_id}", json={"url": "https://example.com"}, headers=headers)
    activate_tenant(tenant_id)

    v2 = services.evolution_versions.create(
        tenant_id=tenant_id, config_type="assistant_tone", payload={"tone_instructions": "v2"}, created_by="owner",
        parent_version_id="does_not_exist",
    )
    services.evolution_versions.activate(tenant_id, v2.version_id)
    activated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 48 * 3600))
    with services.evolution_versions._lock, services.evolution_versions._db() as conn:
        conn.execute("UPDATE evolution_versions SET activated_at = ? WHERE version_id = ?", (activated_at, v2.version_id))
        conn.commit()
    for i in range(10):
        services.analytics_store.log_turn(
            tenant_id=tenant_id, session_id=f"post{i}", query=f"post {i}", answer_status="answered", shows_dissatisfaction=True,
        )
    for i in range(10):
        services.analytics_store.log_turn(
            tenant_id=tenant_id, session_id=f"pre{i}", query=f"pre {i}", answer_status="answered", shows_dissatisfaction=False,
        )
    with services.analytics_store._lock, services.analytics_store._db() as conn:
        pre_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 72 * 3600))
        conn.execute("UPDATE turns SET created_at = ? WHERE session_id LIKE 'pre%'", (pre_at,))
        conn.commit()

    r = client.post("/api/v1/admin/evolution-monitor/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert any(s["tenant_id"] == tenant_id and s["reason"] == "rollback_failed" for s in r.json()["skipped"])
    # The version is still active — a failed rollback must not leave it
    # in a half-changed state.
    assert services.evolution_versions.get_active(tenant_id, "assistant_tone").version_id == v2.version_id
