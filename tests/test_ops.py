"""Tests for Phase 14's production-readiness module: backup/restore
(real tar.gz files on a temp filesystem, no mocks) and the data-
integrity scanner (real store instances).
"""

from __future__ import annotations

import time

import pytest

from business_ai.employees import EmployeeStore
from business_ai.leads import LeadStore
from business_ai.memory import SopStore
from business_ai.ops import check_data_integrity, create_backup, list_backups, restore_backup, upload_backup_to_s3
from business_ai.tasks import TaskStore
from business_ai.tenant import TenantRegistry

TENANT = "salon-a"


def test_create_backup_produces_a_real_archive_containing_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "tenants.db").write_text("fake sqlite content")
    backup_dir = tmp_path / "backups"

    result = create_backup(data_dir, backup_dir)
    assert result.size_bytes > 0
    import tarfile
    with tarfile.open(result.archive_path, "r:gz") as tar:
        names = tar.getnames()
    assert any("tenants.db" in n for n in names)


def test_create_backup_raises_for_missing_data_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_backup(tmp_path / "does-not-exist", tmp_path / "backups")


def test_list_backups_returns_empty_for_no_backups_dir(tmp_path):
    assert list_backups(tmp_path / "nothing-here") == []


def test_list_backups_finds_created_archives(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "x.db").write_text("x")
    backup_dir = tmp_path / "backups"
    create_backup(data_dir, backup_dir)
    time.sleep(1.1)  # ensure a distinct timestamp for a second archive
    create_backup(data_dir, backup_dir)
    backups = list_backups(backup_dir)
    assert len(backups) == 2


def test_restore_backup_moves_current_data_aside_and_extracts(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "original.db").write_text("original content")
    backup_dir = tmp_path / "backups"
    result = create_backup(data_dir, backup_dir)

    # Simulate the live data dir changing after the backup was taken.
    (data_dir / "original.db").write_text("corrupted!")
    (data_dir / "new_file.db").write_text("should be moved aside, not lost")

    restore_backup(result.archive_path, data_dir)

    assert (data_dir / "original.db").read_text() == "original content"
    # The pre-restore directory must still exist somewhere, not deleted.
    aside_dirs = list(tmp_path.glob("data.pre_restore_*"))
    assert len(aside_dirs) == 1
    assert (aside_dirs[0] / "new_file.db").read_text() == "should be moved aside, not lost"


def test_restore_backup_raises_for_missing_archive(tmp_path):
    with pytest.raises(FileNotFoundError):
        restore_backup(tmp_path / "nope.tar.gz", tmp_path / "data")


@pytest.fixture()
def integrity_stores(tmp_path):
    return {
        "employee_store": EmployeeStore(tmp_path / "employees.db"),
        "task_store": TaskStore(tmp_path / "tasks.db"),
        "sop_store": SopStore(tmp_path / "sops.db"),
        "lead_store": LeadStore(tmp_path / "leads.db"),
        "tenant_registry": TenantRegistry(tmp_path / "tenants.db"),
    }


def _register_tenant(registry, tenant_id):
    from business_ai.tenant import TenantConfig
    registry.register(TenantConfig(tenant_id=tenant_id, business_name="Test Biz", owner_email="o@example.com"))


def test_integrity_check_clean_when_no_data(integrity_stores):
    report = check_data_integrity(**integrity_stores)
    assert report["issue_count"] == 0


def test_integrity_check_finds_orphaned_task_assignee(integrity_stores):
    _register_tenant(integrity_stores["tenant_registry"], TENANT)
    task_store = integrity_stores["task_store"]
    task = task_store.create(
        tenant_id=TENANT, title="Restock", assigned_to_employee_id="emp_ghost", assigned_by_employee_id="emp_ghost",
    )
    report = check_data_integrity(**integrity_stores)
    assert report["issue_count"] == 1
    assert report["issues"][0]["type"] == "task_orphaned_assignee"
    assert report["issues"][0]["task_id"] == task.task_id


def test_integrity_check_passes_for_a_real_employee_assignee(integrity_stores):
    _register_tenant(integrity_stores["tenant_registry"], TENANT)
    employee = integrity_stores["employee_store"].add(tenant_id=TENANT, whatsapp_number="919876500001", name="Ravi")
    integrity_stores["task_store"].create(
        tenant_id=TENANT, title="Restock", assigned_to_employee_id=employee.employee_id,
        assigned_by_employee_id=employee.employee_id,
    )
    report = check_data_integrity(**integrity_stores)
    assert report["issue_count"] == 0


def test_integrity_check_finds_orphaned_sop_author(integrity_stores):
    _register_tenant(integrity_stores["tenant_registry"], TENANT)
    integrity_stores["sop_store"].approve(
        tenant_id=TENANT, theme="software_or_tools", text="workaround", approved_by_employee_id="emp_ghost",
    )
    report = check_data_integrity(**integrity_stores)
    assert any(i["type"] == "sop_orphaned_author" for i in report["issues"])


def test_upload_backup_to_s3_noop_when_unconfigured(tmp_path, settings):
    archive = tmp_path / "test_backup.tar.gz"
    archive.write_text("archive data")
    assert settings.backup_s3_bucket is None
    res = upload_backup_to_s3(archive, settings)
    assert res is None


def test_upload_backup_to_s3_uploads_with_configured_settings(tmp_path, settings, monkeypatch):
    import dataclasses
    import boto3

    archive = tmp_path / "test_backup.tar.gz"
    archive.write_text("archive data")

    configured_settings = dataclasses.replace(
        settings,
        backup_s3_bucket="my-backup-bucket",
        backup_s3_access_key_id="test-key-id",
        backup_s3_secret_access_key="test-secret-key",
        backup_s3_endpoint_url="https://s3.us-west-002.backblazeb2.com",
        backup_s3_region="us-west-002",
    )

    captured_client_kwargs = {}
    uploaded_files = []

    class FakeS3Client:
        def upload_file(self, filename, bucket, key):
            uploaded_files.append({"filename": filename, "bucket": bucket, "key": key})

    def fake_boto3_client(service, **kwargs):
        assert service == "s3"
        captured_client_kwargs.update(kwargs)
        return FakeS3Client()

    monkeypatch.setattr(boto3, "client", fake_boto3_client)

    result = upload_backup_to_s3(archive, configured_settings)
    assert result == "uploaded"
    assert captured_client_kwargs == {
        "endpoint_url": "https://s3.us-west-002.backblazeb2.com",
        "region_name": "us-west-002",
        "aws_access_key_id": "test-key-id",
        "aws_secret_access_key": "test-secret-key",
    }
    assert len(uploaded_files) == 1
    assert uploaded_files[0] == {
        "filename": str(archive),
        "bucket": "my-backup-bucket",
        "key": "test_backup.tar.gz",
    }


def test_upload_backup_to_s3_raises_on_error(tmp_path, settings, monkeypatch):
    import dataclasses
    import boto3

    archive = tmp_path / "test_backup.tar.gz"
    archive.write_text("archive data")

    configured_settings = dataclasses.replace(settings, backup_s3_bucket="my-backup-bucket")

    class FailingS3Client:
        def upload_file(self, filename, bucket, key):
            raise ConnectionError("S3 endpoint timed out")

    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: FailingS3Client())

    with pytest.raises(ConnectionError, match="S3 endpoint timed out"):
        upload_backup_to_s3(archive, configured_settings)

