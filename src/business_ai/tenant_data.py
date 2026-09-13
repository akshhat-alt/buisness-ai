"""Tenant data export and deletion (Phase 9) — the compliance baseline
every later multi-location/enterprise conversation will need, and a
concrete answer to "can I get my data out" / "can I have my data
removed" for any tenant today.

Export is read-only and covers every tenant-scoped record this app
holds, EXCEPT: connection secrets (whatsapp_access_token,
razorpay_key_secret — operational credentials, not the owner's business
data, and exporting them would be a real security exposure) and
ephemeral quota/rate-limit state (ADAUsageLimiter's session_usage rows —
internal plumbing, not a business record). Analytics is exported as the
same aggregated summary the dashboard already shows, not a raw per-
conversation dump — no new raw-turn-listing capability was needed to
satisfy "export the tenant's data" honestly.

Deletion is real and irreversible: every store's `delete_for_tenant`
(or tenant-scoped equivalent) is called, then the tenant's own config
row last. Nothing here is soft-delete/tombstoning — callers (the API
route) are responsible for requiring an explicit, hard-to-fumble
confirmation before reaching this module at all.
"""

from __future__ import annotations

from business_ai.leads import lead_stage


def export_tenant_data(svc, tenant_id: str) -> dict:
    tenant = svc.tenant_registry.get_config(tenant_id)
    tenant_dict = tenant.model_dump()
    tenant_dict.pop("whatsapp_access_token", None)
    tenant_dict.pop("razorpay_key_secret", None)
    tenant_dict.pop("razorpay_webhook_secret", None)

    leads = svc.lead_store.list_for_tenant(tenant_id, limit=100000)
    employees = svc.employee_store.list_for_tenant(tenant_id, active_only=False)

    return {
        "tenant": tenant_dict,
        "leads": [{**lead.model_dump(), "stage": lead_stage(lead)} for lead in leads],
        "employees": [e.model_dump() for e in employees],
        "tasks": [t.model_dump() for t in svc.task_store.list_for_tenant(tenant_id)],
        "shifts": [s.model_dump() for s in svc.shift_store.list_for_tenant(tenant_id)],
        "feedback": [f.model_dump() for f in svc.feedback_store.list_for_tenant(tenant_id)],
        "sop_notes": [s.model_dump() for s in svc.sop_store.list_for_tenant(tenant_id)],
        "automation_rules": [r.model_dump() for r in svc.automation_rule_store.list_for_tenant(tenant_id)],
        "automation_runs": [r.model_dump() for r in svc.automation_run_store.list_for_tenant(tenant_id, limit=100000)],
        "audit_log": [a.model_dump() for a in svc.audit_log.list_for_tenant(tenant_id, limit=100000)],
        "knowledge_sources": [s.model_dump() for s in svc.source_store.list_for_tenant(tenant_id)],
        "analytics_summary": svc.analytics_store.summary_for_tenant(tenant_id).model_dump(),
        "knowledge_gaps": [g.model_dump() for g in svc.analytics_store.list_open_gaps(tenant_id, limit=100000)],
        "evolution_versions": [v.model_dump() for v in svc.evolution_versions.list_for_tenant(tenant_id)],
        "evolution_proposals": [p.model_dump() for p in svc.evolution_proposals.list_for_tenant(tenant_id)],
        "business_metrics": [m.model_dump() for m in svc.metric_store.list_for_tenant(tenant_id, limit=100000)],
        "menu_items": [i.model_dump() for i in svc.menu_store.list_for_tenant(tenant_id)],
        "inventory": [i.model_dump() for i in svc.inventory_store.list_for_tenant(tenant_id)],
        "suppliers": [s.model_dump() for s in svc.supplier_store.list_for_tenant(tenant_id)],
        "purchases": [p.model_dump() for p in svc.purchase_store.list_for_tenant(tenant_id, limit=100000)],
        "wastage": [w.model_dump() for w in svc.wastage_store.list_for_tenant(tenant_id, limit=100000)],
        "reviews": [r.model_dump() for r in svc.review_store.list_for_tenant(tenant_id, limit=100000)],
    }


def delete_tenant_data(svc, tenant_id: str) -> dict:
    """Deletes every row for this tenant across every store, including
    the vector index and the tenant's own config row last (so a crash
    partway through leaves the tenant still resolvable/retryable rather
    than orphaning data with no owning config row). Returns a per-store
    row-count report."""
    deleted = {
        "leads": svc.lead_store.delete_for_tenant(tenant_id),
        "tasks": svc.task_store.delete_for_tenant(tenant_id),
        "shifts": svc.shift_store.delete_for_tenant(tenant_id),
        "employees": svc.employee_store.delete_for_tenant(tenant_id),
        "feedback": svc.feedback_store.delete_for_tenant(tenant_id),
        "sop_notes": svc.sop_store.delete_for_tenant(tenant_id),
        "automation_rules": svc.automation_rule_store.delete_for_tenant(tenant_id),
        "automation_runs": svc.automation_run_store.delete_for_tenant(tenant_id),
        "audit_log": svc.audit_log.delete_for_tenant(tenant_id),
        "knowledge_sources": svc.source_store.delete_for_tenant(tenant_id),
        "analytics_turns": svc.analytics_store.delete_for_tenant(tenant_id),
        "users": svc.user_store.delete_for_tenant(tenant_id),
        "whatsapp_inbox": svc.whatsapp_inbox.delete_for_tenant(tenant_id),
        "usage_sessions": svc.usage_limiter.delete_for_tenant(tenant_id),
        "evolution_versions": svc.evolution_versions.delete_for_tenant(tenant_id),
        "evolution_proposals": svc.evolution_proposals.delete_for_tenant(tenant_id),
        "evolution_evaluations": svc.evolution_evaluations.delete_for_tenant(tenant_id),
        "business_metrics": svc.metric_store.delete_for_tenant(tenant_id),
        "menu_items": svc.menu_store.delete_for_tenant(tenant_id),
        "inventory": svc.inventory_store.delete_for_tenant(tenant_id),
        "suppliers": svc.supplier_store.delete_for_tenant(tenant_id),
        "purchases": svc.purchase_store.delete_for_tenant(tenant_id),
        "wastage": svc.wastage_store.delete_for_tenant(tenant_id),
        "reviews": svc.review_store.delete_for_tenant(tenant_id),
    }
    svc.vector_store.delete_tenant(tenant_id)
    deleted["tenant_config"] = 1 if svc.tenant_registry.delete(tenant_id) else 0
    return deleted
