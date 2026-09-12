"""Tests for Self-Evolution Infrastructure (Phase 11) — the pure
store/pipeline logic in evolution.py, exercised against real store
instances (temp SQLite, no mocks), matching this codebase's established
testing convention.
"""

from __future__ import annotations

import time

import pytest

from business_ai.analytics import AnalyticsStore
from business_ai.evolution import (
    EvolutionEvaluationStore,
    EvolutionProposalStore,
    EvolutionVersionStore,
    UnsafeBehaviorPayloadError,
    detect_failure_signal,
    generate_behavior_proposal,
    run_monitoring_check,
    run_sandbox_evaluation,
    validate_behavior_payload,
)
from business_ai.generation import LLMResponseDraft
from business_ai.ingestion import ingest_text
from business_ai.retrieval import HashEmbeddingProvider, VectorStore

TENANT = "salon-a"


class _ToneSensitiveFakeGenerator:
    """Unlike conftest.FakeGenerator, this fake's output genuinely
    depends on system_prompt content — needed to exercise sandbox
    evaluation's regression-detection path deterministically, since the
    shared FakeGenerator always returns the same answer regardless of
    tone_instructions (by design, for every OTHER test in this suite)."""

    def __init__(self, *, regress_on_marker: str | None = None) -> None:
        self.regress_on_marker = regress_on_marker

    def generate(self, *, system_prompt: str, user_prompt: str) -> LLMResponseDraft:
        import re

        match = re.search(r'evidence_passage id="([^"]+)"', user_prompt)
        seg_id = match.group(1) if match else "seg_unknown"
        if self.regress_on_marker and self.regress_on_marker in system_prompt:
            return LLMResponseDraft(status="insufficient_evidence", answer_text="", cited_segment_ids=[])
        return LLMResponseDraft(status="answered", answer_text="Here you go.", cited_segment_ids=[seg_id])


@pytest.fixture()
def stores(tmp_path):
    return {
        "versions": EvolutionVersionStore(tmp_path / "evolution_versions.db"),
        "proposals": EvolutionProposalStore(tmp_path / "evolution_proposals.db"),
        "evaluations": EvolutionEvaluationStore(tmp_path / "evolution_evaluations.db"),
        "analytics": AnalyticsStore(tmp_path / "analytics.db"),
    }


def _log_turns(analytics, *, total: int, dissatisfied: int, tenant_id: str = TENANT) -> None:
    for i in range(total):
        analytics.log_turn(
            tenant_id=tenant_id, session_id=f"s{i}", query=f"question {i}", answer_status="answered",
            shows_dissatisfaction=i < dissatisfied,
        )


# ------------------------------------------------------------------ safety boundary


def test_validate_behavior_payload_rejects_unknown_config_type():
    with pytest.raises(UnsafeBehaviorPayloadError):
        validate_behavior_payload("python_code", {"tone_instructions": "be nice"})


def test_validate_behavior_payload_rejects_unknown_keys():
    with pytest.raises(UnsafeBehaviorPayloadError):
        validate_behavior_payload("assistant_tone", {"tone_instructions": "be nice", "sql_query": "DROP TABLE x"})


def test_validate_behavior_payload_rejects_oversized_text():
    with pytest.raises(UnsafeBehaviorPayloadError):
        validate_behavior_payload("assistant_tone", {"tone_instructions": "x" * 501})


@pytest.mark.parametrize(
    "banned",
    ["```python\nimport os\n```", "please DROP TABLE tenants", "run rm -rf / now", "<script>alert(1)</script>"],
)
def test_validate_behavior_payload_rejects_code_and_sql_lookalikes(banned):
    with pytest.raises(UnsafeBehaviorPayloadError):
        validate_behavior_payload("assistant_tone", {"tone_instructions": banned})


def test_validate_behavior_payload_accepts_clean_text():
    result = validate_behavior_payload("assistant_tone", {"tone_instructions": "  Be warm and concise.  "})
    assert result == {"tone_instructions": "Be warm and concise."}


def test_evolution_version_store_rejects_unsafe_payload_even_direct(stores):
    with pytest.raises(UnsafeBehaviorPayloadError):
        stores["versions"].create(
            tenant_id=TENANT, config_type="assistant_tone", payload={"tone_instructions": "DROP TABLE leads"},
            created_by="owner",
        )


# ------------------------------------------------------------------ version lifecycle


def test_activate_supersedes_prior_active_version(stores):
    versions = stores["versions"]
    v1 = versions.create(tenant_id=TENANT, config_type="assistant_tone", payload={"tone_instructions": "v1"}, created_by="owner")
    versions.activate(TENANT, v1.version_id)
    v2 = versions.create(
        tenant_id=TENANT, config_type="assistant_tone", payload={"tone_instructions": "v2"}, created_by="owner",
        parent_version_id=v1.version_id,
    )
    versions.activate(TENANT, v2.version_id)

    active = versions.get_active(TENANT, "assistant_tone")
    assert active.version_id == v2.version_id
    superseded = versions.get(TENANT, v1.version_id)
    assert superseded.status == "superseded"
    assert superseded.deactivated_at is not None


def test_rollback_reactivates_prior_version_and_preserves_history(stores):
    versions = stores["versions"]
    v1 = versions.create(tenant_id=TENANT, config_type="assistant_tone", payload={"tone_instructions": "v1"}, created_by="owner")
    versions.activate(TENANT, v1.version_id)
    v2 = versions.create(
        tenant_id=TENANT, config_type="assistant_tone", payload={"tone_instructions": "v2"}, created_by="owner",
        parent_version_id=v1.version_id,
    )
    versions.activate(TENANT, v2.version_id)

    restored = versions.rollback_to(TENANT, v1.version_id)
    assert restored.version_id == v1.version_id
    assert versions.get_active(TENANT, "assistant_tone").version_id == v1.version_id
    rolled_back_v2 = versions.get(TENANT, v2.version_id)
    assert rolled_back_v2.status == "rolled_back"
    # full lineage survives — nothing is deleted by activation/rollback
    all_versions = versions.list_for_tenant(TENANT)
    assert {v.version_id for v in all_versions} == {v1.version_id, v2.version_id}


def test_get_active_returns_none_when_nothing_activated(stores):
    assert stores["versions"].get_active(TENANT, "assistant_tone") is None


def test_delete_for_tenant_removes_all_rows(stores):
    versions, proposals, evaluations = stores["versions"], stores["proposals"], stores["evaluations"]
    v1 = versions.create(tenant_id=TENANT, config_type="assistant_tone", payload={"tone_instructions": "v1"}, created_by="owner")
    proposals.create(tenant_id=TENANT, trigger_reason="manual", candidate_version_id=v1.version_id)
    evaluations.record(tenant_id=TENANT, version_id=v1.version_id, evaluation_type="sandbox", metrics={}, verdict="pass")

    assert versions.delete_for_tenant(TENANT) == 1
    assert proposals.delete_for_tenant(TENANT) == 1
    assert evaluations.delete_for_tenant(TENANT) == 1
    assert versions.list_for_tenant(TENANT) == []


# ------------------------------------------------------------------ failure detection / proposal generation


def test_detect_failure_signal_returns_none_below_sample_threshold(stores):
    _log_turns(stores["analytics"], total=3, dissatisfied=3)
    assert detect_failure_signal(TENANT, analytics_store=stores["analytics"]) is None


def test_detect_failure_signal_returns_none_below_rate_threshold(stores):
    _log_turns(stores["analytics"], total=20, dissatisfied=1)  # 5% << 20% threshold
    assert detect_failure_signal(TENANT, analytics_store=stores["analytics"]) is None


def test_detect_failure_signal_fires_above_threshold(stores):
    _log_turns(stores["analytics"], total=20, dissatisfied=6)  # 30% >= 20% threshold
    signal = detect_failure_signal(TENANT, analytics_store=stores["analytics"])
    assert signal is not None
    assert signal.reason == "elevated_dissatisfaction_rate"
    assert signal.sample_size == 20
    assert signal.suggested_tone_instructions


def test_generate_behavior_proposal_creates_draft_version_and_proposal(stores):
    _log_turns(stores["analytics"], total=20, dissatisfied=6)
    proposal = generate_behavior_proposal(
        TENANT, analytics_store=stores["analytics"], evolution_version_store=stores["versions"],
        evolution_proposal_store=stores["proposals"],
    )
    assert proposal is not None
    assert proposal.status == "pending_sandbox"
    candidate = stores["versions"].get(TENANT, proposal.candidate_version_id)
    assert candidate.status == "draft"
    assert candidate.created_by == "self_evolution_engine"
    assert candidate.parent_version_id is None  # no prior active version yet


def test_generate_behavior_proposal_returns_none_without_failure_signal(stores):
    _log_turns(stores["analytics"], total=20, dissatisfied=0)
    proposal = generate_behavior_proposal(
        TENANT, analytics_store=stores["analytics"], evolution_version_store=stores["versions"],
        evolution_proposal_store=stores["proposals"],
    )
    assert proposal is None


# ------------------------------------------------------------------ monitoring + automatic rollback


def _make_active_version(versions, *, activated_hours_ago: float, parent_id: str | None = "parent_v"):
    v = versions.create(
        tenant_id=TENANT, config_type="assistant_tone", payload={"tone_instructions": "x"}, created_by="owner",
        parent_version_id=parent_id,
    )
    versions.activate(TENANT, v.version_id)
    if activated_hours_ago:
        activated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - activated_hours_ago * 3600))
        with versions._lock, versions._db() as conn:  # test-only direct backdating of activated_at
            conn.execute("UPDATE evolution_versions SET activated_at = ? WHERE version_id = ?", (activated_at, v.version_id))
            conn.commit()
    return versions.get(TENANT, v.version_id)


def test_monitoring_skips_version_with_no_parent(stores):
    v = _make_active_version(stores["versions"], activated_hours_ago=48, parent_id=None)
    result = run_monitoring_check(TENANT, v, analytics_store=stores["analytics"])
    assert result["action"] == "skipped"
    assert result["reason"] == "no_prior_version_to_compare_or_roll_back_to"


def test_monitoring_skips_too_recent_activation(stores):
    v = _make_active_version(stores["versions"], activated_hours_ago=1)
    result = run_monitoring_check(TENANT, v, analytics_store=stores["analytics"])
    assert result["action"] == "skipped"
    assert result["reason"] == "too_recent"


def test_monitoring_skips_insufficient_data(stores):
    v = _make_active_version(stores["versions"], activated_hours_ago=48)
    result = run_monitoring_check(TENANT, v, analytics_store=stores["analytics"])
    assert result["action"] == "skipped"
    assert result["reason"] == "insufficient_data"


def test_monitoring_detects_regression(stores):
    analytics = stores["analytics"]
    v = _make_active_version(stores["versions"], activated_hours_ago=48)
    # Baseline window (before activation): low dissatisfaction.
    for i in range(10):
        analytics.log_turn(tenant_id=TENANT, session_id=f"pre{i}", query=f"pre {i}", answer_status="answered", shows_dissatisfaction=False)
    # Backdate those rows to before activation.
    with analytics._lock, analytics._db() as conn:
        pre_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 72 * 3600))
        conn.execute("UPDATE turns SET created_at = ? WHERE session_id LIKE 'pre%'", (pre_at,))
        conn.commit()
    # Post-activation window: high dissatisfaction.
    for i in range(10):
        analytics.log_turn(tenant_id=TENANT, session_id=f"post{i}", query=f"post {i}", answer_status="answered", shows_dissatisfaction=True)

    result = run_monitoring_check(TENANT, v, analytics_store=analytics)
    assert result["action"] == "regression"
    assert result["metrics"]["baseline_rate"] == 0.0
    assert result["metrics"]["post_rate"] == 1.0


def test_monitoring_reports_ok_without_regression(stores):
    analytics = stores["analytics"]
    v = _make_active_version(stores["versions"], activated_hours_ago=48)
    for i in range(10):
        analytics.log_turn(tenant_id=TENANT, session_id=f"pre{i}", query=f"pre {i}", answer_status="answered", shows_dissatisfaction=False)
    with analytics._lock, analytics._db() as conn:
        pre_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 72 * 3600))
        conn.execute("UPDATE turns SET created_at = ? WHERE session_id LIKE 'pre%'", (pre_at,))
        conn.commit()
    for i in range(10):
        analytics.log_turn(tenant_id=TENANT, session_id=f"post{i}", query=f"post {i}", answer_status="answered", shows_dissatisfaction=False)

    result = run_monitoring_check(TENANT, v, analytics_store=analytics)
    assert result["action"] == "ok"


# ------------------------------------------------------------------ sandbox evaluation


@pytest.fixture()
def sandbox_setup(tmp_path):
    vector_store = VectorStore(tmp_path / "chroma")
    embeddings = HashEmbeddingProvider()
    ingest_text(
        text="We are open 9am to 6pm Monday through Saturday. We offer haircuts and coloring.",
        tenant_id=TENANT, source_id="src1", source_label="FAQ", source_url=None,
        embeddings=embeddings, store=vector_store,
    )
    analytics = AnalyticsStore(tmp_path / "analytics.db")
    # High word-overlap with the ingested text so the HashEmbeddingProvider
    # fake scores this well above the evidence-gate confidence threshold
    # (a vague paraphrase like "what time do you open" scores too low
    # under this deterministic word-overlap fake and abstains instead).
    analytics.log_turn(tenant_id=TENANT, session_id="s1", query="we offer haircuts and coloring", answer_status="answered")
    versions = EvolutionVersionStore(tmp_path / "evolution_versions.db")
    evaluations = EvolutionEvaluationStore(tmp_path / "evolution_evaluations.db")
    proposals = EvolutionProposalStore(tmp_path / "evolution_proposals.db")
    return {
        "vector_store": vector_store, "embeddings": embeddings, "analytics": analytics,
        "versions": versions, "evaluations": evaluations, "proposals": proposals,
    }


def _make_proposal(sandbox_setup, tone_text: str):
    versions, proposals = sandbox_setup["versions"], sandbox_setup["proposals"]
    candidate = versions.create(
        tenant_id=TENANT, config_type="assistant_tone", payload={"tone_instructions": tone_text},
        created_by="self_evolution_engine",
    )
    return proposals.create(tenant_id=TENANT, trigger_reason="test", candidate_version_id=candidate.version_id)


def test_sandbox_evaluation_passes_when_no_regression(sandbox_setup):
    proposal = _make_proposal(sandbox_setup, "Be warm and concise.")
    evaluation = run_sandbox_evaluation(
        TENANT, proposal, evolution_version_store=sandbox_setup["versions"],
        evolution_evaluation_store=sandbox_setup["evaluations"], analytics_store=sandbox_setup["analytics"],
        vector_store=sandbox_setup["vector_store"], embeddings=sandbox_setup["embeddings"],
        generator=_ToneSensitiveFakeGenerator(), business_name="Priya Salon", assistant_name="Assistant",
    )
    assert evaluation.verdict == "pass"
    assert evaluation.metrics["sample_size"] > 0
    assert evaluation.metrics["regressions"] == 0
    candidate = sandbox_setup["versions"].get(TENANT, proposal.candidate_version_id)
    assert candidate.status == "shadow_tested_pass"


def test_sandbox_evaluation_fails_on_detected_regression(sandbox_setup):
    tone_text = "Be extremely terse."
    proposal = _make_proposal(sandbox_setup, tone_text)
    evaluation = run_sandbox_evaluation(
        TENANT, proposal, evolution_version_store=sandbox_setup["versions"],
        evolution_evaluation_store=sandbox_setup["evaluations"], analytics_store=sandbox_setup["analytics"],
        vector_store=sandbox_setup["vector_store"], embeddings=sandbox_setup["embeddings"],
        generator=_ToneSensitiveFakeGenerator(regress_on_marker=tone_text), business_name="Priya Salon",
        assistant_name="Assistant",
    )
    assert evaluation.verdict == "fail"
    assert evaluation.metrics["regressions"] > 0
    candidate = sandbox_setup["versions"].get(TENANT, proposal.candidate_version_id)
    assert candidate.status == "shadow_tested_fail"


def test_sandbox_evaluation_reports_insufficient_data_with_no_history(sandbox_setup):
    # A fresh AnalyticsStore with zero logged turns has nothing for
    # list_recent_answered_queries to return.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        fresh_analytics = AnalyticsStore(Path(d) / "empty_analytics.db")
        proposal = _make_proposal(sandbox_setup, "Be warm.")
        evaluation = run_sandbox_evaluation(
            TENANT, proposal, evolution_version_store=sandbox_setup["versions"],
            evolution_evaluation_store=sandbox_setup["evaluations"], analytics_store=fresh_analytics,
            vector_store=sandbox_setup["vector_store"], embeddings=sandbox_setup["embeddings"],
            generator=_ToneSensitiveFakeGenerator(), business_name="Priya Salon", assistant_name="Assistant",
        )
    assert evaluation.verdict == "insufficient_data"
