"""Ask Your Business Anything (Phase 26) — dashboard-facing NL query
bar. Reuses generation.py's classify_employee_message() report_request
intent (the SAME classifier and taxonomy admin_bot.py's WhatsApp NL
fallback already uses) so a question means the same thing on both
channels, then answers via business_query.py's per-report-type dispatch
— every answer is an ALREADY-EXISTING computed report, never a new one.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from business_ai.business_query import NO_STANDALONE_RENDERER_HINTS, REPORT_TYPE_SPECS
from business_ai.tenant import TenantNotFoundError, UnauthorizedError, authorize


class BusinessQueryRequest(BaseModel):
    question: str


def register_business_query(app: FastAPI, svc, ctx) -> None:
    @app.post("/api/business-query")
    def post_business_query(request: BusinessQueryRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._require(authorization)
        question = request.question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="question must not be empty.")

        try:
            classification = svc.generator().classify_employee_message(
                text=question, current_date_iso=time.strftime("%Y-%m-%d", time.gmtime()), employee_role=principal.role,
            )
        except Exception:  # noqa: BLE001 - best-effort classification, never crash the dashboard over it
            return {"report_type": None, "answer": "Couldn't understand that question — try rephrasing it, or check the dashboard sections directly."}

        report_type = classification.report_type if classification.intent == "report_request" else None
        if report_type is None:
            return {"report_type": None, "answer": "I couldn't match that to a report this app can pull yet. Try asking about food cost, reorder suggestions, reservations, repeat customers, supplier spend, reviews, or revenue leakage."}

        spec = REPORT_TYPE_SPECS.get(report_type)
        if spec is None:
            hint = NO_STANDALONE_RENDERER_HINTS.get(report_type, "That report isn't available from this query bar yet.")
            return {"report_type": report_type, "answer": hint}

        try:
            authorize(principal, spec.action, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return {"report_type": report_type, "answer": spec.answer(tenant_id, svc)}
