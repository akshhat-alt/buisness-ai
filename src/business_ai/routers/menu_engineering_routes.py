"""On-demand read access to Restaurant Profitability Intelligence
(Phase 22) and Restaurant Autopilot (Phase 24): dish food-cost/
profitability/menu-engineering classification, reorder suggestions,
menu-price recommendations, the price what-if simulator ("Business
Twin"), and the Digital GM daily briefing. All pure computation over
stores that already exist (menu, purchases, metrics, inventory,
automation runs, leads, shifts) — no new logging mechanism, no new
SQLite table. Gated by VIEW_INVENTORY, the same owner+manager tier
already gating every other menu/inventory read (restaurant_routes.py).
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from business_ai.digital_gm import build_digital_gm_briefing
from business_ai.menu_engineering import (
    build_menu_engineering_report,
    build_menu_recommendations,
    build_reorder_suggestions,
    simulate_menu_item_price,
)
from business_ai.schemas import SimulateMenuPriceRequest
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

    @app.get("/api/menu-recommendations")
    def get_menu_recommendations(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
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
        recommendations = build_menu_recommendations(report)
        return {"recommendations": [r.model_dump() for r in recommendations]}

    @app.post("/api/menu-engineering/simulate")
    def post_simulate_menu_price(
        tenant_id: str, request: SimulateMenuPriceRequest, authorization: str | None = Header(default=None),
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_INVENTORY, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        result = simulate_menu_item_price(
            tenant_id, request.menu_item_id, request.hypothetical_price_inr,
            menu_store=svc.menu_store, purchase_store=svc.purchase_store,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Menu item not found")
        return result.model_dump()

    @app.get("/api/digital-gm-briefing")
    def get_digital_gm_briefing(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_INVENTORY, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        lines = build_digital_gm_briefing(
            tenant_id, menu_store=svc.menu_store, purchase_store=svc.purchase_store, metric_store=svc.metric_store,
            inventory_store=svc.inventory_store, automation_run_store=svc.automation_run_store,
            lead_store=svc.lead_store, shift_store=svc.shift_store,
        )
        return {"lines": lines}
