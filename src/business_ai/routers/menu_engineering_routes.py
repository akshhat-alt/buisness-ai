"""On-demand read access to Restaurant Profitability Intelligence
(Phase 22): dish food-cost/profitability/menu-engineering classification
and reorder suggestions. Both are pure computation over stores that
already exist (menu, purchases, metrics, inventory, automation runs) —
no new logging mechanism, no new SQLite table. Gated by VIEW_INVENTORY,
the same owner+manager tier already gating every other menu/inventory
read (restaurant_routes.py).
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from business_ai.menu_engineering import build_menu_engineering_report, build_reorder_suggestions
from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_menu_engineering(app: FastAPI, svc, ctx) -> None:
    @app.get("/api/menu-engineering")
    def get_menu_engineering(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_INVENTORY, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        report = build_menu_engineering_report(
            tenant_id, menu_store=svc.menu_store, purchase_store=svc.purchase_store, metric_store=svc.metric_store,
        )
        return report.model_dump()

    @app.get("/api/reorder-suggestions")
    def get_reorder_suggestions(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_INVENTORY, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        report = build_reorder_suggestions(tenant_id, inventory_store=svc.inventory_store, automation_run_store=svc.automation_run_store)
        return report.model_dump()
