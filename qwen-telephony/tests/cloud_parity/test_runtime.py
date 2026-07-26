from __future__ import annotations

from pathlib import Path
import sys

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.deployment import BuildOutcome, DeploymentService, SecretCipher
from server.cloud_parity.runtime import CommandResult, DockerRuntimeExecutor
from server.cloud_parity.store import PlatformStore


class Builder:
    def build(self, source_ref, image_ref):
        return BuildOutcome(True, logs="ok")


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.health = "running"

    def run(self, args, *, env=None, timeout=60):
        self.calls.append((list(args), dict(env or {})))
        if args[:2] == ["docker", "run"]:
            return CommandResult(0, "container-id\n", "")
        if args[:2] == ["docker", "inspect"]:
            return CommandResult(0, self.health + "\n", "")
        if args[:2] == ["docker", "logs"]:
            return CommandResult(0, "2026-07-25T10:00:00Z started\n2026-07-25T10:00:01Z ready\n", "")
        return CommandResult(0, "", "")


@pytest.fixture()
def runtime_stack(tmp_path: Path):
    store = PlatformStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    project = store.create_project(name="Runtime", slug="runtime", owner_id="owner")
    runner = FakeRunner()
    service = DeploymentService(store, SecretCipher.generate(), build_executor=Builder())
    service.put_secret(
        project_id=project["id"], actor_id="owner", name="PRIVATE_KEY", value="hidden-value"
    )
    runtime = DockerRuntimeExecutor(
        store,
        service.resolve_secrets,
        runner=runner,
        health_timeout_seconds=0.1,
        poll_interval_seconds=0,
        drain_timeout_seconds=10,
    )
    service.runtime_executor = runtime
    agent = service.create_agent(
        project_id=project["id"], actor_id="owner", name="runtime-agent"
    )
    return store, service, runtime, project, agent, runner


def build(service, project, agent, tag):
    return service.build_version(
        project_id=project["id"], actor_id="owner", agent_id=agent["id"],
        source_ref=tag, image_ref=f"registry/agent:{tag}",
    )["version"]


def test_rollout_starts_new_replicas_before_draining_old(runtime_stack) -> None:
    _, service, runtime, project, agent, runner = runtime_stack
    v1 = build(service, project, agent, "v1")
    v2 = build(service, project, agent, "v2")
    deployment = service.create_deployment(
        project_id=project["id"], actor_id="owner", agent_id=agent["id"],
        version_id=v1["id"], name="production", desired_replicas=2,
    )
    runner.calls.clear()
    updated = service.rollout(
        project_id=project["id"], actor_id="owner",
        deployment_id=deployment["id"], version_id=v2["id"],
    )

    assert updated["status"] == "ready"
    commands = [call[0] for call in runner.calls]
    run_positions = [i for i, command in enumerate(commands) if command[:2] == ["docker", "run"]]
    stop_positions = [i for i, command in enumerate(commands) if command[:2] == ["docker", "stop"]]
    assert len(run_positions) == 2
    assert len(stop_positions) == 2
    assert max(run_positions) < min(stop_positions)
    instances = runtime.list_instances(
        project_id=project["id"], user_id="owner", deployment_id=deployment["id"]
    )
    assert sum(item["status"] == "ready" for item in instances) == 2
    assert sum(item["status"] == "stopped" for item in instances) == 2


def test_failed_new_revision_does_not_stop_old_instances(runtime_stack) -> None:
    _, service, runtime, project, agent, runner = runtime_stack
    v1 = build(service, project, agent, "v1")
    v2 = build(service, project, agent, "v2")
    deployment = service.create_deployment(
        project_id=project["id"], actor_id="owner", agent_id=agent["id"],
        version_id=v1["id"], name="safe", desired_replicas=1,
    )
    runner.calls.clear()
    runner.health = "unhealthy"
    failed = service.rollout(
        project_id=project["id"], actor_id="owner",
        deployment_id=deployment["id"], version_id=v2["id"],
    )

    assert failed["status"] == "failed"
    assert failed["active_version_id"] == v1["id"]
    assert not any(call[0][:2] == ["docker", "stop"] for call in runner.calls)
    old = runtime.list_instances(
        project_id=project["id"], user_id="owner", deployment_id=deployment["id"]
    )
    assert any(item["version_id"] == v1["id"] and item["status"] == "ready" for item in old)


def test_secret_values_are_passed_via_environment_not_command_arguments(runtime_stack) -> None:
    _, service, _, project, agent, runner = runtime_stack
    version = build(service, project, agent, "secret")
    service.create_deployment(
        project_id=project["id"], actor_id="owner", agent_id=agent["id"],
        version_id=version["id"], name="secret", desired_replicas=1,
    )
    docker_run, env = next(call for call in runner.calls if call[0][:2] == ["docker", "run"])
    assert "PRIVATE_KEY" in docker_run
    assert "hidden-value" not in docker_run
    assert env["PRIVATE_KEY"] == "hidden-value"


def test_runtime_logs_use_incremental_cursor(runtime_stack) -> None:
    _, service, runtime, project, agent, _ = runtime_stack
    version = build(service, project, agent, "logs")
    deployment = service.create_deployment(
        project_id=project["id"], actor_id="owner", agent_id=agent["id"],
        version_id=version["id"], name="logs", desired_replicas=1,
    )
    assert runtime.collect_logs(
        project_id=project["id"], user_id="owner", deployment_id=deployment["id"]
    ) == 2
    first = runtime.logs_after(
        project_id=project["id"], user_id="owner", deployment_id=deployment["id"], limit=1
    )
    second = runtime.logs_after(
        project_id=project["id"], user_id="owner", deployment_id=deployment["id"],
        after_sequence=first["cursor"],
    )
    assert [item["sequence"] for item in first["items"]] == [1]
    assert [item["sequence"] for item in second["items"]] == [2]
