"""Revenue Leakage Radar (Phase 15) HTTP route — owner/manager-gated,
tenant-isolated, reusing VIEW_FEEDBACK (the established "aggregate
management view, not for staff" permission also used by Phase 10's
Business Map — VIEW_LEADS itself is available to staff, which is too
broad for a consolidated missed-revenue report).
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from business_ai.revenue_radar import compute_revenue_leakage
from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_revenue_radar(app: FastAPI, svc, ctx) -> None:
    @app.get("/api/revenue-radar")
    def get_revenue_radar(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        report = compute_revenue_leakage(
            tenant_id, lead_store=svc.lead_store, analytics_store=svc.analytics_store,
            deposit_amount_inr=tenant.deposit_amount_inr,
        )
        return report.model_dump()
