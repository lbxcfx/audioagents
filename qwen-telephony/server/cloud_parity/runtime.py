from __future__ import annotations

from dataclasses import dataclass
import os
import re
import subprocess
import time
from typing import Any, Callable, Protocol
import uuid

from .deployment import RolloutOutcome
from .store import PlatformStore, ResourceNotFoundError, _row, _utc_now


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 60,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(
        self,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 60,
    ) -> CommandResult:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
        return CommandResult(result.returncode, result.stdout, result.stderr)


SecretResolver = Callable[[str], dict[str, str]]


class DockerRuntimeExecutor:
    """Blue/green Docker runtime with health gating and SIGTERM-based draining."""

    def __init__(
        self,
        store: PlatformStore,
        secret_resolver: SecretResolver,
        *,
        runner: CommandRunner | None = None,
        network: str = "qwen-livekit-net",
        health_timeout_seconds: float = 60,
        poll_interval_seconds: float = 0.5,
        drain_timeout_seconds: int = 3600,
    ):
        self.store = store
        self.secret_resolver = secret_resolver
        self.runner = runner or SubprocessCommandRunner()
        self.network = network
        self.health_timeout_seconds = health_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.drain_timeout_seconds = drain_timeout_seconds

    def rollout(self, deployment: dict[str, Any], version: dict[str, Any]) -> RolloutOutcome:
        project_id = deployment["project_id"]
        desired = int(deployment.get("desired_replicas", 1))
        secrets = self.secret_resolver(project_id)
        runtime_env = dict(os.environ)
        runtime_env.update(secrets)
        for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
            if os.getenv(key):
                runtime_env[key] = os.environ[key]
        started: list[dict[str, Any]] = []
        try:
            for replica_index in range(desired):
                instance = self._start_instance(
                    deployment, version, replica_index, runtime_env
                )
                started.append(instance)
                if not self._wait_until_healthy(instance["runtime_name"]):
                    raise RuntimeError(f"runtime health check failed: {instance['runtime_name']}")
                self._mark_instance(instance["id"], "ready", ready=True)
            self._drain_previous_instances(
                project_id=project_id,
                deployment_id=deployment["id"],
                keep_version_id=version["id"],
            )
            return RolloutOutcome(
                success=True,
                healthy=True,
                message=f"{desired} Docker replica(s) are healthy; previous revision drained",
            )
        except Exception as exc:
            for instance in started:
                self.runner.run(
                    ["docker", "rm", "-f", instance["runtime_name"]], timeout=30
                )
                self._mark_instance(instance["id"], "failed", error=str(exc), stopped=True)
            return RolloutOutcome(False, False, str(exc))

    def list_instances(
        self, *, project_id: str, user_id: str, deployment_id: str
    ) -> list[dict[str, Any]]:
        self.store.require_permission(project_id, user_id, "project.read")
        with self.store.connect() as conn:
            self._require_deployment(conn, project_id, deployment_id)
            rows = conn.execute(
                """
                SELECT * FROM runtime_instances
                WHERE project_id = ? AND deployment_id = ?
                ORDER BY created_at DESC, replica_index, id
                """,
                (project_id, deployment_id),
            ).fetchall()
        return [_row(item) or {} for item in rows]

    def collect_logs(
        self,
        *,
        project_id: str,
        user_id: str,
        deployment_id: str,
        tail: int = 500,
    ) -> int:
        self.store.require_permission(project_id, user_id, "project.read")
        safe_tail = max(1, min(tail, 5000))
        with self.store.connect() as conn:
            self._require_deployment(conn, project_id, deployment_id)
            instances = conn.execute(
                """
                SELECT * FROM runtime_instances
                WHERE project_id = ? AND deployment_id = ? AND status IN ('ready', 'draining')
                ORDER BY created_at, id
                """,
                (project_id, deployment_id),
            ).fetchall()
        count = 0
        for instance_row in instances:
            instance = _row(instance_row) or {}
            result = self.runner.run(
                ["docker", "logs", "--tail", str(safe_tail), "--timestamps", instance["runtime_name"]],
                timeout=30,
            )
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                if line.strip():
                    self.append_log(
                        project_id=project_id,
                        deployment_id=deployment_id,
                        instance_id=instance["id"],
                        stream="stdout",
                        message=line[-16_384:],
                    )
                    count += 1
        return count

    def append_log(
        self,
        *,
        project_id: str,
        deployment_id: str,
        instance_id: str | None,
        stream: str,
        message: str,
    ) -> dict[str, Any]:
        log_id = str(uuid.uuid4())
        with self.store.transaction() as conn:
            self._require_deployment(conn, project_id, deployment_id)
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS value FROM runtime_logs WHERE deployment_id = ?",
                    (deployment_id,),
                ).fetchone()["value"]
            )
            conn.execute(
                """
                INSERT INTO runtime_logs (
                    id, project_id, deployment_id, instance_id, sequence,
                    stream, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id, project_id, deployment_id, instance_id, sequence,
                    stream, message, _utc_now(),
                ),
            )
            row = conn.execute("SELECT * FROM runtime_logs WHERE id = ?", (log_id,)).fetchone()
        return _row(row) or {}

    def logs_after(
        self,
        *,
        project_id: str,
        user_id: str,
        deployment_id: str,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        self.store.require_permission(project_id, user_id, "project.read")
        with self.store.connect() as conn:
            self._require_deployment(conn, project_id, deployment_id)
            rows = conn.execute(
                """
                SELECT * FROM runtime_logs
                WHERE project_id = ? AND deployment_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (project_id, deployment_id, max(0, after_sequence), max(1, min(limit, 2000))),
            ).fetchall()
        items = [_row(item) or {} for item in rows]
        return {
            "items": items,
            "cursor": items[-1]["sequence"] if items else max(0, after_sequence),
        }

    def _start_instance(
        self,
        deployment: dict[str, Any],
        version: dict[str, Any],
        replica_index: int,
        runtime_env: dict[str, str],
    ) -> dict[str, Any]:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", deployment["name"]).strip("-")
        runtime_name = (
            f"cp-{safe_name[:24]}-{deployment['id'][:8]}-{version['id'][:8]}-{replica_index}"
        ).lower()
        instance_id = str(uuid.uuid4())
        now = _utc_now()
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO runtime_instances (
                    id, project_id, deployment_id, version_id, replica_index,
                    runtime_kind, runtime_name, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'docker', ?, 'starting', ?)
                """,
                (
                    instance_id, deployment["project_id"], deployment["id"],
                    version["id"], replica_index, runtime_name, now,
                ),
            )
        env_keys = sorted(
            key for key in runtime_env
            if key in {"LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"}
            or key in self.secret_resolver(deployment["project_id"])
        )
        args = [
            "docker", "run", "-d", "--name", runtime_name,
            "--label", f"cloud-parity.deployment={deployment['id']}",
            "--label", f"cloud-parity.version={version['id']}",
            "--restart", "unless-stopped", "--network", self.network,
        ]
        for key in env_keys:
            args.extend(["-e", key])
        args.append(version["image_ref"])
        result = self.runner.run(args, env=runtime_env, timeout=120)
        if result.returncode != 0:
            self._mark_instance(instance_id, "failed", error=result.stderr[-4000:], stopped=True)
            raise RuntimeError(result.stderr.strip() or "docker run failed")
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM runtime_instances WHERE id = ?", (instance_id,)).fetchone()
        return _row(row) or {}

    def _wait_until_healthy(self, runtime_name: str) -> bool:
        deadline = time.monotonic() + self.health_timeout_seconds
        while time.monotonic() <= deadline:
            result = self.runner.run(
                [
                    "docker", "inspect", "--format",
                    "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                    runtime_name,
                ],
                timeout=10,
            )
            status = result.stdout.strip().lower()
            if result.returncode == 0 and status in {"healthy", "running"}:
                return True
            if status in {"unhealthy", "exited", "dead"}:
                return False
            if self.poll_interval_seconds:
                time.sleep(self.poll_interval_seconds)
        return False

    def _drain_previous_instances(
        self, *, project_id: str, deployment_id: str, keep_version_id: str
    ) -> None:
        with self.store.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runtime_instances
                WHERE project_id = ? AND deployment_id = ? AND version_id != ?
                  AND status = 'ready'
                ORDER BY created_at, id
                """,
                (project_id, deployment_id, keep_version_id),
            ).fetchall()
        for row in rows:
            instance = _row(row) or {}
            self._mark_instance(instance["id"], "draining")
            result = self.runner.run(
                [
                    "docker", "stop", "--time", str(self.drain_timeout_seconds),
                    instance["runtime_name"],
                ],
                timeout=self.drain_timeout_seconds + 30,
            )
            self._mark_instance(
                instance["id"],
                "stopped" if result.returncode == 0 else "failed",
                error=result.stderr[-4000:] if result.returncode else "",
                stopped=True,
            )

    def _mark_instance(
        self,
        instance_id: str,
        status: str,
        *,
        error: str = "",
        ready: bool = False,
        stopped: bool = False,
    ) -> None:
        now = _utc_now()
        with self.store.transaction() as conn:
            conn.execute(
                """
                UPDATE runtime_instances
                SET status = ?, error = ?,
                    ready_at = CASE WHEN ? THEN COALESCE(ready_at, ?) ELSE ready_at END,
                    stopped_at = CASE WHEN ? THEN COALESCE(stopped_at, ?) ELSE stopped_at END
                WHERE id = ?
                """,
                (status, error, int(ready), now, int(stopped), now, instance_id),
            )

    @staticmethod
    def _require_deployment(conn: Any, project_id: str, deployment_id: str) -> Any:
        row = conn.execute(
            "SELECT 1 FROM agent_deployments WHERE id = ? AND project_id = ?",
            (deployment_id, project_id),
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("deployment not found")
        return row
