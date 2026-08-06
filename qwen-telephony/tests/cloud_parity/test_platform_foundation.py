from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.api import create_platform_router
from server.cloud_parity.auth import DevelopmentAuthenticator, install_authenticator
from server.cloud_parity.store import (
    AccessDeniedError,
    MigrationDriftError,
    PlatformStore,
)


@pytest.fixture()
def store(tmp_path: Path) -> PlatformStore:
    value = PlatformStore(tmp_path / "platform.sqlite3")
    value.initialize()
    return value


def test_migrations_are_repeatable(store: PlatformStore) -> None:
    first = store.initialize()
    second = store.initialize()

    assert first == second
    assert store.schema_version() >= 2

    with store.connect() as conn:
        metadata = conn.execute(
            "SELECT version, checksum FROM schema_migration_metadata ORDER BY version"
        ).fetchall()
    assert len(metadata) == store.schema_version()
    assert all(len(item["checksum"]) == 64 for item in metadata)


def test_distributed_rate_limit_is_atomic_and_resets_after_window(
    store: PlatformStore,
) -> None:
    now = datetime.now(timezone.utc)

    def consume(_index: int) -> dict:
        return store.consume_api_rate_limit(
            key="shared-oidc-subject",
            limit=5,
            window_seconds=60,
            now=now,
        )

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(consume, range(20)))

    assert sum(result["allowed"] for result in results) == 5
    reset = store.consume_api_rate_limit(
        key="shared-oidc-subject",
        limit=5,
        window_seconds=60,
        now=now + timedelta(seconds=61),
    )
    assert reset["allowed"] is True
    assert reset["remaining"] == 4


def test_owner_membership_invariants_and_admin_offboarding(tmp_path: Path) -> None:
    store = PlatformStore(tmp_path / "membership.sqlite3")
    store.initialize()
    project = store.create_project(
        name="Membership", slug="membership", owner_id="owner-a"
    )
    project_id = project["id"]
    store.add_membership(
        project_id=project_id,
        actor_id="owner-a",
        user_id="admin-a",
        role="admin",
    )
    store.add_membership(
        project_id=project_id,
        actor_id="owner-a",
        user_id="member-a",
        role="member",
    )

    with pytest.raises(AccessDeniedError, match="only an owner"):
        store.add_membership(
            project_id=project_id,
            actor_id="admin-a",
            user_id="admin-a",
            role="owner",
        )
    with pytest.raises(ValueError, match="at least one owner"):
        store.remove_membership(
            project_id=project_id,
            actor_id="owner-a",
            user_id="owner-a",
        )

    removed = store.remove_membership(
        project_id=project_id,
        actor_id="admin-a",
        user_id="member-a",
    )
    assert removed["removed"] is True
    assert {item["user_id"] for item in store.list_memberships(
        project_id=project_id, actor_id="owner-a"
    )} == {"owner-a", "admin-a"}


def test_migration_history_drift_is_rejected(store: PlatformStore) -> None:
    with store.transaction() as conn:
        conn.execute(
            "UPDATE schema_migration_metadata SET checksum = ? WHERE version = ?",
            ("tampered", 1),
        )

    with pytest.raises(MigrationDriftError):
        store.initialize()


def test_database_health_reports_backend_without_connection_details(
    store: PlatformStore,
) -> None:
    health = store.healthcheck()

    assert health["status"] == "ok"
    assert health["backend"] == "sqlite"
    assert health["schema_version"] == store.schema_version()
    assert health["latency_ms"] >= 0


def test_projects_are_isolated_and_roles_are_enforced(store: PlatformStore) -> None:
    alpha = store.create_project(name="Alpha", slug="alpha", owner_id="owner-a")
    beta = store.create_project(name="Beta", slug="beta", owner_id="owner-b")

    assert [item["id"] for item in store.list_projects("owner-a")] == [alpha["id"]]
    assert [item["id"] for item in store.list_projects("owner-b")] == [beta["id"]]

    with pytest.raises(AccessDeniedError):
        store.get_project(beta["id"], "owner-a")

    store.add_membership(
        project_id=alpha["id"], actor_id="owner-a", user_id="viewer-a", role="viewer"
    )
    assert store.get_project(alpha["id"], "viewer-a")["role"] == "viewer"
    with pytest.raises(AccessDeniedError):
        store.add_membership(
            project_id=alpha["id"],
            actor_id="viewer-a",
            user_id="intruder",
            role="admin",
        )

    store.add_membership(
        project_id=alpha["id"],
        actor_id="owner-a",
        user_id="agent-worker",
        role="worker",
    )
    assert store.require_permission(
        alpha["id"], "agent-worker", "telephony.work"
    ) == "worker"
    assert store.require_permission(
        alpha["id"], "agent-worker", "session.write"
    ) == "worker"
    assert store.require_permission(
        alpha["id"], "agent-worker", "session.read"
    ) == "worker"
    with pytest.raises(AccessDeniedError):
        store.require_permission(alpha["id"], "agent-worker", "telephony.operate")
    with pytest.raises(AccessDeniedError):
        store.require_permission(alpha["id"], "agent-worker", "project.manage")


def test_management_operations_create_audit_logs(store: PlatformStore) -> None:
    project = store.create_project(name="Audit", slug="audit", owner_id="owner")
    store.add_membership(
        project_id=project["id"], actor_id="owner", user_id="member", role="member"
    )

    logs = store.list_audit_logs(project_id=project["id"], user_id="owner")

    assert {item["action"] for item in logs} == {
        "project.create",
        "membership.upsert",
    }
    membership_log = next(item for item in logs if item["action"] == "membership.upsert")
    assert membership_log["payload"] == {"role": "member"}


def test_platform_api_enforces_user_header_and_project_access(store: PlatformStore) -> None:
    app = FastAPI()
    install_authenticator(app, DevelopmentAuthenticator())
    app.include_router(create_platform_router(store))
    client = TestClient(app)

    created = client.post(
        "/api/platform/projects",
        json={"name": "API", "slug": "api", "owner_id": "api-owner"},
    )
    assert created.status_code == 201
    project_id = created.json()["id"]

    assert client.get("/api/platform/projects").status_code == 422
    denied = client.get(
        f"/api/platform/projects/{project_id}", headers={"X-User-ID": "other"}
    )
    assert denied.status_code == 403

    allowed = client.get(
        f"/api/platform/projects/{project_id}", headers={"X-User-ID": "api-owner"}
    )
    assert allowed.status_code == 200
    assert allowed.json()["slug"] == "api"

    health = client.get("/api/platform/health")
    assert health.status_code == 200
    assert health.json()["backend"] == "sqlite"
    ready = client.get("/api/platform/health/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
