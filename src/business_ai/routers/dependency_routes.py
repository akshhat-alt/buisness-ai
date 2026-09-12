"""Business Dependency Intelligence (Phase 10): the owner-facing
"Business Map" API — who depends on whom, what breaks if a specific
employee is unavailable. Pure HTTP surface over dependency_graph.py's
deterministic computation; gated the same as business-health/feedback
(aggregated management insight, never a per-employee performance score,
never something staff sees).
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from business_ai.dependency_graph import compute_dependency_snapshot, simulate_employee_unavailable
from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_dependency(app: FastAPI, svc, ctx) -> None:
    @app.get("/api/dependency/map")
    def get_dependency_map(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return compute_dependency_snapshot(
            tenant_id, employee_store=svc.employee_store, task_store=svc.task_store,
            sop_store=svc.sop_store, lead_store=svc.lead_store,
        )

    @app.get("/api/dependency/simulate")
    def get_dependency_simulation(
        tenant_id: str, employee_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        """"What breaks if this employee is unavailable?" — on demand,
        for any employee on the roster."""
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        employee = svc.employee_store.get(tenant_id, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="Unknown employee_id for this business.")
        return simulate_employee_unavailable(
            tenant_id, employee_id, employee_store=svc.employee_store, task_store=svc.task_store,
            sop_store=svc.sop_store, lead_store=svc.lead_store,
        )
