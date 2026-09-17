"""Self-Evolution Infrastructure (Phase 11) owner controls: proposal
review/approve/reject, version history, manual rollback, and the
per-tenant kill switch — plus the two platform_admin cron endpoints that
drive the pipeline (scan for a new proposal + sandbox-evaluate it, and
monitor an already-promoted version for regression + auto-rollback).
Mirrors dependency_routes.py/automation_routes.py's own shape.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException

from business_ai.constants import (
    EVOLUTION_FAILURE_DISSATISFACTION_RATE_THRESHOLD,
    EVOLUTION_LOOKBACK_HOURS,
    EVOLUTION_MONITORING_REGRESSION_DELTA,
)
from business_ai.evolution import generate_behavior_proposal, run_monitoring_check, run_sandbox_evaluation
from business_ai.schemas import EvolutionKillSwitchRequest
from business_ai.tenant import TenantAction, TenantNotFoundError, TenantStatus, UnauthorizedError, authorize

logger = logging.getLogger(__name__)


def register_evolution(app: FastAPI, svc, ctx) -> None:
    @app.get("/api/evolution/versions")
    def list_evolution_versions(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EVOLUTION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        versions = svc.evolution_versions.list_for_tenant(tenant_id, config_type="assistant_tone")
        return {"versions": [v.model_dump() for v in versions]}

    @app.get("/api/evolution/proposals")
    def list_evolution_proposals(tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EVOLUTION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        proposals = svc.evolution_proposals.list_for_tenant(tenant_id)
        out = []
        for p in proposals:
            candidate = svc.evolution_versions.get(tenant_id, p.candidate_version_id)
            evaluation = svc.evolution_evaluations.latest_for_version(tenant_id, p.candidate_version_id)
            out.append({
                **p.model_dump(),
                "candidate_version": candidate.model_dump() if candidate else None,
                "latest_evaluation": evaluation.model_dump() if evaluation else None,
            })
        return {"proposals": out}

    @app.post("/api/evolution/proposals/{proposal_id}/approve")
    def approve_evolution_proposal(proposal_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Gated promotion: refuses to activate anything that hasn't
        passed sandbox evaluation. This is the ONE place a self-evolution
        version ever becomes live for real customers, and it always
        requires an explicit, authenticated owner action — there is no
        code path that auto-promotes a proposal."""
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EVOLUTION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        proposal = svc.evolution_proposals.get(tenant_id, proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="Unknown proposal_id for this business.")
        if proposal.status != "pending_owner_review":
            raise HTTPException(
                status_code=400,
                detail=f"Proposal is '{proposal.status}', not ready for approval (must pass sandbox evaluation first).",
            )
        svc.evolution_versions.activate(tenant_id, proposal.candidate_version_id)
        svc.evolution_proposals.set_status(tenant_id, proposal_id, "approved", decided_by=principal.principal_id)
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="evolution_version_approved",
            target_type="evolution_version", target_id=proposal.candidate_version_id,
            metadata={"proposal_id": proposal_id},
        )
        return {"status": "approved", "active_version_id": proposal.candidate_version_id}

    @app.post("/api/evolution/proposals/{proposal_id}/reject")
    def reject_evolution_proposal(proposal_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EVOLUTION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        proposal = svc.evolution_proposals.get(tenant_id, proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="Unknown proposal_id for this business.")
        if proposal.status in ("approved", "rejected"):
            raise HTTPException(status_code=400, detail=f"Proposal is already '{proposal.status}'.")
        svc.evolution_proposals.set_status(tenant_id, proposal_id, "rejected", decided_by=principal.principal_id)
        svc.evolution_versions.set_status(tenant_id, proposal.candidate_version_id, "rejected")
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="evolution_proposal_rejected",
            target_type="evolution_proposal", target_id=proposal_id, metadata={},
        )
        return {"status": "rejected"}

    @app.post("/api/evolution/versions/{version_id}/rollback")
    def rollback_evolution_version(version_id: str, tenant_id: str, authorization: str | None = Header(default=None)) -> dict:
        """Manual rollback — an owner can do this at any time, for any
        reason, to any of their own tenant's prior versions, independent
        of the automatic monitoring check below."""
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EVOLUTION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        target = svc.evolution_versions.get(tenant_id, version_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Unknown version_id for this business.")
        restored = svc.evolution_versions.rollback_to(tenant_id, version_id)
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="evolution_version_rolled_back_manual",
            target_type="evolution_version", target_id=version_id, metadata={"actor": principal.principal_id},
        )
        return {"active_version_id": restored.version_id}

    @app.post("/api/tenant/evolution-toggle")
    def set_evolution_kill_switch(
        request: EvolutionKillSwitchRequest, tenant_id: str, authorization: str | None = Header(default=None)
    ) -> dict:
        """Owner-only, instant, tenant-wide opt-in/out. See TenantConfig.
        evolution_enabled's own docstring for why the default is False,
        unlike automation_enabled's default True."""
        principal = ctx._resolve(authorization)
        try:
            authorize(principal, TenantAction.MANAGE_EVOLUTION, target_tenant_id=tenant_id, registry=svc.tenant_registry)
        except UnauthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TenantNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        updated = svc.tenant_registry.update_config(tenant_id, evolution_enabled=request.enabled)
        svc.audit_log.record(
            tenant_id=tenant_id, actor_employee_id=None, action="evolution_kill_switch",
            target_type="tenant", target_id=tenant_id, metadata={"state": "on" if request.enabled else "off"},
        )
        return {"evolution_enabled": updated.evolution_enabled}

    # -------------------------------------------------------------- crons
    @app.post("/api/v1/admin/evolution-scan/run")
    def admin_run_evolution_scan(authorization: str | None = Header(default=None)) -> dict:
        """Observe -> failure detection -> proposal -> sandbox evaluation,
        for every ACTIVE, evolution_enabled tenant, meant for the same
        external daily cron convention as every other admin/*/run
        endpoint. Never promotes anything — sandbox evaluation only
        determines whether a proposal becomes eligible for the OWNER to
        approve (see approve_evolution_proposal above). At most one
        proposal in flight per tenant at a time."""
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        proposed: list[dict] = []
        skipped: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                continue
            if not tenant.evolution_enabled:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "evolution kill switch is off"})
                continue
            in_flight = svc.evolution_proposals.list_for_tenant(tenant.tenant_id, status="pending_sandbox")
            in_flight += svc.evolution_proposals.list_for_tenant(tenant.tenant_id, status="pending_owner_review")
            if in_flight:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "a proposal is already awaiting owner review"})
                continue

            generator = svc.generator()
            proposal = generate_behavior_proposal(
                tenant.tenant_id, analytics_store=svc.analytics_store,
                evolution_version_store=svc.evolution_versions, evolution_proposal_store=svc.evolution_proposals,
                lookback_hours=tenant.evolution_lookback_hours or EVOLUTION_LOOKBACK_HOURS,
                dissatisfaction_rate_threshold=(
                    tenant.evolution_dissatisfaction_threshold or EVOLUTION_FAILURE_DISSATISFACTION_RATE_THRESHOLD
                ),
                generator=generator,
            )
            if proposal is None:
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "no failure signal detected"})
                continue

            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=None, action="evolution_proposal_created",
                target_type="evolution_proposal", target_id=proposal.proposal_id,
                metadata={"trigger_reason": proposal.trigger_reason},
            )
            try:
                evaluation = run_sandbox_evaluation(
                    tenant.tenant_id, proposal, evolution_version_store=svc.evolution_versions,
                    evolution_evaluation_store=svc.evolution_evaluations, analytics_store=svc.analytics_store,
                    vector_store=svc.vector_store, embeddings=svc.embeddings(), generator=generator,
                    business_name=tenant.business_name, assistant_name=tenant.assistant_name,
                )
            except Exception:
                logger.exception("Sandbox evaluation failed for tenant %s proposal %s", tenant.tenant_id, proposal.proposal_id)
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "sandbox evaluation errored"})
                continue

            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=None, action="evolution_sandbox_evaluated",
                target_type="evolution_version", target_id=proposal.candidate_version_id,
                metadata={"verdict": evaluation.verdict, "metrics": evaluation.metrics},
            )
            if evaluation.verdict == "pass":
                svc.evolution_proposals.set_status(tenant.tenant_id, proposal.proposal_id, "pending_owner_review")
            else:
                svc.evolution_proposals.set_status(tenant.tenant_id, proposal.proposal_id, "sandbox_failed")
            proposed.append({
                "tenant_id": tenant.tenant_id, "proposal_id": proposal.proposal_id, "verdict": evaluation.verdict,
            })
        return {"proposed": proposed, "skipped": skipped}

    @app.post("/api/v1/admin/evolution-monitor/run")
    def admin_run_evolution_monitor(authorization: str | None = Header(default=None)) -> dict:
        """Monitoring + automatic rollback: for every ACTIVE tenant whose
        currently active assistant_tone version was itself promoted from
        a prior one (parent_version_id is set — i.e. this isn't just a
        tenant with no self-evolution history), compares post-activation
        dissatisfaction rate against the pre-activation baseline and
        rolls back automatically on a real regression. Fail-closed and
        LLM-free by design — this safety net must keep working even
        during an OpenAI outage."""
        principal = ctx._require(authorization)
        if principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="Platform admin only.")

        rolled_back: list[dict] = []
        skipped: list[dict] = []
        for tenant in svc.tenant_registry.list_all():
            if tenant.status != TenantStatus.ACTIVE:
                continue
            active_version = svc.evolution_versions.get_active(tenant.tenant_id, "assistant_tone")
            if active_version is None:
                continue
            result = run_monitoring_check(
                tenant.tenant_id, active_version, analytics_store=svc.analytics_store,
                regression_delta=tenant.evolution_regression_delta or EVOLUTION_MONITORING_REGRESSION_DELTA,
                lookback_hours=tenant.evolution_lookback_hours or EVOLUTION_LOOKBACK_HOURS,
            )
            if result["action"] == "skipped":
                skipped.append({"tenant_id": tenant.tenant_id, "reason": result["reason"]})
                continue
            if result["action"] == "ok":
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "no regression", "metrics": result["metrics"]})
                continue
            # action == "regression" — one tenant's rollback must never
            # abort the loop for every other tenant still to be checked.
            try:
                restored = svc.evolution_versions.rollback_to(tenant.tenant_id, active_version.parent_version_id)
            except Exception:
                logger.exception("Automatic rollback failed for tenant %s version %s", tenant.tenant_id, active_version.version_id)
                skipped.append({"tenant_id": tenant.tenant_id, "reason": "rollback_failed"})
                continue
            svc.audit_log.record(
                tenant_id=tenant.tenant_id, actor_employee_id=None, action="evolution_version_rolled_back_auto",
                target_type="evolution_version", target_id=active_version.version_id,
                metadata={"restored_version_id": restored.version_id, "metrics": result["metrics"]},
            )
            ctx._notify_management_whatsapp(
                tenant,
                "⚠️ Bizistic automatically rolled back a self-evolved assistant tone change after detecting "
                f"a rise in customer dissatisfaction (from {result['metrics']['baseline_rate']:.0%} to "
                f"{result['metrics']['post_rate']:.0%}). Your assistant is back to its previous behavior.",
            )
            rolled_back.append({"tenant_id": tenant.tenant_id, "metrics": result["metrics"]})
        return {"rolled_back": rolled_back, "skipped": skipped}
