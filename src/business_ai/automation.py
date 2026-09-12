"""Automation Engine (Phase 6): owner-configurable trigger -> condition ->
action rules, plus their execution history.

Deliberately NOT a new event-sourcing system or in-process scheduler —
consistent with every other periodic job in this codebase (digest,
reminders, reengagement, winback, task-escalation), rule evaluation
happens when a platform_admin-only cron endpoint is hit
(`POST /api/v1/admin/automation/run`, app.py), meant to be invoked by an
external cron. Triggers are pure, deterministic checks against data that
already exists (leads.py, tasks.py, feedback.py) — no new store duplicates
those; this module only adds the RULE (what to watch for, what to do
about it) and the RUN (what actually happened when a rule fired).

Retries/deduplication follow the same idiom as
`WhatsAppInboxStore.claim()`/`TaskStore.reminder_sent_at`: existence of a
SUCCESS run row for (rule_id, target_id) means "already handled, never
fire again." A FAILED run leaves no such row, so the next cron tick
retries automatically — capped by MAX_ATTEMPTS so a persistently-failing
action (e.g. no WhatsApp recipient reachable) surfaces as GIVEN_UP for the
owner to see rather than retrying forever.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Generator

from pydantic import BaseModel

# A failing action retries once per cron tick until this many failed
# attempts have piled up for the same (rule_id, target_id) pair, then
# gives up permanently rather than retrying forever — a persistent
# failure (e.g. no reachable WhatsApp recipient) becomes a visible
# GIVEN_UP entry in execution history for the owner to notice and fix,
# not a silent infinite retry loop.
MAX_ATTEMPTS = 5


class TriggerType(str, Enum):
    # An open task's due_at has passed by at least trigger_params["hours"].
    # Target: task. Complements (not duplicates) the fixed-threshold
    # task-escalation cron — a rule lets an owner pick their OWN threshold
    # and pair it with a chosen action, not just a WhatsApp ping at 48h.
    TASK_OVERDUE = "task_overdue"
    # A negative-sentiment feedback item has sat unresolved for at least
    # trigger_params["hours"]. Target: feedback item.
    NEGATIVE_FEEDBACK_UNRESOLVED = "negative_feedback_unresolved"
    # A feedback theme has been reported at least trigger_params["min_count"]
    # times within the last trigger_params.get("window_hours", 168) hours.
    # Target: a synthetic "theme:<theme>" id — re-fires only if the count
    # has grown since the last successful fire (see _dedup logic in app.py).
    RECURRING_FEEDBACK_THEME = "recurring_feedback_theme"
    # A lead's appointment_at has passed by at least trigger_params["hours"],
    # a deposit link was sent (deposit_link_sent_at is set), but neither
    # deposit_paid_at nor appointment_outcome has been confirmed yet.
    # Target: lead.
    DEPOSIT_UNPAID_AFTER_APPOINTMENT = "deposit_unpaid_after_appointment"


class ActionType(str, Enum):
    # Reuses the existing dual-channel (WhatsApp + email) management
    # notification path (_notify_management_whatsapp / render_* alerts) —
    # no new send mechanism.
    NOTIFY_OWNER = "notify_owner"
    # Reuses TaskStore.create — assigns to action_params["assigned_to_employee_id"]
    # if set, else the tenant's first owner-role employee.
    CREATE_TASK = "create_task"


class RunStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    GIVEN_UP = "given_up"


class AutomationRule(BaseModel):
    rule_id: str
    tenant_id: str
    name: str
    trigger_type: TriggerType
    trigger_params: dict[str, Any] = {}
    action_type: ActionType
    action_params: dict[str, Any] = {}
    enabled: bool = True
    created_by_employee_id: str | None = None
    created_at: str
    updated_at: str


class AutomationRun(BaseModel):
    run_id: str
    tenant_id: str
    rule_id: str
    trigger_type: str
    target_type: str
    target_id: str
    action_type: str
    status: str
    error: str | None = None
    metadata: dict[str, Any] = {}
    created_at: str


class AutomationRuleStore:
    """Thread-safe SQLite store for owner-configured automation rules."""

    def __init__(self, db_path: Path | str = "data/automation_rules.db") -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_rules (
                    rule_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    trigger_params_json TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_params_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_by_employee_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_automation_rules_tenant ON automation_rules(tenant_id)")
            conn.commit()

    def _row_to_rule(self, row: sqlite3.Row) -> AutomationRule:
        data = dict(row)
        data["trigger_params"] = json.loads(data.pop("trigger_params_json") or "{}")
        data["action_params"] = json.loads(data.pop("action_params_json") or "{}")
        data["enabled"] = bool(data["enabled"])
        return AutomationRule(**data)

    def create(
        self,
        *,
        tenant_id: str,
        name: str,
        trigger_type: TriggerType,
        trigger_params: dict[str, Any],
        action_type: ActionType,
        action_params: dict[str, Any],
        created_by_employee_id: str | None = None,
    ) -> AutomationRule:
        if not name or not name.strip():
            raise ValueError("A rule name is required.")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rule = AutomationRule(
            rule_id=f"rule_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            name=name.strip(),
            trigger_type=trigger_type,
            trigger_params=trigger_params,
            action_type=action_type,
            action_params=action_params,
            enabled=True,
            created_by_employee_id=created_by_employee_id,
            created_at=now_iso,
            updated_at=now_iso,
        )
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO automation_rules (
                    rule_id, tenant_id, name, trigger_type, trigger_params_json,
                    action_type, action_params_json, enabled, created_by_employee_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.rule_id, rule.tenant_id, rule.name, rule.trigger_type.value,
                    json.dumps(rule.trigger_params), rule.action_type.value, json.dumps(rule.action_params),
                    int(rule.enabled), rule.created_by_employee_id, rule.created_at, rule.updated_at,
                ),
            )
            conn.commit()
        return rule

    def get(self, tenant_id: str, rule_id: str) -> AutomationRule | None:
        with self._lock, self._db() as conn:
            row = conn.execute(
                "SELECT * FROM automation_rules WHERE tenant_id = ? AND rule_id = ?", (tenant_id, rule_id)
            ).fetchone()
            return self._row_to_rule(row) if row else None

    def list_for_tenant(self, tenant_id: str, *, enabled_only: bool = False) -> list[AutomationRule]:
        query = "SELECT * FROM automation_rules WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if enabled_only:
            query += " AND enabled = 1"
        query += " ORDER BY created_at ASC"
        with self._lock, self._db() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_rule(r) for r in rows]

    def update(self, tenant_id: str, rule_id: str, **fields: Any) -> AutomationRule | None:
        rule = self.get(tenant_id, rule_id)
        if rule is None:
            return None
        updated = rule.model_copy(update={**fields, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        with self._lock, self._db() as conn:
            conn.execute(
                """
                UPDATE automation_rules SET
                    name = ?, trigger_type = ?, trigger_params_json = ?,
                    action_type = ?, action_params_json = ?, enabled = ?, updated_at = ?
                WHERE tenant_id = ? AND rule_id = ?
                """,
                (
                    updated.name, updated.trigger_type.value, json.dumps(updated.trigger_params),
                    updated.action_type.value, json.dumps(updated.action_params), int(updated.enabled),
                    updated.updated_at, tenant_id, rule_id,
                ),
            )
            conn.commit()
        return updated

    def delete(self, tenant_id: str, rule_id: str) -> bool:
        with self._lock, self._db() as conn:
            cur = conn.execute(
                "DELETE FROM automation_rules WHERE tenant_id = ? AND rule_id = ?", (tenant_id, rule_id)
            )
            conn.commit()
            return cur.rowcount > 0


class AutomationRunStore:
    """Thread-safe, append-only SQLite execution history for automation
    rules — every evaluation that actually fires an action writes one row
    here, success or failure. This IS the "execution history" +
    dedup/retry ledger Phase 6 asks for; it's kept separate from the
    generic AuditLogStore (which also gets one entry per successful
    action, for the owner-facing timeline) because a run row carries
    fields — status, error, retry count — that are specific to automation
    execution and would be noise on every other audit entry type.
    """

    def __init__(self, db_path: Path | str = "data/automation_runs.db") -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=10000;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_runs (
                    run_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_automation_runs_tenant ON automation_runs(tenant_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_automation_runs_target ON automation_runs(rule_id, target_id)"
            )
            conn.commit()

    def _row_to_run(self, row: sqlite3.Row) -> AutomationRun:
        data = dict(row)
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return AutomationRun(**data)

    def record(
        self,
        *,
        tenant_id: str,
        rule_id: str,
        trigger_type: str,
        target_type: str,
        target_id: str,
        action_type: str,
        status: RunStatus,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AutomationRun:
        run = AutomationRun(
            run_id=f"run_{secrets.token_hex(8)}",
            tenant_id=tenant_id,
            rule_id=rule_id,
            trigger_type=trigger_type,
            target_type=target_type,
            target_id=target_id,
            action_type=action_type,
            status=status.value,
            error=error,
            metadata=metadata or {},
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                """
                INSERT INTO automation_runs (
                    run_id, tenant_id, rule_id, trigger_type, target_type, target_id,
                    action_type, status, error, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id, run.tenant_id, run.rule_id, run.trigger_type, run.target_type, run.target_id,
                    run.action_type, run.status, run.error, json.dumps(run.metadata), run.created_at,
                ),
            )
            conn.commit()
        return run

    def list_for_tenant(self, tenant_id: str, *, limit: int = 100) -> list[AutomationRun]:
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM automation_runs WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
            return [self._row_to_run(r) for r in rows]

    def history_for_target(self, tenant_id: str, rule_id: str, target_id: str) -> list[AutomationRun]:
        """Every run recorded for one (rule, target) pair, oldest first —
        used to decide whether a trigger has already succeeded, is still
        retrying, or has been given up on."""
        with self._lock, self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM automation_runs WHERE tenant_id = ? AND rule_id = ? AND target_id = ? "
                "ORDER BY created_at ASC",
                (tenant_id, rule_id, target_id),
            ).fetchall()
            return [self._row_to_run(r) for r in rows]
