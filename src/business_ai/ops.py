"""Production readiness: backup/restore and data-integrity checking
(Phase 14). Business AI runs as SQLite + local Chroma on a single
instance (see ARCHITECTURE.md's "Scaling past one instance") — until
that changes, a lost or corrupted volume has no recovery path unless
something outside the app itself is backing it up. This module is that
something: a plain tar.gz snapshot of the whole data directory, restore
with a safety net (the current data dir is moved aside, never deleted,
before a restore overwrites it), and a read-only integrity scan for
orphaned cross-store references that would otherwise fail silently.
"""

from __future__ import annotations

import shutil
import tarfile
import time
from pathlib import Path

from pydantic import BaseModel

from business_ai.config import Settings


class BackupResult(BaseModel):
    archive_path: str
    size_bytes: int
    created_at: str


def create_backup(data_root: Path, backup_dir: Path) -> BackupResult:
    """Snapshots the entire data directory (every tenant's SQLite files
    plus the Chroma vector store) into one timestamped tar.gz. Simple on
    purpose: no incremental/differential logic, no external service —
    the correctness bar for "can we get the data back" is met by "a
    plain, verifiable archive exists," not by a clever backup format."""
    data_root = Path(data_root)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    if not data_root.is_dir():
        raise FileNotFoundError(f"Data directory does not exist: {data_root}")

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive_path = backup_dir / f"business_ai_backup_{timestamp}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(data_root, arcname="data")

    return BackupResult(
        archive_path=str(archive_path), size_bytes=archive_path.stat().st_size,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


def upload_backup_to_s3(archive_path: Path | str, settings: Settings) -> str | None:
    """Uploads a given local backup archive to S3-compatible storage via boto3.

    A no-op returning None if backup_s3_bucket is unset (matching this codebase's
    optional integration convention). Boto3 is imported lazily inside this
    function so that environments without offsite backup configured do not incur
    unnecessary import overhead.

    Raises an exception if an error occurs during client initialization or upload.
    """
    if not settings.backup_s3_bucket:
        return None

    import boto3

    archive_path = Path(archive_path)
    client_kwargs: dict[str, str] = {}
    if settings.backup_s3_endpoint_url:
        client_kwargs["endpoint_url"] = settings.backup_s3_endpoint_url
    if settings.backup_s3_region:
        client_kwargs["region_name"] = settings.backup_s3_region
    if settings.backup_s3_access_key_id:
        client_kwargs["aws_access_key_id"] = settings.backup_s3_access_key_id
    if settings.backup_s3_secret_access_key:
        client_kwargs["aws_secret_access_key"] = settings.backup_s3_secret_access_key

    s3 = boto3.client("s3", **client_kwargs)
    s3.upload_file(str(archive_path), settings.backup_s3_bucket, archive_path.name)
    return "uploaded"


def list_backups(backup_dir: Path) -> list[dict]:
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return []
    entries = []
    for path in sorted(backup_dir.glob("business_ai_backup_*.tar.gz")):
        stat = path.stat()
        entries.append({
            "archive_path": str(path), "size_bytes": stat.st_size,
            "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
        })
    return entries


def restore_backup(archive_path: Path, data_root: Path) -> Path:
    """Restores an archive over `data_root`. The CURRENT data directory
    is renamed aside (never deleted) before extraction, so a restore
    that turns out to be the wrong archive is itself trivially
    reversible — move the `.pre_restore_*` directory back into place."""
    archive_path = Path(archive_path)
    data_root = Path(data_root)
    if not archive_path.is_file():
        raise FileNotFoundError(f"Backup archive not found: {archive_path}")

    if data_root.exists():
        aside = data_root.parent / f"{data_root.name}.pre_restore_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
        shutil.move(str(data_root), str(aside))

    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(data_root.parent)

    return data_root


def check_data_integrity(*, employee_store, task_store, sop_store, lead_store, tenant_registry) -> dict:
    """Read-only scan for orphaned cross-store references that would
    otherwise fail silently (e.g. a task rendering "(unassigned)" forever
    because its assignee's row is simply gone, not because it was
    genuinely unassigned). Returns a report; never mutates anything."""
    issues: list[dict] = []
    tenants = tenant_registry.list_all()
    for tenant in tenants:
        tenant_id = tenant.tenant_id
        employees_by_id = {e.employee_id: e for e in employee_store.list_for_tenant(tenant_id)}

        for task in task_store.list_for_tenant(tenant_id):
            if task.assigned_to_employee_id and task.assigned_to_employee_id not in employees_by_id:
                issues.append({
                    "tenant_id": tenant_id, "type": "task_orphaned_assignee",
                    "task_id": task.task_id, "missing_employee_id": task.assigned_to_employee_id,
                })
            if task.customer_facing_lead_id:
                lead = lead_store.get(tenant_id, task.customer_facing_lead_id)
                if lead is None:
                    issues.append({
                        "tenant_id": tenant_id, "type": "task_orphaned_lead_link",
                        "task_id": task.task_id, "missing_lead_id": task.customer_facing_lead_id,
                    })

        for sop in sop_store.list_for_tenant(tenant_id):
            if sop.approved_by_employee_id and sop.approved_by_employee_id not in employees_by_id:
                issues.append({
                    "tenant_id": tenant_id, "type": "sop_orphaned_author",
                    "theme": sop.theme, "missing_employee_id": sop.approved_by_employee_id,
                })

    return {"tenants_checked": len(tenants), "issue_count": len(issues), "issues": issues}
