"""Health check + auth: signup, login, "who am I" (Phase 9 extraction
from app.py).
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Header, HTTPException

from business_ai.auth import Principal, create_access_token
from business_ai.formatting import _slugify_tenant_id
from business_ai.schemas import LoginRequest, SignupRequest
from business_ai.security import InvalidTenantIdError, validate_tenant_id
from business_ai.tenant import TenantConfig, TenantStatus


def register_auth(app: FastAPI, svc, ctx) -> None:

    # -------------------------------------------------------------- health
    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    # -------------------------------------------------------------- auth
    @app.post("/api/auth/signup")
    def signup(request: SignupRequest) -> dict:
        tenant_id = _slugify_tenant_id(request.business_name)
        try:
            tenant_id = validate_tenant_id(tenant_id)
        except InvalidTenantIdError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            user = svc.user_store.create_user(
                email=request.email, password=request.password, name=request.name,
                business_name=request.business_name, tenant_id=tenant_id, role="owner",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        new_tenant = svc.tenant_registry.register(
            TenantConfig(
                tenant_id=tenant_id, business_name=request.business_name, owner_email=user.email,
                status=TenantStatus.PROVISIONING,
            )
        )
        ctx._notify_admin_of_signup(new_tenant)

        principal = Principal.owner(user.user_id, tenant_id)
        token = create_access_token(principal, svc.settings)
        return {"access_token": token, "tenant_id": tenant_id, "role": "owner"}

    @app.post("/api/auth/login")
    def login(request: LoginRequest) -> dict:
        user = svc.user_store.get_user_by_email(request.email)
        if user and svc.user_store.verify_password(request.password, user.password_hash, user.salt):
            principal = Principal(principal_id=user.user_id, tenant_id=user.tenant_id, role=user.role)
            token = create_access_token(principal, svc.settings)
            return {"access_token": token, "tenant_id": user.tenant_id, "role": user.role}

        if svc.settings.admin_secret and secrets.compare_digest(request.password, svc.settings.admin_secret):
            principal = Principal.platform_admin(request.email or "admin")
            token = create_access_token(principal, svc.settings)
            return {"access_token": token, "tenant_id": None, "role": "platform_admin"}

        raise HTTPException(status_code=401, detail="Invalid email or password.")

    @app.get("/api/auth/me")
    def me(authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._require(authorization)
        return {"principal_id": principal.principal_id, "tenant_id": principal.tenant_id, "role": principal.role}
