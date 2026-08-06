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


def test_provider_dial_target_preserves_e164_without_prefix(monkeypatch) -> None:
    monkeypatch.delenv("QWEN_SIP_DIAL_PREFIX", raising=False)
    monkeypatch.delenv("QWEN_SIP_STRIP_COUNTRY_CODE", raising=False)
    assert phone_agent._provider_dial_target("+8613812345678") == "+8613812345678"


def test_provider_dial_target_strips_country_code_without_prefix(monkeypatch) -> None:
    monkeypatch.delenv("QWEN_SIP_DIAL_PREFIX", raising=False)
    monkeypatch.setenv("QWEN_SIP_STRIP_COUNTRY_CODE", "86")
    assert phone_agent._provider_dial_target("+8613812345678") == "13812345678"


def test_provider_dial_target_applies_numeric_carrier_prefix(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_SIP_DIAL_PREFIX", "10012008")
    monkeypatch.delenv("QWEN_SIP_STRIP_COUNTRY_CODE", raising=False)
    assert phone_agent._provider_dial_target("+8613812345678") == "100120088613812345678"


def test_provider_dial_target_strips_carrier_country_code(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_SIP_DIAL_PREFIX", "10012008")
    monkeypatch.setenv("QWEN_SIP_STRIP_COUNTRY_CODE", "86")
    assert phone_agent._provider_dial_target("+8613812345678") == "1001200813812345678"


def test_provider_dial_target_rejects_invalid_prefix(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_SIP_DIAL_PREFIX", "carrier-")
    with pytest.raises(ValueError, match="digits only"):
        phone_agent._provider_dial_target("+8613812345678")


def test_provider_dial_target_uses_selected_carrier_profile(monkeypatch) -> None:
    monkeypatch.delenv("QWEN_SIP_DIAL_PREFIX", raising=False)
    monkeypatch.setenv("QWEN_SIP_STRIP_COUNTRY_CODE", "86")
    monkeypatch.setenv("QWEN_SIP_QINGSHANYUN_DIAL_PREFIX", "10012008")
    monkeypatch.setenv("QWEN_SIP_QINGSHANYUN_STRIP_COUNTRY_CODE", "86")

    assert (
        phone_agent._provider_dial_target("+8613812345678", "qingshanyun")
        == "1001200813812345678"
    )
    assert (
        phone_agent._provider_dial_target("+8613812345678", "qingchuanyun")
        == "13812345678"
    )


def test_provider_registration_profile_falls_back_to_primary(monkeypatch) -> None:
    monkeypatch.delenv("QWEN_SIP_QINGCHUANYUN_REGISTER_ENABLED", raising=False)
    monkeypatch.setenv("QWEN_SIP_QINGSHANYUN_REGISTER_ENABLED", "true")

    assert (
        phone_agent._provider_registration_env_prefix("qingshanyun")
        == "QWEN_SIP_QINGSHANYUN_REGISTER"
    )
    assert (
        phone_agent._provider_registration_env_prefix("qingchuanyun")
        == "QWEN_SIP_REGISTER"
    )


def test_registration_refreshes_before_3600_second_expiry(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_SIP_REGISTER_REFRESH_SECONDS", "3540")

    assert phone_agent._registration_refresh_seconds("QWEN_SIP_REGISTER", 3600) == 3540


def test_registration_refresh_rejects_interval_after_expiry(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_SIP_REGISTER_REFRESH_SECONDS", "3601")

    with pytest.raises(ValueError, match="between 30 and 3600"):
        phone_agent._registration_refresh_seconds("QWEN_SIP_REGISTER", 3600)


def test_outbound_sip_media_timeout_ends_missing_rtp_promptly(monkeypatch) -> None:
    monkeypatch.delenv("QWEN_SIP_MEDIA_TIMEOUT_SECONDS", raising=False)

    config = phone_agent._outbound_sip_media_config()

    assert config.media_timeout.seconds == 5
    assert config.only_listed_codecs is True
    assert [(codec.name, codec.rate) for codec in config.codecs] == [("PCMU", 8_000)]


def test_outbound_calls_use_agent_hangup_by_default(monkeypatch) -> None:
    monkeypatch.delenv("QWEN_OUTBOUND_CUSTOMER_HANGUP_ONLY", raising=False)

    assert phone_agent._customer_hangup_only({"direction": "outbound"}) is False
    assert phone_agent._customer_hangup_only({"direction": "inbound"}) is False


def test_customer_hangup_only_can_be_enabled(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_OUTBOUND_CUSTOMER_HANGUP_ONLY", "true")

    assert phone_agent._customer_hangup_only({"direction": "outbound"}) is True


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


def test_console_polling_is_jittered_and_capacity_safe(monkeypatch) -> None:
    monkeypatch.setenv("CLOUD_PARITY_CONSOLE_POLL_SECONDS", "2")

    first = phone_agent._console_poll_interval("session-a")
    second = phone_agent._console_poll_interval("session-b")

    assert 1.7 <= first <= 2.3
    assert 1.7 <= second <= 2.3
    assert first != second


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
    assert phone_agent._outbound_shutdown_transition(
        answered=True,
        reason="parent process shutdown",
        normal_disconnect=True,
    ) == ("completed", "")


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
