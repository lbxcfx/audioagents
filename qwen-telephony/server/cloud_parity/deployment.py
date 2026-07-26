from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Protocol
import uuid

from .store import PlatformStore, ResourceNotFoundError, _row, _utc_now


@dataclass(frozen=True)
class BuildOutcome:
    success: bool
    logs: str = ""
    error: str = ""


@dataclass(frozen=True)
class RolloutOutcome:
    success: bool
    healthy: bool
    message: str = ""


class BuildExecutor(Protocol):
    def build(self, source_ref: str, image_ref: str) -> BuildOutcome: ...


class RuntimeExecutor(Protocol):
    def rollout(self, deployment: dict[str, Any], version: dict[str, Any]) -> RolloutOutcome: ...


class DeploymentDriverUnavailableError(RuntimeError):
    """Raised when an operator-facing deployment action has no active driver."""


class DockerBuildExecutor:
    """BuildKit-compatible local Docker adapter used by the MVP API."""

    driver_name = "docker"
    enabled = True
    source_ref_kind = "local_directory"

    def build(self, source_ref: str, image_ref: str) -> BuildOutcome:
        source = Path(source_ref).resolve()
        if not source.is_dir() or not (source / "Dockerfile").is_file():
            return BuildOutcome(False, error="source_ref must contain a Dockerfile")
        try:
            result = subprocess.run(
                ["docker", "build", "--tag", image_ref, str(source)],
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return BuildOutcome(False, error=str(exc))
        logs = (result.stdout + "\n" + result.stderr)[-200_000:]
        return BuildOutcome(result.returncode == 0, logs=logs, error="" if result.returncode == 0 else logs[-4000:])


class DisabledBuildExecutor:
    """Fail-closed production default when no isolated image builder is configured."""

    driver_name = "disabled"
    enabled = False
    source_ref_kind = "unavailable"

    def build(self, source_ref: str, image_ref: str) -> BuildOutcome:
        return BuildOutcome(
            False,
            error="image build driver is disabled; publish an image through the release pipeline",
        )


class ControlPlaneRuntimeExecutor:
    """Safe default: records a staged release until a Docker/Kubernetes driver is configured."""

    driver_name = "control-plane"
    enabled = False
    supports_instances = False
    supports_logs = False

    def rollout(self, deployment: dict[str, Any], version: dict[str, Any]) -> RolloutOutcome:
        return RolloutOutcome(
            success=True,
            healthy=False,
            message="release staged; configure a runtime deployment driver to activate it",
        )


class SecretCipher:
    def __init__(self, key: bytes):
        from cryptography.fernet import Fernet

        self._fernet = Fernet(key)

    @classmethod
    def load_or_create(cls, key_path: Path) -> "SecretCipher":
        from cryptography.fernet import Fernet

        env_key = os.getenv("CLOUD_PARITY_MASTER_KEY", "").strip()
        if env_key:
            return cls(env_key.encode("ascii"))
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            key = key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            key_path.write_bytes(key + b"\n")
        return cls(key)

    @classmethod
    def generate(cls) -> "SecretCipher":
        from cryptography.fernet import Fernet

        return cls(Fernet.generate_key())

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")


class DeploymentService:
    def __init__(
        self,
        store: PlatformStore,
        cipher: SecretCipher,
        build_executor: BuildExecutor | None = None,
        runtime_executor: RuntimeExecutor | None = None,
    ):
        self.store = store
        self.cipher = cipher
        self.build_executor = build_executor or DockerBuildExecutor()
        self.runtime_executor = runtime_executor or ControlPlaneRuntimeExecutor()

    def capabilities(self) -> dict[str, Any]:
        build_enabled = bool(getattr(self.build_executor, "enabled", True))
        runtime_enabled = bool(getattr(self.runtime_executor, "enabled", True))
        return {
            "build": {
                "enabled": build_enabled,
                "driver": str(
                    getattr(self.build_executor, "driver_name", type(self.build_executor).__name__)
                ),
                "source_ref_kind": str(
                    getattr(self.build_executor, "source_ref_kind", "driver_defined")
                ),
                "message": (
                    "构建驱动已启用"
                    if build_enabled
                    else "构建驱动未启用，请通过受信任的 CI/CD 发布镜像"
                ),
            },
            "runtime": {
                "enabled": runtime_enabled,
                "driver": str(
                    getattr(self.runtime_executor, "driver_name", type(self.runtime_executor).__name__)
                ),
                "supports_instances": bool(
                    getattr(self.runtime_executor, "supports_instances", runtime_enabled)
                ),
                "supports_logs": bool(
                    getattr(self.runtime_executor, "supports_logs", runtime_enabled)
                ),
                "message": (
                    "运行时发布驱动已启用"
                    if runtime_enabled
                    else "自助发布驱动未启用，当前生产工作负载由外部发布流程管理"
                ),
            },
        }

    @staticmethod
    def _require_driver(driver: Any, operation: str) -> None:
        if not bool(getattr(driver, "enabled", True)):
            raise DeploymentDriverUnavailableError(
                f"{operation} driver is not configured"
            )

    def create_agent(
        self, *, project_id: str, actor_id: str, name: str, description: str = ""
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "project.manage")
        if not name.strip():
            raise ValueError("agent name is required")
        agent_id = str(uuid.uuid4())
        now = _utc_now()
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO agent_definitions (
                    id, project_id, name, description, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (agent_id, project_id, name.strip(), description.strip(), now, now),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="agent.create",
                resource_type="agent",
                resource_id=agent_id,
                payload={"name": name.strip()},
            )
            row = conn.execute("SELECT * FROM agent_definitions WHERE id = ?", (agent_id,)).fetchone()
        return _row(row) or {}

    def list_agents(self, *, project_id: str, user_id: str) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "agent.read")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM agent_versions v WHERE v.agent_id = a.id) AS version_count,
                       (SELECT v.id FROM agent_versions v WHERE v.agent_id = a.id
                        ORDER BY v.version_number DESC LIMIT 1) AS latest_version_id
                FROM agent_definitions a
                WHERE a.project_id = ? ORDER BY a.updated_at DESC, a.id DESC
                """,
                (project_id,),
            ).fetchall()
        return [_row(item) or {} for item in rows]

    def list_versions(
        self, *, project_id: str, user_id: str, agent_id: str
    ) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "agent.read")
        with self.store.connect() as conn:
            self._require_agent(conn, project_id, agent_id)
            rows = conn.execute(
                """
                SELECT * FROM agent_versions
                WHERE project_id = ? AND agent_id = ?
                ORDER BY version_number DESC
                """,
                (project_id, agent_id),
            ).fetchall()
        return [self._version_record(item) for item in rows]

    def list_deployments(self, *, project_id: str, user_id: str) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "project.read")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.*, a.name AS agent_name
                FROM agent_deployments d
                JOIN agent_definitions a ON a.id = d.agent_id
                WHERE d.project_id = ? ORDER BY d.updated_at DESC, d.id DESC
                """,
                (project_id,),
            ).fetchall()
        return [_row(item) or {} for item in rows]

    def build_version(
        self,
        *,
        project_id: str,
        actor_id: str,
        agent_id: str,
        source_ref: str,
        image_ref: str,
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "project.manage")
        self._require_driver(self.build_executor, "image build")
        build_id = str(uuid.uuid4())
        now = _utc_now()
        with self.store.transaction() as conn:
            self._require_agent(conn, project_id, agent_id)
            conn.execute(
                """
                INSERT INTO agent_builds (
                    id, project_id, agent_id, source_ref, image_ref, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'building', ?)
                """,
                (build_id, project_id, agent_id, source_ref, image_ref, now),
            )
        outcome = self.build_executor.build(source_ref, image_ref)
        completed_at = _utc_now()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE agent_builds
                SET status = ?, logs = ?, error = ?, completed_at = ?
                WHERE id = ? AND project_id = ?
                """,
                (
                    "succeeded" if outcome.success else "failed",
                    outcome.logs,
                    outcome.error,
                    completed_at,
                    build_id,
                    project_id,
                ),
            )
            version = None
            if outcome.success:
                number = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(version_number), 0) + 1 AS value FROM agent_versions WHERE agent_id = ?",
                        (agent_id,),
                    ).fetchone()["value"]
                )
                version_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO agent_versions (
                        id, project_id, agent_id, build_id, version_number,
                        image_ref, spec_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        project_id,
                        agent_id,
                        build_id,
                        number,
                        image_ref,
                        json.dumps(spec or {}, ensure_ascii=False, separators=(",", ":")),
                        completed_at,
                    ),
                )
                version = conn.execute(
                    "SELECT * FROM agent_versions WHERE id = ?", (version_id,)
                ).fetchone()
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="agent.build.complete",
                resource_type="build",
                resource_id=build_id,
                payload={"status": "succeeded" if outcome.success else "failed"},
            )
            build = conn.execute("SELECT * FROM agent_builds WHERE id = ?", (build_id,)).fetchone()
        result = {"build": _row(build) or {}, "version": self._version_record(version) if version else None}
        return result

    def create_deployment(
        self,
        *,
        project_id: str,
        actor_id: str,
        agent_id: str,
        version_id: str,
        name: str,
        desired_replicas: int = 1,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "project.manage")
        self._require_driver(self.runtime_executor, "runtime deployment")
        if not 0 <= desired_replicas <= 100:
            raise ValueError("desired_replicas must be between 0 and 100")
        deployment_id = str(uuid.uuid4())
        now = _utc_now()
        with self.store.transaction() as conn:
            self._require_agent(conn, project_id, agent_id)
            self._require_version(conn, project_id, agent_id, version_id)
            conn.execute(
                """
                INSERT INTO agent_deployments (
                    id, project_id, agent_id, name, status, desired_replicas,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'created', ?, ?, ?)
                """,
                (deployment_id, project_id, agent_id, name.strip(), desired_replicas, now, now),
            )
        return self.rollout(
            project_id=project_id,
            actor_id=actor_id,
            deployment_id=deployment_id,
            version_id=version_id,
        )

    def rollout(
        self,
        *,
        project_id: str,
        actor_id: str,
        deployment_id: str,
        version_id: str,
        operation: str = "rollout",
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "project.manage")
        self._require_driver(self.runtime_executor, "runtime deployment")
        revision_id = str(uuid.uuid4())
        now = _utc_now()
        with self.store.transaction() as conn:
            deployment_row = conn.execute(
                "SELECT * FROM agent_deployments WHERE id = ? AND project_id = ?",
                (deployment_id, project_id),
            ).fetchone()
            if deployment_row is None:
                raise ResourceNotFoundError("deployment not found")
            deployment = _row(deployment_row) or {}
            version_row = self._require_version(
                conn, project_id, deployment["agent_id"], version_id
            )
            version = self._version_record(version_row)
            conn.execute(
                """
                INSERT INTO deployment_revisions (
                    id, project_id, deployment_id, version_id, operation,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'deploying', ?)
                """,
                (revision_id, project_id, deployment_id, version_id, operation, now),
            )
            conn.execute(
                "UPDATE agent_deployments SET status = 'deploying', updated_at = ? WHERE id = ?",
                (now, deployment_id),
            )
        outcome = self.runtime_executor.rollout(deployment, version)
        finished = _utc_now()
        final_status = "ready" if outcome.success and outcome.healthy else (
            "staged" if outcome.success else "failed"
        )
        with self.store.transaction() as conn:
            if outcome.success:
                conn.execute(
                    """
                    UPDATE agent_deployments
                    SET previous_version_id = CASE
                            WHEN active_version_id IS NOT NULL AND active_version_id != ?
                            THEN active_version_id ELSE previous_version_id END,
                        active_version_id = ?, status = ?, updated_at = ?
                    WHERE id = ? AND project_id = ?
                    """,
                    (version_id, version_id, final_status, finished, deployment_id, project_id),
                )
            else:
                conn.execute(
                    "UPDATE agent_deployments SET status = 'failed', updated_at = ? WHERE id = ?",
                    (finished, deployment_id),
                )
            conn.execute(
                """
                UPDATE deployment_revisions
                SET status = ?, message = ?, completed_at = ? WHERE id = ?
                """,
                (final_status, outcome.message, finished, revision_id),
            )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action=f"deployment.{operation}.complete",
                resource_type="deployment",
                resource_id=deployment_id,
                payload={"version_id": version_id, "status": final_status},
            )
        return self.get_deployment(project_id, actor_id, deployment_id)

    def rollback(
        self, *, project_id: str, actor_id: str, deployment_id: str
    ) -> dict[str, Any]:
        deployment = self.get_deployment(project_id, actor_id, deployment_id)
        previous = deployment.get("previous_version_id")
        if not previous:
            raise ValueError("deployment has no previous version")
        return self.rollout(
            project_id=project_id,
            actor_id=actor_id,
            deployment_id=deployment_id,
            version_id=previous,
            operation="rollback",
        )

    def get_deployment(
        self, project_id: str, user_id: str, deployment_id: str
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "project.read")
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_deployments WHERE id = ? AND project_id = ?",
                (deployment_id, project_id),
            ).fetchone()
        if row is None:
            raise ResourceNotFoundError("deployment not found")
        return _row(row) or {}

    def put_secret(
        self, *, project_id: str, actor_id: str, name: str, value: str
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, actor_id, "project.manage")
        if not name or not name.replace("_", "").isalnum() or name.upper() != name:
            raise ValueError("secret name must use uppercase letters, numbers, and underscores")
        if not value or len(value.encode("utf-8")) > 16_384:
            raise ValueError("secret value must be between 1 byte and 16 KiB")
        secret_id = str(uuid.uuid4())
        now = _utc_now()
        ciphertext = self.cipher.encrypt(value)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        with self.store.transaction() as conn:
            existing = conn.execute(
                "SELECT id, created_at FROM encrypted_secrets WHERE project_id = ? AND name = ?",
                (project_id, name),
            ).fetchone()
            if existing:
                secret_id = existing["id"]
                conn.execute(
                    """
                    UPDATE encrypted_secrets
                    SET ciphertext = ?, value_sha256 = ?, updated_at = ? WHERE id = ?
                    """,
                    (ciphertext, digest, now, secret_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO encrypted_secrets (
                        id, project_id, name, ciphertext, value_sha256, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (secret_id, project_id, name, ciphertext, digest, now, now),
                )
            self.store._append_audit(
                conn,
                project_id=project_id,
                actor_id=actor_id,
                action="secret.upsert",
                resource_type="secret",
                resource_id=secret_id,
                payload={"name": name},
            )
            row = conn.execute(
                "SELECT id, project_id, name, value_sha256, created_at, updated_at FROM encrypted_secrets WHERE id = ?",
                (secret_id,),
            ).fetchone()
        return _row(row) or {}

    def list_secrets(self, *, project_id: str, user_id: str) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "project.manage")
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, project_id, name, value_sha256, created_at, updated_at
                FROM encrypted_secrets WHERE project_id = ? ORDER BY name
                """,
                (project_id,),
            ).fetchall()
        return [_row(item) or {} for item in rows]

    def resolve_secrets(self, project_id: str) -> dict[str, str]:
        """Internal runtime-only API; plaintext is never exposed by the HTTP router."""
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT name, ciphertext FROM encrypted_secrets WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        return {item["name"]: self.cipher.decrypt(item["ciphertext"]) for item in rows}

    @staticmethod
    def _require_agent(conn: Any, project_id: str, agent_id: str) -> Any:
        row = conn.execute(
            "SELECT * FROM agent_definitions WHERE id = ? AND project_id = ?",
            (agent_id, project_id),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("agent not found")
        return row

    @staticmethod
    def _require_version(conn: Any, project_id: str, agent_id: str, version_id: str) -> Any:
        row = conn.execute(
            """
            SELECT * FROM agent_versions
            WHERE id = ? AND project_id = ? AND agent_id = ?
            """,
            (version_id, project_id, agent_id),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("agent version not found")
        return row

    @staticmethod
    def _version_record(row: Any) -> dict[str, Any]:
        record = _row(row) or {}
        record["spec"] = json.loads(record.pop("spec_json") or "{}")
        return record
