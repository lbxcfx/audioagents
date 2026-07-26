from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_DIR = Path(__file__).resolve().parents[2]
KUBERNETES_DIR = PROJECT_DIR / "deploy" / "kubernetes"


def _documents(filename: str) -> list[dict]:
    with (KUBERNETES_DIR / filename).open(encoding="utf-8") as stream:
        return [item for item in yaml.safe_load_all(stream) if item]


def _by_kind_and_name(documents: list[dict]) -> dict[tuple[str, str], dict]:
    return {
        (item["kind"], item["metadata"]["name"]): item
        for item in documents
    }


def _container(deployment: dict) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def _secret_refs(container: dict) -> set[str]:
    return {
        item["secretRef"]["name"]
        for item in container.get("envFrom", [])
        if "secretRef" in item
    }


def test_workloads_only_receive_their_own_secret_scope() -> None:
    resources = _by_kind_and_name(_documents("commercial-stack.yaml"))
    control = _container(resources[("Deployment", "cloud-parity")])
    dispatcher_deployment = resources[("Deployment", "telephony-dispatcher")]
    dispatcher = _container(dispatcher_deployment)
    agent_deployment = resources[("Deployment", "phone-agent")]
    agent = _container(agent_deployment)

    assert _secret_refs(control) == {"cloud-parity-secrets"}
    assert _secret_refs(dispatcher) == {"cloud-parity-dispatcher-secrets"}
    assert _secret_refs(agent) == {"cloud-parity-agent-secrets"}

    dispatcher_volumes = {
        item["name"]: item for item in dispatcher_deployment["spec"]["template"]["spec"]["volumes"]
    }
    agent_volumes = {
        item["name"]: item for item in agent_deployment["spec"]["template"]["spec"]["volumes"]
    }
    assert dispatcher_volumes["service-token"]["secret"]["secretName"] == (
        "cloud-parity-dispatcher-service-token"
    )
    assert agent_volumes["service-token"]["secret"]["secretName"] == (
        "cloud-parity-agent-service-token"
    )


def test_secret_template_excludes_control_plane_keys_from_workers() -> None:
    secrets = _by_kind_and_name(_documents("secrets.example.yaml"))
    control_keys = set(secrets[("Secret", "cloud-parity-secrets")]["stringData"])
    dispatcher_keys = set(
        secrets[("Secret", "cloud-parity-dispatcher-secrets")]["stringData"]
    )
    agent_keys = set(secrets[("Secret", "cloud-parity-agent-secrets")]["stringData"])
    forbidden_worker_keys = {
        "CLOUD_PARITY_DATABASE_URL",
        "CLOUD_PARITY_MASTER_KEY",
        "CLOUD_PARITY_PHONE_HASH_KEY",
    }

    assert forbidden_worker_keys <= control_keys
    assert dispatcher_keys.isdisjoint(forbidden_worker_keys)
    assert agent_keys.isdisjoint(forbidden_worker_keys)
    assert "CLOUD_PARITY_DISPATCH_METADATA_KEY" in dispatcher_keys & agent_keys
    assert "DASHSCOPE_API_KEY" in agent_keys
    assert "DASHSCOPE_API_KEY" not in dispatcher_keys


def test_production_redundancy_and_fail_closed_drivers_are_pinned() -> None:
    resources = _by_kind_and_name(_documents("commercial-stack.yaml"))
    config = resources[("ConfigMap", "cloud-parity-config")]["data"]
    assert config["CLOUD_PARITY_BUILD_DRIVER"] == "disabled"
    assert config["CLOUD_PARITY_RUNTIME_DRIVER"] == "control-plane"
    assert int(config["CLOUD_PARITY_TELEPHONY_PROJECT_CONCURRENCY"]) >= 2
    assert int(config["CLOUD_PARITY_WORKER_SOURCE_REQUESTS_PER_MINUTE"]) >= 12_000

    for name in ("voice-console", "cloud-parity", "telephony-dispatcher", "phone-agent"):
        deployment = resources[("Deployment", name)]
        pod_spec = deployment["spec"]["template"]["spec"]
        assert deployment["spec"]["replicas"] >= 3
        assert deployment["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
        assert pod_spec["automountServiceAccountToken"] is False
        assert pod_spec["terminationGracePeriodSeconds"] >= 30
        assert resources[("PodDisruptionBudget", name)]["spec"]["minAvailable"] >= 2
        assert resources[("HorizontalPodAutoscaler", name)]["spec"]["minReplicas"] >= 3
