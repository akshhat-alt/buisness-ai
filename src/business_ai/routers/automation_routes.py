"""Automation Engine owner controls: rule CRUD, execution history, and
the kill switch (Phase 9 extraction from app.py). Rule evaluation itself
runs from the platform-admin cron endpoint in routers/admin.py, using the
same ctx._fire_automation_rule from routers/admin_bot.py.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from business_ai.schemas import (
    AutomationKillSwitchRequest,
    CreateAutomationRuleRequest,
    UpdateAutomationRuleRequest,
)
from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_automation(app: FastAPI, svc, ctx) -> None:
    @app.get("/api/automation/rules")
    def list_automation_rules(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_AUTOMATION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"rules": [r.model_dump() for r in svc.automation_rule_store.list_for_tenant(tenant_id)]}

    @app.post("/api/automation/rules")
    def create_automation_rule(
        request: CreateAutomationRuleRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_AUTOMATION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            rule = svc.automation_rule_store.create(
                tenant_id=tenant_id, name=request.name, trigger_type=request.trigger_type,
                trigger_params=request.trigger_params, action_type=request.action_type,
                action_params=request.action_params, created_by_employee_id=principal.principal_id,
                escalate_after_hours=request.escalate_after_hours,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="automation_rule_created",
            target_type="automation_rule", target_id=rule.rule_id, metadata={"rule_name": rule.name},
        )
        return rule.model_dump()

    @app.patch("/api/automation/rules/{rule_id}")
    def update_automation_rule(
        rule_id: str, request: UpdateAutomationRuleRequest, tenant_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_AUTOMATION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        fields = {k: v for k, v in request.model_dump().items() if v is not None}
        if not fields:
            existing = svc.automation_rule_store.get(tenant_id, rule_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="Unknown rule_id for this business.")
            return existing.model_dump()
        try:
            updated = svc.automation_rule_store.update(tenant_id, rule_id, **fields)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="Unknown rule_id for this business.")
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="automation_rule_updated",
            target_type="automation_rule", target_id=updated.rule_id, metadata={"rule_name": updated.name},
        )
        return updated.model_dump()

    @app.delete("/api/automation/rules/{rule_id}")
    def delete_automation_rule(rule_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_AUTOMATION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        rule = svc.automation_rule_store.get(tenant_id, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="Unknown rule_id for this business.")
        svc.automation_rule_store.delete(tenant_id, rule_id)
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="automation_rule_deleted",
            target_type="automation_rule", target_id=rule_id, metadata={"rule_name": rule.name},
        )
        return {"deleted": True}

    @app.get("/api/automation/runs")
    def list_automation_runs(tenant_id: str, authorization: str | None = Header(default=None), limit: int = 100) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_AUTOMATION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"runs": [r.model_dump() for r in svc.automation_run_store.list_for_tenant(tenant_id, limit=min(limit, 200))]}

    @app.post("/api/automation/kill-switch")
    def set_automation_kill_switch(
        request: AutomationKillSwitchRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        """Owner-only, instant, tenant-wide: flips automation_enabled on
        TenantConfig, checked fail-closed at the top of every automation
        cron run. Does not touch individual rules' own enabled flags —
        flipping this back on resumes exactly the rules that were already
        turned on before."""
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_AUTOMATION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        updated = svc.tenant_registry.update_config(tenant_id, automation_enabled=request.enabled)
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="automation_kill_switch",
            target_type="tenant", target_id=tenant_id,
            metadata={"state": "on" if request.enabled else "off"},
        )
        return {"automation_enabled": updated.automation_enabled}

