"""Business Dependency Intelligence (Phase 10).

A live map of how the business actually runs — who depends on whom, what
breaks if a specific employee is unavailable — built entirely from data
this app already collects (task assignment/completion history, the
employee roster, SOP authorship, and lead-to-task linkage). Deliberately
NOT a new source of truth: every function here is a pure, deterministic
read over existing stores, computed fresh on every call, exactly the
same "derived read model that can never drift from the real data it
summarizes" pattern already used by `_business_health_snapshot` in
routers/admin_bot.py. No graph database, no new table — an adjacency
view built in memory, which is plenty at the tens-of-employees/
thousands-of-tasks scale this app runs at today (see ARCHITECTURE.md's
"Scaling past one instance" for the actual trigger point to revisit
that, which is nowhere near here yet).

Core concept: a "process" is a recurring task TYPE, identified by its
exact (case/whitespace-normalized) title — "restock shelf 3" done eight
times is one process with eight instances. This reuses the exact
normalization idiom `_find_task_by_title_fragment` already applies to
task titles elsewhere in this codebase, rather than inventing a new
workflow/process entity this app has no other model for. A task title
that has only ever appeared once is a one-off, not a process, and is
excluded from process-level risk scoring (see `min_process_occurrences`).

Every number this module produces is explainable back to the real rows
that produced it — never an opaque index — matching the same
transparency rule `_business_health_snapshot` already holds itself to.
"""

from __future__ import annotations

from collections import Counter

from business_ai.tasks import OPEN_STATUSES, Task

# A task title that has appeared fewer than this many times isn't a
# recurring "process" yet — a one-off doesn't have a bus factor, it just
# hasn't happened again.
DEFAULT_MIN_PROCESS_OCCURRENCES = 2

# A single employee holding more than this share of a resource (open
# task load, SOP authorship) is flagged as a concentration risk. Chosen
# as "clearly more than an even split across a small team," not tuned to
# any specific roster size — see the roster-size floor below for why a
# tiny team doesn't trip this on every metric trivially.
CONCENTRATION_RISK_THRESHOLD_PCT = 60

# Below this many active employees, "one person holds 100% of X" is true
# almost by construction (a 2-person team always has >=50% concentration
# on SOMETHING) and saying so isn't a useful risk signal — it's just
# arithmetic. Concentration risks are suppressed below this roster size;
# bus-factor-1 PROCESS risk is still reported regardless of roster size,
# since "only one person has ever done this" is meaningful even on a
# 2-person team.
MIN_ROSTER_SIZE_FOR_CONCENTRATION_RISK = 3


def _normalize_title(title: str) -> str:
    return " ".join(title.strip().lower().split())


def _employee_label(employees_by_id: dict, employee_id: str | None) -> str:
    if not employee_id:
        return "(unassigned)"
    employee = employees_by_id.get(employee_id)
    return employee.name if employee else "(former employee)"


def _group_tasks_by_process(tasks: list[Task]) -> dict[str, list[Task]]:
    groups: dict[str, list[Task]] = {}
    for task in tasks:
        key = _normalize_title(task.title)
        groups.setdefault(key, []).append(task)
    return groups


def _process_summary(display_title: str, instances: list[Task], employees_by_id: dict) -> dict:
    done = [t for t in instances if t.status == "done"]
    # "Capable" = has actually finished this at least once. If nothing's
    # been finished yet (a new recurring task type still in flight),
    # fall back to who's been assigned it at all, rather than reporting
    # a false zero for a real, already-recurring process.
    capable_source = done if done else instances
    capable_employee_ids = {t.assigned_to_employee_id for t in capable_source}
    assignee_counts = Counter(t.assigned_to_employee_id for t in instances)
    top_employee_id, top_count = assignee_counts.most_common(1)[0]
    return {
        "title": display_title,
        "instance_count": len(instances),
        "done_count": len(done),
        "bus_factor": len(capable_employee_ids),
        "capable_employee_ids": sorted(capable_employee_ids),
        "capable_employee_names": sorted(_employee_label(employees_by_id, eid) for eid in capable_employee_ids),
        "top_employee_id": top_employee_id,
        "top_employee_name": _employee_label(employees_by_id, top_employee_id),
        "dependency_score_pct": round(100 * top_count / len(instances)),
    }


def compute_dependency_snapshot(
    tenant_id: str,
    *,
    employee_store,
    task_store,
    sop_store,
    lead_store,
    min_process_occurrences: int = DEFAULT_MIN_PROCESS_OCCURRENCES,
) -> dict:
    """The full Business Map: processes and their bus factor, workload
    concentration, knowledge (SOP-authorship) concentration, customer
    concentration, and a consolidated, severity-ordered risk list. Every
    field traces back to a real row — see the module docstring."""
    employees = employee_store.list_for_tenant(tenant_id, active_only=True)
    employees_by_id = {e.employee_id: e for e in employees}
    tasks = task_store.list_for_tenant(tenant_id)

    # ---- processes / bus factor -------------------------------------------------
    process_groups = _group_tasks_by_process(tasks)
    processes = []
    for instances in process_groups.values():
        if len(instances) < min_process_occurrences:
            continue
        display_title = instances[0].title.strip()
        processes.append(_process_summary(display_title, instances, employees_by_id))
    processes.sort(key=lambda p: (p["bus_factor"], -p["instance_count"]))
    single_point_of_failure_processes = [p for p in processes if p["bus_factor"] <= 1]

    # ---- workload concentration --------------------------------------------------
    open_tasks = [t for t in tasks if t.status in OPEN_STATUSES]
    workload_counts = Counter(t.assigned_to_employee_id for t in open_tasks)
    workload_concentration = None
    if workload_counts:
        top_employee_id, top_count = workload_counts.most_common(1)[0]
        workload_concentration = {
            "employee_id": top_employee_id,
            "employee_name": _employee_label(employees_by_id, top_employee_id),
            "open_task_count": top_count,
            "total_open_tasks": len(open_tasks),
            "share_pct": round(100 * top_count / len(open_tasks)),
        }

    # ---- knowledge (SOP authorship) concentration ---------------------------------
    sop_notes = sop_store.list_for_tenant(tenant_id)
    sop_author_counts = Counter(n.approved_by_employee_id for n in sop_notes)
    knowledge_concentration = None
    if sop_author_counts:
        top_author_id, top_author_count = sop_author_counts.most_common(1)[0]
        knowledge_concentration = {
            "employee_id": top_author_id,
            "employee_name": _employee_label(employees_by_id, top_author_id),
            "authored_count": top_author_count,
            "total_sop_notes": len(sop_notes),
            "share_pct": round(100 * top_author_count / len(sop_notes)),
        }

    # ---- customer concentration ----------------------------------------------------
    leads = lead_store.list_for_tenant(tenant_id, limit=5000)
    tasks_by_lead: dict[str, list[Task]] = {}
    for task in tasks:
        if task.customer_facing_lead_id:
            tasks_by_lead.setdefault(task.customer_facing_lead_id, []).append(task)
    sole_contact_leads = []
    for lead in leads:
        linked = tasks_by_lead.get(lead.lead_id)
        if not linked:
            continue
        assignees = {t.assigned_to_employee_id for t in linked}
        if len(assignees) == 1:
            (sole_employee_id,) = assignees
            sole_contact_leads.append({
                "lead_id": lead.lead_id,
                "lead_name": lead.name or lead.phone or lead.lead_id,
                "employee_id": sole_employee_id,
                "employee_name": _employee_label(employees_by_id, sole_employee_id),
            })

    # ---- consolidated, severity-ordered risk list -----------------------------------
    risks: list[dict] = []
    for p in single_point_of_failure_processes:
        who = p["capable_employee_names"][0] if p["capable_employee_names"] else "no one currently active"
        risks.append({
            "severity": "high",
            "type": "single_point_of_failure_process",
            "target_id": f"process:{_normalize_title(p['title'])}",
            "summary": f'"{p["title"]}" has only ever been handled by {who} — {p["instance_count"]} time(s), no one else.',
            "employee_id": p["capable_employee_ids"][0] if p["capable_employee_ids"] else None,
        })

    enough_roster = len(employees) >= MIN_ROSTER_SIZE_FOR_CONCENTRATION_RISK
    if enough_roster and workload_concentration and workload_concentration["share_pct"] >= CONCENTRATION_RISK_THRESHOLD_PCT:
        wc = workload_concentration
        risks.append({
            "severity": "medium",
            "type": "workload_concentration",
            "target_id": "workload_concentration",
            "summary": f'{wc["employee_name"]} holds {wc["share_pct"]}% of all open tasks ({wc["open_task_count"]}/{wc["total_open_tasks"]}).',
            "employee_id": wc["employee_id"],
        })
    if enough_roster and knowledge_concentration and knowledge_concentration["share_pct"] >= CONCENTRATION_RISK_THRESHOLD_PCT:
        kc = knowledge_concentration
        risks.append({
            "severity": "low",
            "type": "knowledge_concentration",
            "target_id": "knowledge_concentration",
            "summary": f'{kc["employee_name"]} has authored {kc["share_pct"]}% of approved team guidance ({kc["authored_count"]}/{kc["total_sop_notes"]}) — still written down, but worth spreading out.',
            "employee_id": kc["employee_id"],
        })
    if sole_contact_leads:
        risks.append({
            "severity": "medium",
            "type": "customer_concentration",
            "target_id": "customer_concentration",
            "summary": f"{len(sole_contact_leads)} customer(s) have only ever been handled by one employee each.",
            "employee_id": None,
        })
    severity_order = {"high": 0, "medium": 1, "low": 2}
    risks.sort(key=lambda r: severity_order.get(r["severity"], 9))

    return {
        "roster_size": len(employees),
        "processes": processes,
        "single_point_of_failure_processes": single_point_of_failure_processes,
        "workload_concentration": workload_concentration,
        "knowledge_concentration": knowledge_concentration,
        "sole_contact_leads": sole_contact_leads,
        "risks": risks,
    }


def simulate_employee_unavailable(
    tenant_id: str,
    employee_id: str,
    *,
    employee_store,
    task_store,
    sop_store,
    lead_store,
) -> dict:
    """"What breaks if this employee is unavailable?" — a deterministic
    graph traversal, not a prediction: every item returned is something
    that is ALREADY true today (an open task assigned to them, a process
    only they've ever completed, a customer only they've ever handled),
    just reframed around "what happens if they're the one who's out."
    """
    employee = employee_store.get(tenant_id, employee_id)
    tasks = task_store.list_for_tenant(tenant_id)

    own_open_tasks = [
        t for t in tasks if t.assigned_to_employee_id == employee_id and t.status in OPEN_STATUSES
    ]

    process_groups = _group_tasks_by_process(tasks)
    orphaned_processes = []
    for instances in process_groups.values():
        if len(instances) < DEFAULT_MIN_PROCESS_OCCURRENCES:
            continue
        done = [t for t in instances if t.status == "done"]
        capable_source = done if done else instances
        capable_employee_ids = {t.assigned_to_employee_id for t in capable_source}
        if capable_employee_ids == {employee_id}:
            orphaned_processes.append({
                "title": instances[0].title.strip(),
                "instance_count": len(instances),
            })

    leads = lead_store.list_for_tenant(tenant_id, limit=5000)
    tasks_by_lead: dict[str, list[Task]] = {}
    for task in tasks:
        if task.customer_facing_lead_id:
            tasks_by_lead.setdefault(task.customer_facing_lead_id, []).append(task)
    sole_contact_leads = []
    for lead in leads:
        linked = tasks_by_lead.get(lead.lead_id)
        if not linked:
            continue
        assignees = {t.assigned_to_employee_id for t in linked}
        if assignees == {employee_id}:
            sole_contact_leads.append({"lead_id": lead.lead_id, "lead_name": lead.name or lead.phone or lead.lead_id})

    sop_notes = sop_store.list_for_tenant(tenant_id)
    authored_sop_themes = [n.theme for n in sop_notes if n.approved_by_employee_id == employee_id]

    return {
        "employee_id": employee_id,
        "employee_name": employee.name if employee else "(former employee)",
        "orphaned_open_tasks": [{"task_id": t.task_id, "title": t.title, "due_at": t.due_at} for t in own_open_tasks],
        "orphaned_processes": orphaned_processes,
        "sole_contact_leads": sole_contact_leads,
        "authored_sop_themes": authored_sop_themes,
        "is_currently_a_risk": bool(own_open_tasks or orphaned_processes or sole_contact_leads),
    }
