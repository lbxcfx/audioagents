from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_DIR / "scripts" / "telephony-dispatcher.py"
SPEC = importlib.util.spec_from_file_location("telephony_dispatcher", SCRIPT_PATH)
assert SPEC and SPEC.loader
dispatcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatcher)


class FakeControl:
    def __init__(self) -> None:
        self.transitions: list[tuple[str, dict]] = []
        self.observations: list[dict] = []
        self.heartbeats = 0

    async def transition(self, project_id, call, status, **fields):
        self.transitions.append((status, fields))
        return {**call, "status": status}

    async def observe(self, project_id, call, **fields):
        self.observations.append(fields)
        return fields

    async def heartbeat(self, project_id, call):
        self.heartbeats += 1
        return call


class FakeAgentDispatch:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests = []

    async def create_dispatch(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("simulated dispatch failure")
        return SimpleNamespace(id="dispatch-1")


def _call(**overrides):
    value = {
        "id": "call-1",
        "lease_token": "l" * 64,
        "agent_name": "commercial-agent",
        "destination_number": "+8613800000001",
        "source_number": "+8610000000000",
        "livekit_trunk_id": "ST_primary",
        "room_name": "",
    }
    value.update(overrides)
    return value


def _settings():
    return SimpleNamespace(
        worker_id="worker-1",
        heartbeat_seconds=10,
        reconciliation_grace_seconds=120,
    )


def test_dispatcher_marks_dispatching_before_livekit_side_effect() -> None:
    control = FakeControl()
    livekit = SimpleNamespace(agent_dispatch=FakeAgentDispatch())

    asyncio.run(
        dispatcher.dispatch_call(
            _settings(), control, livekit, "project-1", _call()
        )
    )

    assert control.transitions[0][0] == "dispatching"
    request = livekit.agent_dispatch.requests[0]
    metadata = json.loads(request.metadata)
    assert request.agent_name == "commercial-agent"
    assert request.room == "call-call-1"
    assert metadata["kind"] == "telephony.outbound"
    assert metadata["livekit_trunk_id"] == "ST_primary"
    assert "service_token" not in metadata


def test_dispatcher_encrypts_sensitive_livekit_metadata() -> None:
    control = FakeControl()
    livekit = SimpleNamespace(agent_dispatch=FakeAgentDispatch())
    settings = _settings()
    settings.dispatch_metadata_key = Fernet.generate_key().decode("ascii")

    asyncio.run(
        dispatcher.dispatch_call(settings, control, livekit, "project-1", _call())
    )

    raw_metadata = livekit.agent_dispatch.requests[0].metadata
    assert raw_metadata.startswith("enc:v1:")
    assert "+8613800000001" not in raw_metadata
    decrypted = Fernet(settings.dispatch_metadata_key.encode("ascii")).decrypt(
        raw_metadata.removeprefix("enc:v1:").encode("ascii")
    )
    assert json.loads(decrypted)["phone_number"] == "+8613800000001"


def test_dispatcher_metrics_token_uses_constant_time_bearer_check() -> None:
    expected = "m" * 32
    assert dispatcher.metrics_token_valid(f"Bearer {expected}", expected) is True
    assert dispatcher.metrics_token_valid("Bearer wrong", expected) is False
    assert dispatcher.metrics_token_valid("", expected) is False
    assert dispatcher.metrics_token_valid("", "") is True


def test_dispatcher_rejects_reused_production_encryption_key(monkeypatch) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("CLOUD_PARITY_ENV", "production")
    monkeypatch.setenv("CLOUD_PARITY_CONTROL_URL", "https://control.example.com")
    monkeypatch.setenv("CLOUD_PARITY_TELEPHONY_PROJECT_IDS", "project-1")
    monkeypatch.setenv("CLOUD_PARITY_SERVICE_BEARER_TOKEN", "service-token")
    monkeypatch.setenv("CLOUD_PARITY_MASTER_KEY", key)
    monkeypatch.setenv("CLOUD_PARITY_DISPATCH_METADATA_KEY", key)
    monkeypatch.setenv("CLOUD_PARITY_METRICS_TOKEN", "m" * 32)

    with pytest.raises(ValueError, match="must differ"):
        dispatcher.DispatcherSettings()


def test_dispatcher_fails_closed_for_missing_trunk_and_reconciles_dispatch_errors() -> None:
    missing_control = FakeControl()
    missing_livekit = SimpleNamespace(agent_dispatch=FakeAgentDispatch())
    asyncio.run(
        dispatcher.dispatch_call(
            _settings(),
            missing_control,
            missing_livekit,
            "project-1",
            _call(livekit_trunk_id=""),
        )
    )
    assert missing_control.transitions == [
        (
            "failed",
            {
                "failure_code": "outbound_trunk_missing",
                "failure_detail": "call has no active LiveKit outbound trunk",
                "retryable": False,
            },
        )
    ]
    assert missing_livekit.agent_dispatch.requests == []

    failed_control = FakeControl()
    failed_livekit = SimpleNamespace(agent_dispatch=FakeAgentDispatch(fail=True))
    asyncio.run(
        dispatcher.dispatch_call(
            _settings(), failed_control, failed_livekit, "project-1", _call()
        )
    )
    assert [item[0] for item in failed_control.transitions] == ["dispatching", "reconciling"]
    assert failed_control.transitions[-1][1] == {
        "failure_code": "agent_dispatch_result_uncertain",
        "failure_detail": "RuntimeError",
    }


class FakeRoomService:
    def __init__(self, participants=None, *, fail=False, missing=False) -> None:
        self.participants = list(participants or [])
        self.fail = fail
        self.missing = missing

    async def list_participants(self, _request):
        if self.fail:
            raise RuntimeError("simulated LiveKit outage")
        if self.missing:
            raise dispatcher.api.TwirpError("not_found", "room not found", status=404)
        return SimpleNamespace(participants=self.participants)


def test_reconciler_observes_live_call_without_redial() -> None:
    control = FakeControl()
    participant = SimpleNamespace(
        identity="sip-call-1",
        attributes={
            "sip.callID": "sip-id-1",
            "sip.callIDFull": "provider-id-1",
            "sip.callStatus": "active",
            "sip.phoneNumber": "+8613800000001",
        },
    )
    livekit = SimpleNamespace(room=FakeRoomService([participant]))
    call = _call(
        status="reconciling",
        room_name="call-call-1",
        reconcile_started_at="2026-01-01T00:00:00Z",
    )

    asyncio.run(
        dispatcher.reconcile_call(_settings(), control, livekit, "project-1", call)
    )

    assert control.transitions == []
    assert control.heartbeats == 1
    assert control.observations[0]["provider_call_id"] == "provider-id-1"
    assert control.observations[0]["sip_status"] == "active"


def test_reconciler_fails_safe_on_query_outage_and_terminalizes_after_grace() -> None:
    call = _call(
        status="reconciling",
        room_name="call-call-1",
        reconcile_started_at="2026-01-01T00:00:00Z",
        answered_at="2026-01-01T00:00:10Z",
    )
    outage_control = FakeControl()
    asyncio.run(
        dispatcher.reconcile_call(
            _settings(),
            outage_control,
            SimpleNamespace(room=FakeRoomService(fail=True)),
            "project-1",
            call,
        )
    )
    assert outage_control.heartbeats == 1
    assert outage_control.transitions == []

    ended_control = FakeControl()
    asyncio.run(
        dispatcher.reconcile_call(
            _settings(),
            ended_control,
            SimpleNamespace(room=FakeRoomService()),
            "project-1",
            call,
        )
    )
    assert ended_control.transitions[0][0] == "completed"

    missing_control = FakeControl()
    asyncio.run(
        dispatcher.reconcile_call(
            _settings(),
            missing_control,
            SimpleNamespace(room=FakeRoomService(missing=True)),
            "project-1",
            call,
        )
    )
    assert missing_control.heartbeats == 0
    assert missing_control.transitions[0][0] == "completed"
    assert "no longer exists" in missing_control.transitions[0][1]["failure_detail"]


def test_dispatcher_runtime_state_exposes_readiness_and_metrics() -> None:
    state = dispatcher.DispatcherRuntimeState(1.0)
    assert state.ready() is False
    state.failure()
    assert state.consecutive_errors == 1
    state.success(3)
    assert state.ready() is True
    rendered = state.prometheus()
    assert "telephony_dispatcher_ready 1" in rendered
    assert "telephony_dispatcher_jobs_claimed_total 3" in rendered
