"""Financial Truth Layer (Phase 12) owner/manager routes: view the
manual sales/expense/collection ledger's aggregated summary and export
it as CSV. Logging itself happens only over the admin WhatsApp bot's
`log sale/expense/collection <amount> [note]` commands (admin_bot.py) —
no HTTP write route, matching the "needs no permission check, any
roster member can do it" shape of feedback submission.
"""

from __future__ import annotations

import csv
import io

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse

from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_metrics(app: FastAPI, svc, ctx) -> None:
    @app.get("/api/metrics/summary")
    def get_metrics_summary(
        tenant_id: str, since: str | None = None, until: str | None = None,
        authorization: str | None = Header(default=None),
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FINANCIALS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        summary = svc.metric_store.summary_for_tenant(tenant_id, since_iso=since, until_iso=until)
        return {
            "summary": [s.model_dump() for s in summary],
            "source": "manual",  # every response labels itself — never implies a live POS/accounting sync
        }

    @app.get("/api/metrics/export.csv")
    def export_metrics_csv(
        tenant_id: str, since: str | None = None, until: str | None = None,
        authorization: str | None = Header(default=None),
    ):
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_FINANCIALS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        entries = svc.metric_store.list_for_tenant(tenant_id, since_iso=since, until_iso=until, limit=100000)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["metric_id", "metric_type", "amount_inr", "note", "reported_by_employee_id", "source", "created_at"])
        for m in entries:
            writer.writerow([m.metric_id, m.metric_type, m.amount_inr, m.note, m.reported_by_employee_id or "", m.source, m.created_at])
        buffer.seek(0)
        return StreamingResponse(
            iter([buffer.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="financials_{tenant_id}.csv"'},
        )
