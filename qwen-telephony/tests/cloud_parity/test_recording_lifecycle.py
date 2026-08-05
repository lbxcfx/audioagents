from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


PROJECT_DIR = Path(__file__).resolve().parents[2]
AGENT_DIR = PROJECT_DIR / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import phone_agent


class FakeEgressService:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.polls = 0
        self.started = []

    async def start_room_composite_egress(self, request):
        self.started.append(request)
        return SimpleNamespace(egress_id="EG_chrome")

    async def list_egress(self, request):
        self.polls += 1
        status = self.statuses.pop(0)
        return SimpleNamespace(
            items=[SimpleNamespace(egress_id=request.egress_id, status=status)]
        )


def test_wait_for_egress_active_polls_until_media_pipeline_is_ready() -> None:
    service = FakeEgressService(
        [
            phone_agent.api.EgressStatus.EGRESS_STARTING,
            phone_agent.api.EgressStatus.EGRESS_ACTIVE,
        ]
    )
    ctx = SimpleNamespace(api=SimpleNamespace(egress=service))

    asyncio.run(
        phone_agent._wait_for_egress_active(
            ctx,
            "EG_test",
            timeout_seconds=1,
            poll_seconds=0,
        )
    )

    assert service.polls == 2


def test_wait_for_egress_active_rejects_terminal_state() -> None:
    service = FakeEgressService([phone_agent.api.EgressStatus.EGRESS_FAILED])
    ctx = SimpleNamespace(api=SimpleNamespace(egress=service))

    with pytest.raises(RuntimeError, match="EGRESS_FAILED"):
        asyncio.run(
            phone_agent._wait_for_egress_active(
                ctx,
                "EG_test",
                timeout_seconds=1,
                poll_seconds=0,
            )
        )


def test_recording_disclosure_waits_for_playout() -> None:
    events: list[str] = []

    class Speech:
        async def wait_for_playout(self) -> None:
            events.append("played")

    class Session:
        def say(self, text: str, *, allow_interruptions: bool):
            assert text == "本次通话将被录音"
            assert allow_interruptions is False
            events.append("say")
            return Speech()

    asyncio.run(
        phone_agent._play_recording_disclosure(
            Session(),
            {
                "recording_mode": "always",
                "recording_disclosure_text": "本次通话将被录音",
            },
        )
    )

    assert events == ["say", "played"]


def test_managed_recording_uses_chrome_room_composite(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_RECORDING_S3_BUCKET", "recordings")
    monkeypatch.setenv("QWEN_RECORDING_S3_REGION", "us-east-1")
    monkeypatch.setenv("QWEN_RECORDING_S3_ENDPOINT", "http://minio:9000")
    service = FakeEgressService([phone_agent.api.EgressStatus.EGRESS_ACTIVE])
    ctx = SimpleNamespace(
        room=SimpleNamespace(name="call-room"),
        api=SimpleNamespace(egress=service),
    )

    async def run():
        original = phone_agent._telephony_record_recording
        phone_agent._telephony_record_recording = lambda *args, **kwargs: asyncio.sleep(0)
        try:
            return await phone_agent._start_managed_recording(
                ctx,
                {
                    "recording_mode": "always",
                    "recording_disclosure_text": "本次通话将被录音",
                    "project_id": "project-1",
                    "call_id": "call-1",
                },
            )
        finally:
            phone_agent._telephony_record_recording = original

    egress_ids, storage_uri = asyncio.run(run())

    assert egress_ids == ["EG_chrome"]
    assert storage_uri == "s3://recordings/telephony-recordings/project-1/call-1.mp3"
    request = service.started[0]
    assert request.audio_only is True
    assert request.audio_mixing == phone_agent.api.AudioMixing.DUAL_CHANNEL_AGENT
    assert request.advanced.audio_codec == phone_agent.api.AudioCodec.AC_MP3
    assert request.advanced.audio_frequency == 16_000
    assert request.advanced.audio_bitrate == 64
    assert request.file_outputs[0].file_type == phone_agent.api.EncodedFileType.MP3
