from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
AGENT_DIR = PROJECT_DIR / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import phone_agent


def _payload() -> dict[str, str]:
    return {
        "kind": "telephony.outbound",
        "project_id": "project-1",
        "call_id": "call-1",
        "worker_id": "worker-1",
        "lease_token": "lease-token",
        "phone_number": "+8613800000001",
        "livekit_trunk_id": "ST_primary",
    }


def test_phone_agent_authenticates_and_decrypts_dispatch_metadata(monkeypatch) -> None:
    key = Fernet.generate_key()
    monkeypatch.setenv("CLOUD_PARITY_DISPATCH_METADATA_KEY", key.decode("ascii"))
    encrypted = Fernet(key).encrypt(
        json.dumps(_payload(), separators=(",", ":")).encode("utf-8")
    )

    parsed = phone_agent._outbound_job("enc:v1:" + encrypted.decode("ascii"))

    assert parsed == _payload()


def test_phone_agent_rejects_tampered_dispatch_metadata(monkeypatch) -> None:
    key = Fernet.generate_key()
    monkeypatch.setenv("CLOUD_PARITY_DISPATCH_METADATA_KEY", key.decode("ascii"))

    with pytest.raises(RuntimeError, match="authentication failed"):
        phone_agent._outbound_job("enc:v1:not-a-valid-fernet-token")


def test_phone_agent_heartbeat_stays_safely_inside_lease() -> None:
    assert phone_agent._telephony_heartbeat_interval(
        {"heartbeat_seconds": 10, "lease_seconds": 10}
    ) == pytest.approx(10 / 3)
    assert phone_agent._telephony_heartbeat_interval(
        {"heartbeat_seconds": 2, "lease_seconds": 30}
    ) == 2


def test_phone_agent_shutdown_does_not_complete_unanswered_call() -> None:
    assert phone_agent._outbound_shutdown_transition(
        answered=False, reason="worker shutdown"
    ) == ("reconciling", "agent_shutdown_before_answer")
    assert phone_agent._outbound_shutdown_transition(
        answered=True, reason="participant disconnected"
    ) == ("completed", "")
    assert phone_agent._outbound_shutdown_transition(
        answered=True, reason="worker shutdown"
    ) == ("failed", "agent_runtime_terminated")


def test_phone_agent_executes_console_dtmf_and_say_commands() -> None:
    sent: list[tuple[int, str]] = []
    spoken: list[str] = []

    class Participant:
        async def publish_dtmf(self, *, code: int, digit: str) -> None:
            sent.append((code, digit))

    class Speech:
        async def wait_for_playout(self) -> None:
            return None

    class Session:
        def say(self, text: str, **_options):
            spoken.append(text)
            return Speech()

    context = SimpleNamespace(
        room=SimpleNamespace(local_participant=Participant()),
        shutdown=lambda **_options: None,
    )
    dtmf = asyncio.run(
        phone_agent._execute_console_command(
            context,
            Session(),
            {"command_type": "dtmf", "payload": {"digits": "10#A"}},
        )
    )
    say = asyncio.run(
        phone_agent._execute_console_command(
            context,
            Session(),
            {
                "command_type": "rpc",
                "payload": {"method": "agent.say", "arguments": {"text": "请稍候"}},
            },
        )
    )

    assert sent == [(1, "1"), (0, "0"), (11, "#"), (12, "A")]
    assert dtmf == {"digits_sent": 4}
    assert spoken == ["请稍候"]
    assert say == {"spoken": True}


def test_phone_agent_executes_managed_console_transfer(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def transfer(ctx, job, sip_identity, destination_name, reason):
        captured.update(
            {
                "ctx": ctx,
                "job": job,
                "sip_identity": sip_identity,
                "destination_name": destination_name,
                "reason": reason,
            }
        )

    monkeypatch.setattr(phone_agent, "_execute_managed_transfer", transfer)
    context = SimpleNamespace(room=SimpleNamespace(local_participant=object()))
    job = _payload()

    result = asyncio.run(
        phone_agent._execute_console_command(
            context,
            SimpleNamespace(),
            {
                "command_type": "rpc",
                "payload": {
                    "method": "call.transfer",
                    "arguments": {
                        "destination_name": "human-support",
                        "reason": "客户要求人工服务",
                    },
                },
            },
            managed_job=job,
            sip_identity="sip-caller",
        )
    )

    assert result == {
        "transfer_completed": True,
        "destination_name": "human-support",
    }
    assert captured["job"] == job
    assert captured["sip_identity"] == "sip-caller"
    assert captured["destination_name"] == "human-support"
