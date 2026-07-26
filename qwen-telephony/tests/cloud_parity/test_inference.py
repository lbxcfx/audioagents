from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from server.cloud_parity.deployment import DeploymentService, SecretCipher
from server.cloud_parity.inference import InferenceGateway, InferenceResult
from server.cloud_parity.insights import InsightsService
from server.cloud_parity.store import PlatformStore, ResourceNotFoundError


class FakeAdapter:
    def __init__(self, name: str, fail: bool = False):
        self.name = name
        self.fail = fail
        self.calls = []

    async def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise TimeoutError(f"{self.name} unavailable")
        return InferenceResult(
            output={"text": f"reply-from-{self.name}"}, quantity=12, unit="tokens", cost_usd=0.01
        )


@pytest.fixture()
def gateway_stack(tmp_path: Path):
    store = PlatformStore(tmp_path / "inference.sqlite3")
    store.initialize()
    project = store.create_project(name="Inference", slug="inference", owner_id="owner")
    insights = InsightsService(store)
    session = insights.create_session(
        project_id=project["id"], actor_id="owner", room_name="inference-room"
    )
    deployment = DeploymentService(store, SecretCipher.generate())
    primary = FakeAdapter("primary", fail=True)
    fallback = FakeAdapter("fallback")
    gateway = InferenceGateway(
        store, deployment, insights, adapters={"primary": primary, "fallback": fallback}
    )
    return store, gateway, insights, project, session, primary, fallback


def add_route(gateway, project, provider, priority):
    return gateway.put_route(
        project_id=project["id"],
        actor_id="owner",
        descriptor="voice/default-llm",
        modality="llm",
        provider=provider,
        provider_model=f"{provider}-model",
        priority=priority,
        timeout_seconds=1,
    )


def test_primary_failure_uses_fallback_and_records_usage(gateway_stack) -> None:
    store, gateway, insights, project, session, primary, fallback = gateway_stack
    add_route(gateway, project, "primary", 10)
    add_route(gateway, project, "fallback", 20)

    result = asyncio.run(gateway.invoke(
        project_id=project["id"], actor_id="owner", descriptor="voice/default-llm",
        modality="llm", input_data={"messages": [{"role": "user", "content": "hello"}]},
        session_id=session["id"],
    ))

    assert result["provider"] == "fallback"
    assert result["output"]["text"] == "reply-from-fallback"
    assert result["fallbacks"] == [{"provider": "primary", "error_type": "TimeoutError"}]
    timeline = insights.timeline(
        project_id=project["id"], user_id="owner", session_id=session["id"]
    )
    assert timeline["usage"][0]["quantity"] == 12
    with store.connect() as conn:
        attempts = conn.execute(
            "SELECT status, error_type FROM inference_attempts ORDER BY created_at, rowid"
        ).fetchall()
    assert [(item["status"], item["error_type"]) for item in attempts] == [
        ("failed", "TimeoutError"), ("succeeded", "")
    ]


def test_inference_attempts_never_store_input_or_output(gateway_stack) -> None:
    store, gateway, _, project, _, primary, _ = gateway_stack
    primary.fail = False
    add_route(gateway, project, "primary", 10)
    asyncio.run(gateway.invoke(
        project_id=project["id"], actor_id="owner", descriptor="voice/default-llm",
        modality="llm", input_data={"messages": [{"role": "user", "content": "TOP_SECRET_PROMPT"}]},
    ))
    assert "TOP_SECRET_PROMPT" not in Path(store.database_path).read_bytes().decode("utf-8", errors="ignore")


def test_missing_descriptor_is_rejected(gateway_stack) -> None:
    _, gateway, _, project, _, _, _ = gateway_stack
    with pytest.raises(ResourceNotFoundError):
        asyncio.run(gateway.invoke(
            project_id=project["id"], actor_id="owner", descriptor="missing/model",
            modality="llm", input_data={"messages": [{"role": "user", "content": "hi"}]},
        ))


@pytest.mark.parametrize(
    ("modality", "input_data", "message"),
    [
        ("llm", {"text": "wrong shape"}, "messages"),
        ("stt", {"audio_base64": ""}, "audio_base64"),
        ("tts", {"text": ""}, "text"),
    ],
)
def test_invalid_inference_inputs_fail_before_provider_attempt(
    gateway_stack, modality, input_data, message
) -> None:
    _, gateway, _, project, _, primary, _ = gateway_stack
    with pytest.raises(ValueError, match=message):
        asyncio.run(
            gateway.invoke(
                project_id=project["id"],
                actor_id="owner",
                descriptor="voice/default-llm",
                modality=modality,
                input_data=input_data,
            )
        )
    assert primary.calls == []


def test_route_configuration_is_project_scoped(gateway_stack) -> None:
    store, gateway, _, project, _, primary, _ = gateway_stack
    primary.fail = False
    add_route(gateway, project, "primary", 10)
    other = store.create_project(name="Other", slug="other-inference", owner_id="other")
    with pytest.raises(ResourceNotFoundError):
        asyncio.run(gateway.invoke(
            project_id=other["id"], actor_id="other", descriptor="voice/default-llm",
            modality="llm", input_data={"messages": [{"role": "user", "content": "hi"}]},
        ))
