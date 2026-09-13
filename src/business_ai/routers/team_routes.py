"""Employee roster and task assignment/lifecycle (Phase 9 extraction from
app.py) — the dashboard/API-side equivalent of the WhatsApp admin-bot
commands in routers/admin_bot.py.
"""

from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from business_ai.employees import normalize_whatsapp_number
from business_ai.formatting import _parse_appointment_to_utc, _short_task_id
from business_ai.schemas import CreateEmployeeRequest, CreateTaskRequest, RejectTaskRequest, UpdateEmployeeRequest
from business_ai.tenant import TenantAction, TenantNotFoundError, UnauthorizedError, authorize


def register_team(app: FastAPI, svc, ctx) -> None:
    # -------------------------------------------------------------- employees & tasks (admin WhatsApp bot)

    @app.post("/api/employees")
    def create_employee(request: CreateEmployeeRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EMPLOYEES, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        try:
            employee = svc.employee_store.add(
                tenant_id=tenant_id, whatsapp_number=request.whatsapp_number, name=request.name, role=request.role,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="employee_added",
            target_type="employee", target_id=employee.employee_id, metadata={"role": employee.role},
        )
        return employee.model_dump()

    @app.get("/api/employees")
    def list_employees(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EMPLOYEES, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"employees": [e.model_dump() for e in svc.employee_store.list_for_tenant(tenant_id, active_only=False)]}

    @app.put("/api/employees/{employee_id}")
    def update_employee(
        employee_id: str, request: UpdateEmployeeRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EMPLOYEES, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        updated = None
        if request.role is not None:
            try:
                updated = svc.employee_store.set_role(tenant_id, employee_id, request.role)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            svc.audit_log.record(
                tenant_id=tenant_id, actor_employee_id=None, action="role_changed",
                target_type="employee", target_id=employee_id, metadata={"role": request.role},
            )
        if request.active is False:
            updated = svc.employee_store.deactivate(tenant_id, employee_id)
            svc.audit_log.record(
                tenant_id=tenant_id, actor_employee_id=None, action="employee_deactivated",
                target_type="employee", target_id=employee_id,
            )
        if updated is None:
            updated = svc.employee_store.get(tenant_id, employee_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Unknown employee_id for this business.")
        return updated.model_dump()

    @app.post("/api/tasks")
    def create_task(request: CreateTaskRequest, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Dashboard-side task creation — the WhatsApp `assign` command's
        equivalent for a caller that already knows the exact
        assigned_to_employee_id (e.g. from GET /api/employees) rather
        than typing a name to fuzzy-match. The only way today to set
        customer_facing_lead_id (linking a task to the lead/conversation
        it originated from) without going through the store directly,
        which is what enables the verified-outcome ping on completion."""
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.ASSIGN_TASK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        assignee = svc.employee_store.get(tenant_id, request.assigned_to_employee_id)
        if assignee is None:
            raise HTTPException(status_code=404, detail="Unknown assigned_to_employee_id for this business.")
        if request.customer_facing_lead_id and svc.lead_store.get(tenant_id, request.customer_facing_lead_id) is None:
            raise HTTPException(status_code=404, detail="Unknown customer_facing_lead_id for this business.")

        due_at = None
        if request.due_at:
            try:
                due_at = _parse_appointment_to_utc(request.due_at)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid due_at: {exc}") from exc

        task = svc.task_store.create(
            tenant_id=tenant_id, title=request.title, description=request.description,
            assigned_to_employee_id=assignee.employee_id, assigned_by_employee_id=principal.principal_id,
            due_at=due_at, approval_required=request.approval_required,
            customer_facing_lead_id=request.customer_facing_lead_id,
        )
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="task_assigned",
            target_type="task", target_id=task.task_id, metadata={"assigned_to": assignee.employee_id, "via": "api"},
        )
        due_note = f" — due {due_at[:16].replace('T', ' ')} UTC" if due_at else ""
        ctx._send_admin_bot_message(
            tenant, assignee.whatsapp_number,
            f'📋 New task: "{task.title}"{due_note}. Reply "done {_short_task_id(task.task_id)}" when finished.',
        )
        return task.model_dump()

    @app.get("/api/tasks")
    def list_tasks(tenant_id: str, authorization: str | None = Header(default=None), status: str | None = None) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.VIEW_TASKS, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"tasks": [t.model_dump() for t in svc.task_store.list_for_tenant(tenant_id, status=status)]}

    @app.post("/api/tasks/{task_id}/approve")
    def approve_task(task_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Dashboard-side equivalent of the WhatsApp "approve <id>"
        command (admin_bot.py) — same ASSIGN_TASK gate the WhatsApp
        handler's own can_manage check enforces (owner+manager, never
        staff), same store call, same audit action, same verified-
        outcome customer ping when the task is linked to a real lead.
        Phase 21's Approval Inbox is the first caller of this route, but
        it's a general-purpose dashboard action, not inbox-specific."""
        principal = ctx._resolve(authorization)
        try:
            tenant = authorize(principal, TenantAction.ASSIGN_TASK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        task = svc.task_store.get(tenant_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Unknown task_id for this business.")
        updated = svc.task_store.approve(tenant_id, task_id, principal.principal_id)
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="task_approved",
            target_type="task", target_id=task_id, metadata={"via": "api"},
        )
        ctx._maybe_verify_outcome_with_customer(tenant, updated)
        return updated.model_dump()

    @app.post("/api/tasks/{task_id}/reject")
    def reject_task(
        task_id: str, request: RejectTaskRequest, tenant_id: str, authorization: str | None = Header(default=None),
    ) -> dict:
        """Dashboard-side equivalent of the WhatsApp "reject <id> <reason>"
        command — sends the task back to in_progress, never cancelled."""
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.ASSIGN_TASK, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        task = svc.task_store.get(tenant_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Unknown task_id for this business.")
        updated = svc.task_store.reject(tenant_id, task_id, reason=request.reason)
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="task_rejected",
            target_type="task", target_id=task_id, metadata={"reason": request.reason, "via": "api"},
        )
        return updated.model_dump()
