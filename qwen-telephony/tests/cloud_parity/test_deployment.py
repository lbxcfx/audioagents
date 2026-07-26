from __future__ import annotations

from pathlib import Path
import sys

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.deployment import (
    BuildOutcome,
    DeploymentService,
    RolloutOutcome,
    SecretCipher,
)
from server.cloud_parity.store import PlatformStore


class FakeBuilder:
    def __init__(self):
        self.should_fail = False

    def build(self, source_ref: str, image_ref: str) -> BuildOutcome:
        if self.should_fail:
            return BuildOutcome(False, logs="compile failed", error="compile failed")
        return BuildOutcome(True, logs=f"built {source_ref} as {image_ref}")


class FakeRuntime:
    def __init__(self):
        self.should_fail = False
        self.calls: list[str] = []

    def rollout(self, deployment, version) -> RolloutOutcome:
        self.calls.append(version["id"])
        if self.should_fail:
            return RolloutOutcome(False, False, "health check failed")
        return RolloutOutcome(True, True, "healthy")


@pytest.fixture()
def deployment_stack(tmp_path: Path):
    store = PlatformStore(tmp_path / "deploy.sqlite3")
    store.initialize()
    project = store.create_project(name="Deploy", slug="deploy", owner_id="owner")
    builder = FakeBuilder()
    runtime = FakeRuntime()
    service = DeploymentService(
        store,
        SecretCipher.generate(),
        build_executor=builder,
        runtime_executor=runtime,
    )
    agent = service.create_agent(
        project_id=project["id"], actor_id="owner", name="support-agent"
    )
    return store, service, project, agent, builder, runtime


def build(service, project, agent, tag):
    result = service.build_version(
        project_id=project["id"],
        actor_id="owner",
        agent_id=agent["id"],
        source_ref=f"source-{tag}",
        image_ref=f"registry/agent:{tag}",
        spec={"tag": tag},
    )
    assert result["build"]["status"] == "succeeded"
    return result["version"]


def test_successful_build_creates_immutable_numbered_version(deployment_stack) -> None:
    _, service, project, agent, _, _ = deployment_stack
    first = build(service, project, agent, "v1")
    second = build(service, project, agent, "v2")

    assert first["version_number"] == 1
    assert second["version_number"] == 2
    assert first["spec"] == {"tag": "v1"}


def test_failed_build_does_not_create_version(deployment_stack) -> None:
    _, service, project, agent, builder, _ = deployment_stack
    builder.should_fail = True

    result = service.build_version(
        project_id=project["id"],
        actor_id="owner",
        agent_id=agent["id"],
        source_ref="broken",
        image_ref="registry/agent:broken",
    )

    assert result["build"]["status"] == "failed"
    assert result["version"] is None


def test_failed_rollout_keeps_previous_active_version_and_rollback_reuses_image(deployment_stack) -> None:
    _, service, project, agent, _, runtime = deployment_stack
    v1 = build(service, project, agent, "v1")
    v2 = build(service, project, agent, "v2")
    deployment = service.create_deployment(
        project_id=project["id"],
        actor_id="owner",
        agent_id=agent["id"],
        version_id=v1["id"],
        name="production",
    )
    assert deployment["active_version_id"] == v1["id"]

    runtime.should_fail = True
    failed = service.rollout(
        project_id=project["id"],
        actor_id="owner",
        deployment_id=deployment["id"],
        version_id=v2["id"],
    )
    assert failed["status"] == "failed"
    assert failed["active_version_id"] == v1["id"]

    runtime.should_fail = False
    ready = service.rollout(
        project_id=project["id"],
        actor_id="owner",
        deployment_id=deployment["id"],
        version_id=v2["id"],
    )
    assert ready["previous_version_id"] == v1["id"]
    rolled_back = service.rollback(
        project_id=project["id"], actor_id="owner", deployment_id=deployment["id"]
    )
    assert rolled_back["active_version_id"] == v1["id"]
    assert runtime.calls[-1] == v1["id"]


def test_secrets_are_encrypted_and_never_return_plaintext(deployment_stack) -> None:
    store, service, project, _, _, _ = deployment_stack
    metadata = service.put_secret(
        project_id=project["id"],
        actor_id="owner",
        name="QWEN_API_KEY",
        value="very-secret-value",
    )

    assert "ciphertext" not in metadata
    assert "value" not in metadata
    assert service.list_secrets(project_id=project["id"], user_id="owner")[0]["name"] == "QWEN_API_KEY"
    with store.connect() as conn:
        stored = conn.execute("SELECT ciphertext FROM encrypted_secrets").fetchone()["ciphertext"]
    assert "very-secret-value" not in stored
    assert service.resolve_secrets(project["id"]) == {"QWEN_API_KEY": "very-secret-value"}
