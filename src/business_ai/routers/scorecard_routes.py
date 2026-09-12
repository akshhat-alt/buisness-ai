"""On-demand read access to the Weekly Business Scorecard (Phase 16) —
the cron (admin_routes.py's admin_run_weekly_scorecard) pushes this data
by email/WhatsApp on a schedule; this route lets the owner pull the same
numbers on demand from the dashboard, computed identically (same
scorecard.py functions), with zero side effects (no send, no dedup
state) — a pure read.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Header, HTTPException

from business_ai.dependency_graph import compute_dependency_snapshot
from business_ai.scorecard import build_weekly_scorecard_data
from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_scorecard(app: FastAPI, svc, ctx) -> None:
    @app.get("/api/scorecard")
    def get_scorecard(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        now = time.time()
        one_week_ago_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7 * 86400))
        two_weeks_ago_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 14 * 86400))

        snapshot = compute_dependency_snapshot(
            tenant_id, employee_store=svc.employee_store, task_store=svc.task_store,
            sop_store=svc.sop_store, lead_store=svc.lead_store,
        )
        high_severity_count = len([r for r in snapshot["risks"] if r["severity"] == "high"])

        data = build_weekly_scorecard_data(
            tenant_id, one_week_ago_iso=one_week_ago_iso, two_weeks_ago_iso=two_weeks_ago_iso,
            task_store=svc.task_store, analytics_store=svc.analytics_store, metric_store=svc.metric_store,
            feedback_store=svc.feedback_store, high_severity_risk_count=high_severity_count,
        )
        return data.model_dump()
