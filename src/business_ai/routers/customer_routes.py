"""The customer-facing chat endpoint (Phase 9 extraction from app.py) —
the one grounded-answer pipeline both the website widget and the
WhatsApp webhook (routers/admin_bot.py) call, via ctx._process_question.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Header, HTTPException, Request

from business_ai.formatting import _whatsapp_link
from business_ai.schemas import AskRequest
from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize
from business_ai.usage_limiter import AccessDecision


def register_customer(app: FastAPI, svc, ctx) -> None:
    @app.post("/api/ask")
    def ask(
        request: AskRequest,
        req: Request,
        tenant_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.QUERY_ASSISTANT, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        session_id = request.session_id or f"sess_{secrets.token_hex(8)}"

        res, answer = ctx._process_question(
            tenant=tenant, tenant_id=tenant_id, session_id=session_id, query=request.query, channel="web",
        )
        if res.decision != AccessDecision.ALLOW:
            status_code = 429 if res.decision == AccessDecision.RATE_LIMITED else 400
            raise HTTPException(
                status_code=status_code,
                detail={"error": res.reason, "questions_remaining": res.questions_remaining, "questions_limit": res.questions_limit},
            )
        assert answer is not None  # guaranteed by res.decision == ALLOW

        quota_status = svc.usage_limiter.get_session_status(tenant_id, session_id, quota_override=tenant.question_quota)
        whatsapp_url = None
        if answer.suggested_handoff or answer.status.value == "insufficient_evidence":
            wa_number = tenant.whatsapp_number or svc.settings.default_whatsapp_number
            whatsapp_url = _whatsapp_link(wa_number, f"Hi, I was chatting with your assistant about: {request.query}")

        return {
            "session_id": session_id,
            "status": answer.status.value,
            "answer_text": answer.answer_text,
            "abstention_reason": answer.abstention_reason,
            "citations_used": [c.model_dump() for c in answer.citations_used],
            "shows_buying_intent": answer.shows_buying_intent,
            "suggested_handoff": answer.suggested_handoff,
            "shows_dissatisfaction": answer.shows_dissatisfaction,
            "whatsapp_url": whatsapp_url,
            "questions_remaining": quota_status["questions_remaining"],
            "questions_limit": quota_status["questions_limit"],
        }
