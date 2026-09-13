"""Read-only intelligence views: raw analytics/gaps, the Business Health
snapshot, the Owner Command Center, and the audit-log-backed timeline
(Phase 9 extraction from app.py). All computation lives in
routers/admin_bot.py (ctx._business_health_snapshot etc) — this module is
purely the HTTP surface over it, reused by the WhatsApp scorecard/health
commands too.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_insights(app: FastAPI, svc, ctx) -> None:
    @app.get("/api/analytics")
    def get_analytics(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_ANALYTICS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return svc.analytics_store.summary_for_tenant(tenant_id).model_dump()

    @app.get("/api/analytics/gaps")
    def list_gaps(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_ANALYTICS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"gaps": [g.model_dump() for g in svc.analytics_store.list_open_gaps(tenant_id)]}


    @app.get("/api/business-health")
    def get_business_health(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """The dashboard's read of the same transparent, component-based
        snapshot the `scorecard`/`health` WhatsApp command renders as
        text — one computation (_business_health_snapshot), two
        presentations. Gated the same as feedback: aggregated management
        insight, not something staff sees."""
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return ctx._business_health_snapshot(tenant, window_hours=svc.settings.digest_window_hours)

    @app.get("/api/command-center")
    def get_command_center(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Phase 7: the owner's single "what's going on" view — a pure
        reorganization of signals that already exist (the same
        business-health snapshot above, plus the automation engine's own
        execution history) around the questions an owner actually asks:
        what needs my attention, where's the revenue opportunity, what's
        operationally broken, is it trending up or down, what should I do
        next, and what has automation already handled for me. No LLM call
        on this path — see _recommended_actions. Gated the same as
        business-health/feedback: aggregated management insight."""
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        snapshot = ctx._business_health_snapshot(tenant, window_hours=svc.settings.digest_window_hours)
        recent_successes = [
            r for r in svc.automation_run_store.list_for_tenant(tenant_id, limit=50) if r.status == "success"
        ][:10]
        rule_names: dict[str, str] = {}
        automated_actions_taken = []
        for run in recent_successes:
            if run.rule_id not in rule_names:
                rule = svc.automation_rule_store.get(tenant_id, run.rule_id)
                rule_names[run.rule_id] = rule.name if rule else "(deleted rule)"
            automated_actions_taken.append(ctx._render_automated_action_line(run, rule_names[run.rule_id]))

        return {
            "needs_attention": {
                "overdue_tasks": snapshot["tasks_overdue"],
                "overdue_task_lines": snapshot["overdue_lines"],
                "unresolved_complaints": snapshot["dissatisfaction_count"],
                "recurring_issues_without_sop": snapshot["recurring_themes_total"] - snapshot["recurring_themes_with_sop"],
            },
            "revenue_opportunities": {
                "missed_opportunities": snapshot["missed_opportunity_count"],
                "missed_opportunity_lines": snapshot["missed_opportunity_lines"],
                "buying_intent_count": snapshot["buying_intent_count"],
                "confirmed_revenue_inr": snapshot["confirmed_revenue_inr"],
                "conversion_rate_pct": snapshot["conversion_rate_pct"],
            },
            "operational_problems": {
                "tasks_overdue": snapshot["tasks_overdue"],
                "unresolved_negative_feedback": snapshot["dissatisfaction_count"],
                "recurring_feedback_lines": snapshot["recurring_feedback_lines"],
            },
            "trends": snapshot["trends"],
            "recommended_actions": ctx._recommended_actions(snapshot),
            "automated_actions_taken": automated_actions_taken,
        }

    @app.get("/api/approvals")
    def get_approvals(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Phase 21's Approval Inbox: aggregates every entity type in
        this codebase with a genuine pending-approval concept — a task
        marked awaiting_approval, and a Self-Evolution proposal pending
        owner review — into one list. Deliberately NOT one blanket
        permission check: task approval is owner+manager (ASSIGN_TASK)
        while evolution is owner-only (MANAGE_EVOLUTION), so this checks
        each section against its own existing authorize() call and
        simply omits a section the caller isn't authorized for, rather
        than 403ing the whole inbox — a manager's inbox correctly shows
        pending tasks with no evolution section, not an error."""
        principal = ctx._resolve(authorization)
        try:
            svc.tenant_registry.get_config(tenant_id)
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        items: list[dict] = []
        try:
            authorize(principal, TenantAction.VIEW_TASKS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError:
            pass
        else:
            for t in svc.task_store.list_for_tenant(tenant_id, status="awaiting_approval"):
                items.append({
                    "type": "task", "id": t.task_id, "title": t.title,
                    "detail": t.description or "", "created_at": t.updated_at,
                })

        try:
            authorize(principal, TenantAction.MANAGE_EVOLUTION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError:
            pass
        else:
            for p in svc.evolution_proposals.list_for_tenant(tenant_id, status="pending_owner_review"):
                candidate = svc.evolution_versions.get(tenant_id, p.candidate_version_id)
                items.append({
                    "type": "evolution_proposal", "id": p.proposal_id, "title": "Assistant tone adjustment",
                    "detail": candidate.rationale if candidate else "", "created_at": p.created_at,
                })

        items.sort(key=lambda x: x["created_at"])
        return {"items": items}

    @app.get("/api/timeline")
    def get_timeline(tenant_id: str, authorization: str | None = Header(default=None), limit: int = 50) -> dict:
        """A read over the existing audit log, not a new event-store —
        every entry here is something AuditLogStore already recorded for
        an unrelated reason (permissions, dispute resolution); this
        endpoint just renders it chronologically. Gated the same as
        feedback/business-health: aggregated management insight."""
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"entries": [e.model_dump() for e in svc.audit_log.list_for_tenant(tenant_id, limit=min(limit, 200))]}
