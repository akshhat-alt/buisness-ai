"""On-demand read/write access to Phase 25's review aggregation
(reviews.py): the latest snapshot per platform, and a manual paste-in
write path for the dashboard (mirrors POST /api/metrics's own VIEW_
FINANCIALS gate exactly — reading and manually logging a review rating
are both a business-truth operation, same sensitivity tier).
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from business_ai.reviews import KNOWN_PLATFORMS
from business_ai.schemas import ManualReviewLogRequest
from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_reviews(app: FastAPI, svc, ctx) -> None:
    @app.get("/api/reviews")
    def get_reviews(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FINANCIALS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        latest = svc.review_store.latest_by_platform(tenant_id)
        return {"platforms": {platform: snapshot.model_dump() for platform, snapshot in latest.items()}}

    @app.post("/api/reviews/manual")
    def post_manual_review(request: ManualReviewLogRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FINANCIALS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        platform = request.platform.strip().lower()
        if platform not in KNOWN_PLATFORMS:
            raise HTTPException(status_code=400, detail=f"platform must be one of: {', '.join(sorted(KNOWN_PLATFORMS))}")
        snapshot = svc.review_store.record(
            tenant_id=tenant_id, platform=platform, rating=request.rating, review_count=request.review_count,
            source="manual",
        )
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="review_logged",
            target_type="review", target_id=snapshot.review_id,
            metadata={"platform": platform, "rating": request.rating, "review_count": request.review_count, "via": "api"},
        )
        return snapshot.model_dump()
