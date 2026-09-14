"""HTTP-level tests for Phase 14's ops routes: backup trigger/listing,
detailed health check, and data-integrity report — all platform_admin
gated.
"""

from __future__ import annotations

import dataclasses


def test_backup_run_requires_platform_admin(client, owner_session):
    headers, _ = owner_session
    r = client.post("/api/v1/admin/backup/run", headers=headers)
    assert r.status_code == 403


def test_backup_run_creates_a_real_archive(client, admin_headers, services):
    r = client.post("/api/v1/admin/backup/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["size_bytes"] > 0
    from pathlib import Path
    assert Path(data["archive_path"]).is_file()
    assert "offsite" not in data


def test_backup_run_pushes_to_s3_when_configured(client, admin_headers, services, monkeypatch):
    services.settings = dataclasses.replace(services.settings, backup_s3_bucket="offsite-bucket")

    from business_ai.routers import ops_routes

    uploaded = []

    def fake_upload(archive_path, settings):
        uploaded.append((archive_path, settings.backup_s3_bucket))
        return "uploaded"

    monkeypatch.setattr(ops_routes, "upload_backup_to_s3", fake_upload)

    r = client.post("/api/v1/admin/backup/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["offsite"] == "uploaded"
    assert len(uploaded) == 1
    assert uploaded[0][1] == "offsite-bucket"
    from pathlib import Path
    assert Path(data["archive_path"]).is_file()


def test_backup_run_handles_offsite_failure_gracefully(client, admin_headers, services, monkeypatch):
    services.settings = dataclasses.replace(services.settings, backup_s3_bucket="offsite-bucket")

    from business_ai.routers import ops_routes

    def failing_upload(archive_path, settings):
        raise ConnectionResetError("Connection dropped by S3 peer")

    monkeypatch.setattr(ops_routes, "upload_backup_to_s3", failing_upload)

    r = client.post("/api/v1/admin/backup/run", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["offsite"] == "failed"
    assert data["size_bytes"] > 0
    from pathlib import Path
    assert Path(data["archive_path"]).is_file()


def test_backup_list_reflects_created_backups(client, admin_headers):
    r1 = client.post("/api/v1/admin/backup/run", headers=admin_headers)
    assert r1.status_code == 200, r1.text
    r2 = client.get("/api/v1/admin/backup/list", headers=admin_headers)
    assert r2.status_code == 200, r2.text
    assert len(r2.json()["backups"]) >= 1


def test_detailed_health_requires_platform_admin(client, owner_session):
    headers, _ = owner_session
    r = client.get("/api/v1/admin/health/detailed", headers=headers)
    assert r.status_code == 403


def test_detailed_health_reports_ok_stores(client, admin_headers):
    r = client.get("/api/v1/admin/health/detailed", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["stores"]["tenants"] == "ok"
    assert data["vector_store"] == "ok"
    assert "checked_at" in data


def test_data_integrity_requires_platform_admin(client, owner_session):
    headers, _ = owner_session
    r = client.get("/api/v1/admin/data-integrity", headers=headers)
    assert r.status_code == 403


def test_data_integrity_clean_report_for_normal_usage(client, owner_session, services, admin_headers):
    headers, tenant_id = owner_session
    ravi = services.employee_store.add(tenant_id=tenant_id, whatsapp_number="919876500001", name="Ravi")
    services.task_store.create(
        tenant_id=tenant_id, title="Restock", assigned_to_employee_id=ravi.employee_id,
        assigned_by_employee_id=ravi.employee_id,
    )
    r = client.get("/api/v1/admin/data-integrity", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["issue_count"] == 0


def test_data_integrity_finds_orphaned_reference(client, owner_session, services, admin_headers):
    headers, tenant_id = owner_session
    services.task_store.create(
        tenant_id=tenant_id, title="Restock", assigned_to_employee_id="emp_ghost", assigned_by_employee_id="emp_ghost",
    )
    r = client.get("/api/v1/admin/data-integrity", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["issue_count"] >= 1
    assert any(i["type"] == "task_orphaned_assignee" for i in data["issues"])
