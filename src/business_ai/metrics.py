"""Business Financial Truth Layer (Phase 12): a manual sales/expense/
collection ledger, honestly labeled as manual — this app has no live
POS/accounting/bank integration, and every report or alert built on this
data must say so, never imply a live sync that doesn't exist.

Reuses the exact `log <type> <amount> [note]` deterministic-keyword-
command idiom already established for tasks (Phase 0) and feedback —
zero LLM cost, 100% predictable. Any employee can log an entry (the same
"submitting needs no permission check" shape as feedback submission);
viewing the aggregated summary is owner/manager-gated (VIEW_FINANCIALS),
like VIEW_FEEDBACK — financial totals are more sensitive than a single
data-entry action.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path

from pydantic import BaseModel

from business_ai.storage import SqliteStore

METRIC_TYPES = frozenset({"sale", "expense", "collection"})


class BusinessMetric(BaseModel):
    metric_id: str
    tenant_id: str
    metric_type: str  # "sale" | "expense" | "collection"
    amount_inr: int
    note: str = ""
    reported_by_employee_id: str | None = None
    source: str = "manual"  # always "manual" today — see module docstring
    # Phase 17 (Restaurant Foundation): set only for a dish sale logged via
    # "log sale <dish> [x<qty>]" — lets Phase 22's food-cost/menu-engineering
    # intelligence query "every sale of this dish" directly from the SAME
    # ledger every other financial number already comes from, rather than
    # inventing a second, parallel "orders" table that could drift from it.
    menu_item_id: str | None = None
    quantity: float | None = None
    created_at: str


class MetricSummary(BaseModel):
    metric_type: str
    total_inr: int
    entry_count: int


class BusinessMetricStore(SqliteStore):
    def __init__(self, db_path: Path | str = "data/metrics.db") -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS business_metrics (
                    metric_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    amount_inr INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    reported_by_employee_id TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_tenant ON business_metrics(tenant_id)")
            # Additive columns added after the table's initial shape (Phase
            # 17) — guarded ALTER TABLE self-heals an existing database
            # that predates them, same pattern as analytics.py's own
            # "resolved"/"shows_dissatisfaction" columns.
            for column_sql in ("menu_item_id TEXT", "quantity REAL"):
                try:
                    conn.execute(f"ALTER TABLE business_metrics ADD COLUMN {column_sql}")
                except sqlite3.OperationalError:
                    pass  # column already exists
            conn.commit()

    def record(
        self, *, tenant_id: str, metric_type: str, amount_inr: int, note: str = "",
        reported_by_employee_id: str | None = None, menu_item_id: str | None = None, quantity: float | None = None,
    ) -> BusinessMetric:
        if metric_type not in METRIC_TYPES:
            raise ValueError(f"Unknown metric_type: {metric_type!r}. Must be one of {sorted(METRIC_TYPES)}.")
        if amount_inr <= 0:
            raise ValueError("amount_inr must be a positive whole number.")
        metric = BusinessMetric(
            metric_id=f"metric_{secrets.token_hex(8)}", tenant_id=tenant_id, metric_type=metric_type,
            amount_inr=amount_inr, note=note.strip(), reported_by_employee_id=reported_by_employee_id,
            menu_item_id=menu_item_id, quantity=quantity,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with self._lock, self._db() as conn:
            conn.execute(
                "INSERT INTO business_metrics "
                "(metric_id, tenant_id, metric_type, amount_inr, note, reported_by_employee_id, source, menu_item_id, quantity, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    metric.metric_id, metric.tenant_id, metric.metric_type, metric.amount_inr, metric.note,
                    metric.reported_by_employee_id, metric.source, metric.menu_item_id, metric.quantity, metric.created_at,
                ),
            )
            conn.commit()
        return metric

    def list_dish_sales(self, tenant_id: str, menu_item_id: str, *, since_iso: str | None = None) -> list[BusinessMetric]:
        """Every logged sale of one specific dish — the raw material
        Phase 22's food-cost/menu-engineering intelligence reads. Reads
        the SAME ledger every other financial number comes from."""
        clause = "tenant_id = ? AND metric_type = 'sale' AND menu_item_id = ?"
        params: list = [tenant_id, menu_item_id]
        if since_iso:
            clause += " AND created_at >= ?"
            params.append(since_iso)
        with self._lock, self._db() as conn:
            rows = conn.execute(f"SELECT * FROM business_metrics WHERE {clause} ORDER BY created_at DESC", params).fetchall()
            return [BusinessMetric(**dict(r)) for r in rows]

    def list_for_tenant(
        self, tenant_id: str, *, metric_type: str | None = None, since_iso: str | None = None,
        until_iso: str | None = None, limit: int = 500,
    ) -> list[BusinessMetric]:
        clause = "tenant_id = ?"
        params: list = [tenant_id]
        if metric_type:
            clause += " AND metric_type = ?"
            params.append(metric_type)
        if since_iso:
            clause += " AND created_at >= ?"
            params.append(since_iso)
        if until_iso:
            clause += " AND created_at < ?"
            params.append(until_iso)
        with self._lock, self._db() as conn:
            rows = conn.execute(
                f"SELECT * FROM business_metrics WHERE {clause} ORDER BY created_at DESC LIMIT ?", (*params, limit),
            ).fetchall()
            return [BusinessMetric(**dict(r)) for r in rows]

    def summary_for_tenant(
        self, tenant_id: str, *, since_iso: str | None = None, until_iso: str | None = None,
    ) -> list[MetricSummary]:
        clause = "tenant_id = ?"
        params: list = [tenant_id]
        if since_iso:
            clause += " AND created_at >= ?"
            params.append(since_iso)
        if until_iso:
            clause += " AND created_at < ?"
            params.append(until_iso)
        with self._lock, self._db() as conn:
            rows = conn.execute(
                f"SELECT metric_type, SUM(amount_inr) as total, COUNT(*) as cnt "
                f"FROM business_metrics WHERE {clause} GROUP BY metric_type",
                params,
            ).fetchall()
            return [MetricSummary(metric_type=r["metric_type"], total_inr=r["total"], entry_count=r["cnt"]) for r in rows]

    def delete_for_tenant(self, tenant_id: str) -> int:
        with self._lock, self._db() as conn:
            cur = conn.execute("DELETE FROM business_metrics WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount
