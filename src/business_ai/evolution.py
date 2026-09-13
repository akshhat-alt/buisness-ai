"""Self-Evolution Infrastructure (Phase 11): a versioned, owner-gated
behavior/configuration store plus the observe -> failure detection ->
proposal -> sandbox evaluation -> owner approval -> monitoring ->
automatic rollback pipeline.

HARD SAFETY BOUNDARY, enforced here at the data layer, not just by
convention or by which routes exist: self-evolution may only ever create
and activate versions of a small, explicitly whitelisted, plain-text
BEHAVIOR CONFIG (today: the customer assistant's tone-guidance string).
It can NEVER touch Python source, SQL schema, infrastructure, secrets,
money/payment logic, or a tenant's account/activation status — there is
no code path anywhere in this module that writes a file, runs a
migration, calls payments.py, or calls TenantRegistry's status-changing
methods. `validate_behavior_payload` below is the single choke point
every version write passes through, and it rejects anything outside the
whitelisted keys/bounds/lexical safety filter, regardless of whether the
caller is an owner, the proposal engine, or a test.

Mirrors dependency_graph.py's own shape: derived computation over
existing stores (AnalyticsStore) for detection, plus three small new
SqliteStore-backed tables for the version/proposal/evaluation lineage
this phase genuinely needs to record (a version's payload and status
history is not something any existing store can derive)."""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from business_ai.constants import (
    EVOLUTION_FAILURE_DISSATISFACTION_RATE_THRESHOLD,
    EVOLUTION_LOOKBACK_HOURS,
    EVOLUTION_MIN_SAMPLE_FOR_DETECTION,
    EVOLUTION_MONITORING_MIN_HOURS_ACTIVE,
    EVOLUTION_MONITORING_MIN_SAMPLE,
    EVOLUTION_MONITORING_REGRESSION_DELTA,
    EVOLUTION_SANDBOX_SAMPLE_SIZE,
)
from business_ai.storage import SqliteStore

# ==============================================================================
# The whitelisted behavior config itself
# ==============================================================================

# The ONLY config_type this phase understands. Adding a second one later
# (e.g. a bounded numeric scheduling window) is a natural extension of
# this same pattern — but it must be added here, explicitly, with its own
# validation; there is deliberately no generic "any tenant setting" path.
CONFIG_TYPES = frozenset({"assistant_tone"})

MAX_TONE_INSTRUCTIONS_LENGTH = 500

# Lexical defense-in-depth: this text is never executed, only ever
# interpolated into an LLM system prompt as plain guidance (see
# generation.build_system_prompt) — but rejecting code/SQL/shell
# look-alikes at the point of storage means a malformed or malicious
# proposal (self-generated or, if this were ever exposed more broadly,
# owner-submitted) can never even be saved, not just "never executed."
_BANNED_SUBSTRINGS = (
    "```", "<script", "import ", "os.system", "subprocess", "eval(", "exec(",
    "drop table", "delete from", "insert into", "update ", "alter table",
    "rm -rf", "curl ", "wget ", "__import__", "javascript:",
)


class UnsafeBehaviorPayloadError(ValueError):
    pass


def validate_behavior_payload(config_type: str, payload: dict) -> dict:
    """The single choke point for every version write. Raises
    UnsafeBehaviorPayloadError on anything outside the whitelist, bounds,
    or lexical safety filter — called from EvolutionVersionStore.create
    itself, so it applies no matter which caller (owner-facing route,
    proposal engine, or a test) is writing."""
    if config_type not in CONFIG_TYPES:
        raise UnsafeBehaviorPayloadError(f"Unknown config_type: {config_type!r}.")
    if config_type == "assistant_tone":
        allowed_keys = {"tone_instructions"}
        unknown = set(payload) - allowed_keys
        if unknown:
            raise UnsafeBehaviorPayloadError(f"Unknown assistant_tone key(s): {sorted(unknown)}.")
        text = str(payload.get("tone_instructions", "")).strip()
        if len(text) > MAX_TONE_INSTRUCTIONS_LENGTH:
            raise UnsafeBehaviorPayloadError(
                f"tone_instructions exceeds {MAX_TONE_INSTRUCTIONS_LENGTH} characters."
            )
        lowered = text.lower()
        for pattern in _BANNED_SUBSTRINGS:
            if pattern in lowered:
                raise UnsafeBehaviorPayloadError(f"tone_instructions contains a disallowed pattern: {pattern!r}.")
        return {"tone_instructions": text}
    raise UnsafeBehaviorPayloadError(f"Unknown config_type: {config_type!r}.")  # pragma: no cover - unreachable given CONFIG_TYPES


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _iso_minus_hours(value: str, hours: float) -> str:
    dt = _parse_iso(value) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hours_since(value: str, *, now_iso: str | None = None) -> float:
    now_dt = _parse_iso(now_iso) if now_iso else datetime.now(timezone.utc)
    delta = now_dt - _parse_iso(value)
    return delta.total_seconds() / 3600.0


# ==============================================================================
# EvolutionVersionStore — versioned, immutable-once-created behavior config
# ==============================================================================


class EvolutionVersion(BaseModel):
    version_id: str
    tenant_id: str
    config_type: str
    payload: dict
    status: str  # draft | shadow_tested_pass | shadow_tested_fail | active | superseded | rejected | rolled_back
    parent_version_id: str | None = None
    created_by: str  # "owner" | "self_evolution_engine"
    rationale: str = ""
    created_at: str
    activated_at: str | None = None
    deactivated_at: str | None = None


class EvolutionVersionStore(SqliteStore):
    def __init__(self, db_path: Path | str = "data/evolution_versions.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evolution_versions (
                    version_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    config_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    parent_version_id TEXT,
                    created_by TEXT NOT NULL,
                    rationale TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    deactivated_at TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evolution_versions_tenant ON evolution_versions(tenant_id)")
            conn.commit()

    def _row_to_version(self, row) -> EvolutionVersion:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json") or "{}")
        return EvolutionVersion(**data)

    def create(
        self, *, tenant_id: str, config_type: str, payload: dict, created_by: str,
        rationale: str = "", parent_version_id: str | None = None, status: str = "draft",
    ) -> EvolutionVersion:
        safe_payload = validate_behavior_payload(config_type, payload)
        version = EvolutionVersion(
            version_id=f"evver_{secrets.token_hex(8)}", tenant_id=tenant_id, config_type=config_type,
            payload=safe_payload, status=status, parent_version_id=parent_version_id, created_by=created_by,
            rationale=rationale, created_at=_now_iso(),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO evolution_versions
                    (version_id, tenant_id, config_type, payload_json, status, parent_version_id,
                     created_by, rationale, created_at, activated_at, deactivated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.version_id, version.tenant_id, version.config_type, json.dumps(version.payload),
                    version.status, version.parent_version_id, version.created_by, version.rationale,
                    version.created_at, version.activated_at, version.deactivated_at,
                ),
            )
            conn.commit()
        return version

    def get(self, tenant_id: str, version_id: str) -> EvolutionVersion | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM evolution_versions WHERE tenant_id = ? AND version_id = ?", (tenant_id, version_id),
            ).fetchone()
            return self._row_to_version(row) if row else None

    def list_for_tenant(self, tenant_id: str, *, config_type: str | None = None) -> list[EvolutionVersion]:
        with self._lock, self._db() as conn:
            if config_type:
                rows = conn.execute(
                    "SELECT * FROM evolution_versions WHERE tenant_id = ? AND config_type = ? ORDER BY created_at DESC",
                    (tenant_id, config_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM evolution_versions WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,),
                ).fetchall()
            return [self._row_to_version(r) for r in rows]

    def get_active(self, tenant_id: str, config_type: str) -> EvolutionVersion | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM evolution_versions WHERE tenant_id = ? AND config_type = ? AND status = 'active' "
                "ORDER BY activated_at DESC LIMIT 1",
                (tenant_id, config_type),
            ).fetchone()
            return self._row_to_version(row) if row else None

    def set_status(self, tenant_id: str, version_id: str, status: str) -> EvolutionVersion | None:
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE evolution_versions SET status = ? WHERE tenant_id = ? AND version_id = ?",
                (status, tenant_id, version_id),
            )
            conn.commit()
        return self.get(tenant_id, version_id)

    def activate(self, tenant_id: str, version_id: str) -> EvolutionVersion:
        """Promotes `version_id` to active, superseding whatever was
        previously active for the SAME config_type (if anything). This is
        the only function anywhere in this codebase that makes a
        self-evolution version live for real customer traffic."""
        version = self.get(tenant_id, version_id)
        if version is None:
            raise ValueError(f"Unknown version_id {version_id!r} for tenant {tenant_id!r}.")
        now = _now_iso()
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE evolution_versions SET status = 'superseded', deactivated_at = ? "
                "WHERE tenant_id = ? AND config_type = ? AND status = 'active' AND version_id != ?",
                (now, tenant_id, version.config_type, version_id),
            )
            conn.execute(
                "UPDATE evolution_versions SET status = 'active', activated_at = ?, deactivated_at = NULL "
                "WHERE tenant_id = ? AND version_id = ?",
                (now, tenant_id, version_id),
            )
            conn.commit()
        return self.get(tenant_id, version_id)  # type: ignore[return-value]

    def rollback_to(self, tenant_id: str, target_version_id: str) -> EvolutionVersion:
        """Manual OR automatic rollback: reactivates a prior version
        (already-known-good, since it was live before), marking whatever
        is currently active as rolled_back. Full history survives — the
        superseded/rolled_back version rows are never deleted."""
        target = self.get(tenant_id, target_version_id)
        if target is None:
            raise ValueError(f"Unknown version_id {target_version_id!r} for tenant {tenant_id!r}.")
        now = _now_iso()
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE evolution_versions SET status = 'rolled_back', deactivated_at = ? "
                "WHERE tenant_id = ? AND config_type = ? AND status = 'active' AND version_id != ?",
                (now, tenant_id, target.config_type, target_version_id),
            )
            conn.execute(
                "UPDATE evolution_versions SET status = 'active', activated_at = ?, deactivated_at = NULL "
                "WHERE tenant_id = ? AND version_id = ?",
                (now, tenant_id, target_version_id),
            )
            conn.commit()
        return self.get(tenant_id, target_version_id)  # type: ignore[return-value]

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM evolution_versions WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount


# ==============================================================================
# EvolutionProposalStore — the owner-facing "should we adopt this?" record
# ==============================================================================


class EvolutionProposal(BaseModel):
    proposal_id: str
    tenant_id: str
    trigger_reason: str
    candidate_version_id: str
    status: str  # pending_sandbox | pending_owner_review | sandbox_failed | approved | rejected
    created_at: str
    decided_at: str | None = None
    decided_by: str | None = None


class EvolutionProposalStore(SqliteStore):
    def __init__(self, db_path: Path | str = "data/evolution_proposals.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evolution_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    trigger_reason TEXT NOT NULL,
                    candidate_version_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evolution_proposals_tenant ON evolution_proposals(tenant_id)")
            conn.commit()

    def create(self, *, tenant_id: str, trigger_reason: str, candidate_version_id: str, status: str = "pending_sandbox") -> EvolutionProposal:
        proposal = EvolutionProposal(
            proposal_id=f"evprop_{secrets.token_hex(8)}", tenant_id=tenant_id, trigger_reason=trigger_reason,
            candidate_version_id=candidate_version_id, status=status, created_at=_now_iso(),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO evolution_proposals "
                "(proposal_id, tenant_id, trigger_reason, candidate_version_id, status, created_at, decided_at, decided_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal.proposal_id, proposal.tenant_id, proposal.trigger_reason, proposal.candidate_version_id,
                    proposal.status, proposal.created_at, proposal.decided_at, proposal.decided_by,
                ),
            )
            conn.commit()
        return proposal

    def get(self, tenant_id: str, proposal_id: str) -> EvolutionProposal | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM evolution_proposals WHERE tenant_id = ? AND proposal_id = ?", (tenant_id, proposal_id),
            ).fetchone()
            return EvolutionProposal(**dict(row)) if row else None

    def list_for_tenant(self, tenant_id: str, *, status: str | None = None) -> list[EvolutionProposal]:
        with self._lock, self._db() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM evolution_proposals WHERE tenant_id = ? AND status = ? ORDER BY created_at DESC",
                    (tenant_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM evolution_proposals WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,),
                ).fetchall()
            return [EvolutionProposal(**dict(r)) for r in rows]

    def set_status(self, tenant_id: str, proposal_id: str, status: str, *, decided_by: str | None = None) -> EvolutionProposal | None:
        with self._lock, self._db() as conn:
            conn.execute(
                "UPDATE evolution_proposals SET status = ?, decided_at = ?, decided_by = ? "
                "WHERE tenant_id = ? AND proposal_id = ?",
                (status, _now_iso(), decided_by, tenant_id, proposal_id),
            )
            conn.commit()
        return self.get(tenant_id, proposal_id)

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM evolution_proposals WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount


# ==============================================================================
# EvolutionEvaluationStore — sandbox (pre-promotion) and monitoring
# (post-promotion) evaluation history
# ==============================================================================


class EvolutionEvaluation(BaseModel):
    evaluation_id: str
    tenant_id: str
    version_id: str
    evaluation_type: str  # "sandbox" | "monitoring"
    metrics: dict
    verdict: str  # "pass" | "fail" | "insufficient_data" | "regression" | "ok"
    created_at: str


class EvolutionEvaluationStore(SqliteStore):
    def __init__(self, db_path: Path | str = "data/evolution_evaluations.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evolution_evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    evaluation_type TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evolution_evaluations_version ON evolution_evaluations(tenant_id, version_id)")
            conn.commit()

    def record(self, *, tenant_id: str, version_id: str, evaluation_type: str, metrics: dict, verdict: str) -> EvolutionEvaluation:
        evaluation = EvolutionEvaluation(
            evaluation_id=f"eveval_{secrets.token_hex(8)}", tenant_id=tenant_id, version_id=version_id,
            evaluation_type=evaluation_type, metrics=metrics, verdict=verdict, created_at=_now_iso(),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO evolution_evaluations "
                "(evaluation_id, tenant_id, version_id, evaluation_type, metrics_json, verdict, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    evaluation.evaluation_id, evaluation.tenant_id, evaluation.version_id, evaluation.evaluation_type,
                    json.dumps(evaluation.metrics), evaluation.verdict, evaluation.created_at,
                ),
            )
            conn.commit()
        return evaluation

    def list_for_version(self, tenant_id: str, version_id: str) -> list[EvolutionEvaluation]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM evolution_evaluations WHERE tenant_id = ? AND version_id = ? ORDER BY created_at DESC",
                (tenant_id, version_id),
            ).fetchall()
            out = []
            for r in rows:
                data = dict(r)
                data["metrics"] = json.loads(data.pop("metrics_json") or "{}")
                out.append(EvolutionEvaluation(**data))
            return out

    def latest_for_version(self, tenant_id: str, version_id: str) -> EvolutionEvaluation | None:
        results = self.list_for_version(tenant_id, version_id)
        return results[0] if results else None

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM evolution_evaluations WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount


# ==============================================================================
# Observe -> outcome labeling -> failure detection
# ==============================================================================


class FailureSignal(BaseModel):
    tenant_id: str
    reason: str
    dissatisfaction_rate: float
    sample_size: int
    suggested_tone_instructions: str


DEFAULT_TONE_SUGGESTION = (
    "Acknowledge the customer's specific concern in your own words before "
    "answering, and if you can only partially help, say plainly what you "
    "don't know instead of glossing over it."
)


def detect_failure_signal(
    tenant_id: str, *, analytics_store, lookback_hours: int = EVOLUTION_LOOKBACK_HOURS,
    dissatisfaction_rate_threshold: float = EVOLUTION_FAILURE_DISSATISFACTION_RATE_THRESHOLD,
) -> FailureSignal | None:
    """Pure read-model failure detection over EXISTING conversation
    analytics — the only signal that's actually about the customer
    assistant's own behavior (employee feedback in feedback.py is about
    internal business operations, a different thing entirely, and is
    deliberately not consulted here). Returns None when there isn't
    enough real traffic yet to draw a conclusion, or when the
    dissatisfaction rate isn't actually elevated — both are "no signal",
    not "everything is fine," but the distinction only matters for
    logging, never for whether a proposal gets created.

    lookback_hours/dissatisfaction_rate_threshold default to the platform
    constants but are overridable per tenant (Phase 20's "configurable
    levers" — see TenantConfig.evolution_lookback_hours/
    evolution_dissatisfaction_threshold); this function itself stays
    LLM-free and deterministic either way."""
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - lookback_hours * 3600))
    summary = analytics_store.summary_for_tenant(tenant_id, since_iso=since_iso)
    if summary.total_questions < EVOLUTION_MIN_SAMPLE_FOR_DETECTION:
        return None
    rate = summary.dissatisfaction_count / summary.total_questions
    if rate < dissatisfaction_rate_threshold:
        return None
    return FailureSignal(
        tenant_id=tenant_id, reason="elevated_dissatisfaction_rate", dissatisfaction_rate=round(rate, 4),
        sample_size=summary.total_questions, suggested_tone_instructions=DEFAULT_TONE_SUGGESTION,
    )


# How many of the window's actual dissatisfied questions to hand the LLM
# for theme identification / tailored drafting — a small, representative
# sample, not the whole window (keeps the prompt small and the signal
# concentrated on the most recent complaints).
THEME_SAMPLE_SIZE = 8
MAX_THEME_LENGTH = 80


def generate_behavior_proposal(
    tenant_id: str, *, analytics_store, evolution_version_store: EvolutionVersionStore,
    evolution_proposal_store: EvolutionProposalStore,
    lookback_hours: int = EVOLUTION_LOOKBACK_HOURS,
    dissatisfaction_rate_threshold: float = EVOLUTION_FAILURE_DISSATISFACTION_RATE_THRESHOLD,
    generator=None,
) -> EvolutionProposal | None:
    """Improvement proposal generation: on a detected failure signal,
    drafts a candidate assistant_tone version (status="draft", never
    active) and records a proposal for it.

    Detection itself stays exactly as deterministic as before (see
    detect_failure_signal) — that reliability guarantee is never
    compromised. What's new in Phase 20 is WHAT the candidate says once a
    signal has already, deterministically, fired: when `generator` is
    given, this pulls a sample of the window's actual dissatisfied
    questions and asks the LLM to (a) name the common theme and (b) draft
    tone guidance tailored to it — grounded in real complaints instead of
    always suggesting the same generic text. The draft is validated
    through validate_behavior_payload (via the pre-check below, and again
    inside evolution_version_store.create — the one choke point never
    changes) BEFORE it's trusted; on any failure (no generator, no LLM
    access, a malformed response, or content the safety filter rejects),
    this falls back to the exact original deterministic
    DEFAULT_TONE_SUGGESTION — the proposal pipeline must keep working
    during an LLM outage, same invariant run_monitoring_check already
    holds for the rollback safety net."""
    signal = detect_failure_signal(
        tenant_id, analytics_store=analytics_store, lookback_hours=lookback_hours,
        dissatisfaction_rate_threshold=dissatisfaction_rate_threshold,
    )
    if signal is None:
        return None

    tone_instructions = signal.suggested_tone_instructions
    theme: str | None = None
    if generator is not None:
        since_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - lookback_hours * 3600))
        sample_queries = analytics_store.list_recent_dissatisfied_queries(
            tenant_id, since_iso=since_iso, limit=THEME_SAMPLE_SIZE,
        )
        if sample_queries:
            try:
                draft = generator.draft_tone_adjustment(dissatisfied_queries=sample_queries)
                candidate_text = draft.tone_instructions
                validate_behavior_payload("assistant_tone", {"tone_instructions": candidate_text})
                tone_instructions = candidate_text
                theme = (draft.theme or "").strip()[:MAX_THEME_LENGTH] or None
            except Exception:
                pass  # any failure (LLM error, malformed output, unsafe text) -> keep the deterministic default

    rationale = (
        f"Dissatisfaction rate {signal.dissatisfaction_rate:.0%} over the last "
        f"{signal.sample_size} customer questions crossed the "
        f"{dissatisfaction_rate_threshold:.0%} threshold."
    )
    if theme:
        rationale += f" Common theme in the dissatisfied questions: {theme}."

    current_active = evolution_version_store.get_active(tenant_id, "assistant_tone")
    candidate = evolution_version_store.create(
        tenant_id=tenant_id, config_type="assistant_tone",
        payload={"tone_instructions": tone_instructions},
        created_by="self_evolution_engine",
        rationale=rationale,
        parent_version_id=current_active.version_id if current_active else None,
        status="draft",
    )
    return evolution_proposal_store.create(
        tenant_id=tenant_id, trigger_reason=signal.reason, candidate_version_id=candidate.version_id,
    )


# ==============================================================================
# Sandbox / shadow evaluation — real retrieval+generation, NEVER shown to
# a real customer, run only against already-answered historical questions
# ==============================================================================


def run_sandbox_evaluation(
    tenant_id: str, proposal: EvolutionProposal, *, evolution_version_store: EvolutionVersionStore,
    evolution_evaluation_store: EvolutionEvaluationStore, analytics_store, vector_store, embeddings, generator,
    business_name: str, assistant_name: str,
) -> EvolutionEvaluation:
    """Shadow-replays a small sample of the tenant's own recently
    ANSWERED (never abstained) real questions through the real
    retrieval+generation pipeline twice — once with the current active
    tone config, once with the candidate — comparing outcomes. Neither
    run is ever sent to a customer or logged as a real conversation turn;
    this is pure evaluation. A query where the two runs' evidence pack is
    identical (retrieval never depends on tone) but the candidate's
    answer regresses (flips from answered to insufficient_evidence, or
    newly shows dissatisfaction) counts as a regression; any regression
    fails the sandbox outright — gated promotion below refuses to
    activate a version that hasn't passed this check."""
    from business_ai.generation import build_system_prompt, build_user_prompt, evaluate_evidence_gate, validate_llm_draft
    from business_ai.retrieval import RetrievalEngine

    candidate_version = evolution_version_store.get(tenant_id, proposal.candidate_version_id)
    if candidate_version is None:
        raise ValueError(f"Unknown candidate_version_id {proposal.candidate_version_id!r}.")
    baseline_tone = ""
    if candidate_version.parent_version_id:
        parent = evolution_version_store.get(tenant_id, candidate_version.parent_version_id)
        if parent is not None:
            baseline_tone = parent.payload.get("tone_instructions", "")
    candidate_tone = candidate_version.payload.get("tone_instructions", "")

    queries = analytics_store.list_recent_answered_queries(tenant_id, limit=EVOLUTION_SANDBOX_SAMPLE_SIZE)
    engine = RetrievalEngine(vector_store, embeddings)
    sample_size = 0
    regressions = 0
    regression_details: list[str] = []
    for query in queries:
        pack = engine.retrieve(query, tenant_id=tenant_id, top_k=5)
        gate = evaluate_evidence_gate(pack)
        if gate.should_abstain:
            continue  # tone can't change an abstention outcome; not informative
        sample_size += 1
        user_prompt = build_user_prompt(pack)

        baseline_prompt = build_system_prompt(business_name, assistant_name, tone_instructions=baseline_tone)
        baseline_answer = validate_llm_draft(generator.generate(system_prompt=baseline_prompt, user_prompt=user_prompt), pack)

        candidate_prompt = build_system_prompt(business_name, assistant_name, tone_instructions=candidate_tone)
        candidate_answer = validate_llm_draft(generator.generate(system_prompt=candidate_prompt, user_prompt=user_prompt), pack)

        regressed = (
            (baseline_answer.status.value == "answered" and candidate_answer.status.value != "answered")
            or (not baseline_answer.shows_dissatisfaction and candidate_answer.shows_dissatisfaction)
        )
        if regressed:
            regressions += 1
            regression_details.append(query)

    if sample_size == 0:
        verdict = "insufficient_data"
    elif regressions > 0:
        verdict = "fail"
    else:
        verdict = "pass"

    metrics = {"sample_size": sample_size, "regressions": regressions, "regressed_queries": regression_details}
    evaluation = evolution_evaluation_store.record(
        tenant_id=tenant_id, version_id=candidate_version.version_id, evaluation_type="sandbox",
        metrics=metrics, verdict=verdict,
    )
    evolution_version_store.set_status(
        tenant_id, candidate_version.version_id,
        "shadow_tested_pass" if verdict == "pass" else "shadow_tested_fail" if verdict == "fail" else "draft",
    )
    return evaluation


# ==============================================================================
# Monitoring + automatic rollback (post-promotion)
# ==============================================================================


def run_monitoring_check(
    tenant_id: str, active_version: EvolutionVersion, *, analytics_store,
    regression_delta: float = EVOLUTION_MONITORING_REGRESSION_DELTA,
    lookback_hours: int = EVOLUTION_LOOKBACK_HOURS,
) -> dict:
    """Pure, LLM-free comparison of a promoted version's post-activation
    dissatisfaction rate against its own pre-activation baseline window —
    deliberately reads only durable AnalyticsStore data so this safety
    net keeps working even during an LLM/API outage, which is exactly
    when it matters most. Returns one of:
      - {"action": "skipped", "reason": ...} — not enough signal yet, do nothing
      - {"action": "ok", "metrics": {...}} — no regression, do nothing
      - {"action": "regression", "metrics": {...}} — caller should roll back

    regression_delta defaults to the platform constant but is overridable
    per tenant (TenantConfig.evolution_regression_delta) — a lower value
    makes automatic rollback more sensitive, a higher one more tolerant;
    this function's own comparison logic is unchanged either way."""
    if active_version.activated_at is None:
        return {"action": "skipped", "reason": "not_activated"}
    if active_version.parent_version_id is None:
        return {"action": "skipped", "reason": "no_prior_version_to_compare_or_roll_back_to"}
    hours_active = _hours_since(active_version.activated_at)
    if hours_active < EVOLUTION_MONITORING_MIN_HOURS_ACTIVE:
        return {"action": "skipped", "reason": "too_recent"}

    lookback_start = _iso_minus_hours(active_version.activated_at, lookback_hours)
    baseline = analytics_store.summary_for_tenant(tenant_id, since_iso=lookback_start, until_iso=active_version.activated_at)
    post = analytics_store.summary_for_tenant(tenant_id, since_iso=active_version.activated_at)

    if baseline.total_questions < EVOLUTION_MONITORING_MIN_SAMPLE or post.total_questions < EVOLUTION_MONITORING_MIN_SAMPLE:
        return {
            "action": "skipped", "reason": "insufficient_data",
            "baseline_n": baseline.total_questions, "post_n": post.total_questions,
        }

    baseline_rate = baseline.dissatisfaction_count / baseline.total_questions
    post_rate = post.dissatisfaction_count / post.total_questions
    delta = post_rate - baseline_rate
    metrics = {
        "baseline_rate": round(baseline_rate, 4), "post_rate": round(post_rate, 4), "delta": round(delta, 4),
        "baseline_n": baseline.total_questions, "post_n": post.total_questions,
    }
    if delta >= regression_delta:
        return {"action": "regression", "metrics": metrics}
    return {"action": "ok", "metrics": metrics}
