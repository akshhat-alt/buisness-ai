"""Tests for grandfather migration of legacy tenants to 'scale' plan."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from business_ai.app import Services, create_app
from business_ai.auth import Principal, create_access_token
from business_ai.retrieval import HashEmbeddingProvider
from business_ai.tenant import TenantConfig, TenantRegistry, TenantStatus
from tests.conftest import FakeGenerator


def test_grandfather_migration_migrates_legacy_rows_to_scale(tmp_path: Path):
    db_path = tmp_path / "tenants.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE tenants (
                tenant_id TEXT PRIMARY KEY,
                config_json TEXT NOT NULL
            )
            """
        )
        # Seed a legacy tenant row with NO "plan" key
        legacy_data = {
            "tenant_id": "legacy-tenant",
            "business_name": "Legacy Business",
            "owner_email": "legacy@example.com",
            "status": "active",
        }
        conn.execute(
            "INSERT INTO tenants (tenant_id, config_json) VALUES (?, ?)",
            ("legacy-tenant", json.dumps(legacy_data)),
        )
        conn.commit()

    # Initializing registry runs migrate_grandfathered_tenants() on startup
    registry = TenantRegistry(db_path)
    tenant = registry.get_config("legacy-tenant")
    assert tenant.plan == "scale"

    # Verify directly in SQLite storage that "plan": "scale" was written
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT config_json FROM tenants WHERE tenant_id = 'legacy-tenant'").fetchone()
        saved = json.loads(row[0])
        assert saved.get("plan") == "scale"


def test_fresh_signup_tenant_defaults_to_starter_and_migration_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "tenants.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE tenants (
                tenant_id TEXT PRIMARY KEY,
                config_json TEXT NOT NULL
            )
            """
        )
        legacy_data = {
            "tenant_id": "old-tenant",
            "business_name": "Old Business",
            "owner_email": "old@example.com",
            "status": "active",
        }
        conn.execute(
            "INSERT INTO tenants (tenant_id, config_json) VALUES (?, ?)",
            ("old-tenant", json.dumps(legacy_data)),
        )
        conn.commit()

    registry = TenantRegistry(db_path)
    assert registry.get_config("old-tenant").plan == "scale"

    # Fresh signup through normal register path
    new_tenant = registry.register(
        TenantConfig(
            tenant_id="fresh-tenant",
            business_name="Fresh Business",
            owner_email="fresh@example.com",
            status=TenantStatus.PROVISIONING,
        )
    )
    assert new_tenant.plan == "starter"
    assert registry.get_config("fresh-tenant").plan == "starter"

    # Re-running migration is a no-op (idempotent)
    migrated = registry.migrate_grandfathered_tenants()
    assert migrated == 0
    assert registry.get_config("old-tenant").plan == "scale"
    assert registry.get_config("fresh-tenant").plan == "starter"


def test_grandfather_migration_via_app_startup_and_usage_endpoint(tmp_path: Path, settings):
    # 1. Seed a legacy tenant row directly into tenants.db before Services starts
    db_path = tmp_path / "tenants.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id TEXT PRIMARY KEY,
                config_json TEXT NOT NULL
            )
            """
        )
        legacy_data = {
            "tenant_id": "grandfathered-cafe",
            "business_name": "Grandfathered Cafe",
            "owner_email": "gf@example.com",
            "status": "active",
        }
        conn.execute(
            "INSERT INTO tenants (tenant_id, config_json) VALUES (?, ?)",
            ("grandfathered-cafe", json.dumps(legacy_data)),
        )
        conn.commit()

    # 2. Boot Services + create_app (simulates app boot)
    svc = Services(settings, data_root=tmp_path)
    svc.embeddings = lambda: HashEmbeddingProvider()
    svc.generator = lambda: FakeGenerator()
    app = create_app(svc)
    client = TestClient(app)

    # 3. Authenticate as owner of grandfathered-cafe and check /api/tenant/usage
    token_gf = create_access_token(Principal.owner("user_gf", "grandfathered-cafe"), settings)
    headers_gf = {"Authorization": f"Bearer {token_gf}"}
    r_gf = client.get("/api/tenant/usage?tenant_id=grandfathered-cafe", headers=headers_gf)
    assert r_gf.status_code == 200, r_gf.text
    assert r_gf.json()["plan"] == "scale"

    # 4. Sign up a new tenant via /api/auth/signup
    r_signup = client.post(
        "/api/auth/signup",
        json={
            "email": "newbie@example.com",
            "password": "strong-password-123",
            "name": "Newbie Owner",
            "business_name": "Newbie Bistro",
        },
    )
    assert r_signup.status_code == 200, r_signup.text
    data_newbie = r_signup.json()
    newbie_tenant_id = data_newbie["tenant_id"]
    headers_newbie = {"Authorization": f"Bearer {data_newbie['access_token']}"}

    r_usage = client.get(f"/api/tenant/usage?tenant_id={newbie_tenant_id}", headers=headers_newbie)
    assert r_usage.status_code == 200, r_usage.text
    assert r_usage.json()["plan"] == "starter"

    svc.vector_store.close()
