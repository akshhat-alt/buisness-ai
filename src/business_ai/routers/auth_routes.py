"""Health check + auth: signup, login, "who am I" (Phase 9 extraction
from app.py).
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import FastAPI, Header, HTTPException

from business_ai.alerts import render_password_reset_email
from business_ai.auth import Principal, create_access_token
from business_ai.email_sender import EmailSendError
from business_ai.formatting import _slugify_tenant_id
from business_ai.schemas import ForgotPasswordRequest, LoginRequest, ResetPasswordRequest, SignupRequest
from business_ai.security import InvalidTenantIdError, validate_tenant_id
from business_ai.tenant import TenantConfig, TenantStatus

logger = logging.getLogger(__name__)



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

    @app.post("/api/auth/forgot-password")
    def forgot_password(request: ForgotPasswordRequest) -> dict:
        # Note: IP rate-limiting or CAPTCHA on forgot-password is out of scope for this pass
        clean_email = request.email.strip().lower()
        generic_msg = {"message": "If an account with that email exists, we have sent a password reset link."}
        if not clean_email:
            return generic_msg

        user = svc.user_store.get_user_by_email(clean_email)
        if not user:
            return generic_msg

        token = svc.password_reset_store.create(user.user_id)
        base_url = (svc.settings.public_base_url or "http://localhost:8000").rstrip("/")
        reset_link = f"{base_url}/reset-password?token={token}"

        if svc.settings.resend_api_key and svc.settings.digest_from_email:
            subject, html = render_password_reset_email(user_name=user.name, reset_link=reset_link)
            try:
                svc.email_sender().send(to=user.email, subject=subject, html_body=html)
            except EmailSendError as exc:
                logger.warning("Failed to send password reset email to %s: %s", user.email, exc)

        return generic_msg

    @app.post("/api/auth/reset-password")
    def reset_password(request: ResetPasswordRequest) -> dict:
        token = request.token.strip() if request.token else ""
        if not token:
            raise HTTPException(status_code=400, detail="Reset token is required.")

        record = svc.password_reset_store.get(token)
        if not record:
            raise HTTPException(status_code=400, detail="Invalid or expired reset token.")

        if record.used_at is not None:
            raise HTTPException(status_code=400, detail="Reset token has already been used.")

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if record.expires_at <= now_iso:
            raise HTTPException(status_code=400, detail="Reset token has expired.")

        try:
            svc.user_store.update_password(record.user_id, request.new_password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        svc.password_reset_store.mark_used(token)
        return {"message": "Password has been reset successfully. You can now sign in with your new password."}

