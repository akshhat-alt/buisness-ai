"""Production readiness routes (Phase 14): platform_admin-only backup
trigger/listing and a detailed health check, plus a read-only data-
integrity report. Complements the bare `/healthz` liveness probe with
real operational visibility — whether backups are actually happening,
whether every core store is actually reachable, and whether any
cross-store reference has silently gone orphaned.
"""

from __future__ import annotations

import sqlite3
import time

from fastapi import FastAPI, Header, HTTPException

from business_ai.ops import check_data_integrity, create_backup, list_backups


def register_ops(app: FastAPI, svc, ctx) -> None:
    @app.post("/api/v1/admin/backup/run")
    def admin_run_backup(authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")
        try:
            result = create_backup(svc.data_root, svc.backup_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return result.model_dump()

    @app.get("/api/v1/admin/backup/list")
    def admin_list_backups(authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")
        return {"backups": list_backups(svc.backup_dir)}

    @app.get("/api/v1/admin/health/detailed")
    def admin_detailed_health(authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        store_checks: dict[str, str] = {}
        for name, store in (
            ("tenants", svc.tenant_registry), ("leads", svc.lead_store), ("tasks", svc.task_store),
            ("employees", svc.employee_store), ("analytics", svc.analytics_store), ("audit", svc.audit_log),
            ("metrics", svc.metric_store),
        ):
            try:
                with store._lock, store._db() as conn:
                    conn.execute("SELECT 1").fetchone()
                store_checks[name] = "ok"
            except sqlite3.Error as exc:
                store_checks[name] = f"error: {exc}"

        try:
            svc.vector_store.count_for_tenant("__healthcheck__")
            vector_store_status = "ok"
        except Exception as exc:  # noqa: BLE001 - health check must report, never crash
            vector_store_status = f"error: {exc}"

        backups = list_backups(svc.backup_dir)
        last_backup = backups[-1] if backups else None

        disk = None
        try:
            usage = __import__("shutil").disk_usage(svc.data_root)
            disk = {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
        except OSError:
            pass

        return {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stores": store_checks,
            "vector_store": vector_store_status,
            "last_backup": last_backup,
            "backup_count": len(backups),
            "disk": disk,
        }

    @app.get("/api/v1/admin/data-integrity")
    def admin_data_integrity(authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")
        return check_data_integrity(
            employee_store=svc.employee_store, task_store=svc.task_store, sop_store=svc.sop_store,
            lead_store=svc.lead_store, tenant_registry=svc.tenant_registry,
        )
