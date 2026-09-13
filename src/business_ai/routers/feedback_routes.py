"""Employee feedback (list/resolve) and owner-approved SOP notes (Phase 9
extraction from app.py). Classification/theme logic lives in
routers/admin_bot.py (ctx._resolve_theme_key) since the WhatsApp
`feedback`/`approve sop` commands share it with these API routes.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from business_ai.schemas import ApproveSopRequest
from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_feedback(app: FastAPI, svc, ctx) -> None:
    @app.get("/api/feedback")
    def list_feedback(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "items": [f.model_dump() for f in svc.feedback_store.list_for_tenant(tenant_id)],
            "themes": [t.model_dump() for t in svc.feedback_store.summarize_by_theme(tenant_id)],
        }

    @app.post("/api/feedback/{feedback_id}/resolve")
    def resolve_feedback(feedback_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        updated = svc.feedback_store.mark_resolved(tenant_id, feedback_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Unknown feedback_id for this business.")
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="feedback_resolved",
            target_type="feedback", target_id=feedback_id,
        )
        return updated.model_dump()

    @app.get("/api/sops")
    def list_sops(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FEEDBACK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"sops": [s.model_dump() for s in svc.sop_store.list_for_tenant(tenant_id)]}

    @app.post("/api/sops")
    def approve_sop(request: ApproveSopRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_SOPS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        theme_key = ctx._resolve_theme_key(request.theme)
        if theme_key is None:
            raise HTTPException(status_code=400, detail=f"Unknown feedback theme '{request.theme}'.")
        try:
            note = svc.sop_store.approve(
                tenant_id=tenant_id, theme=theme_key, text=request.text, approved_by_employee_id=principal.principal_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="sop_approved",
            target_type="sop", target_id=theme_key,
        )
        return note.model_dump()

