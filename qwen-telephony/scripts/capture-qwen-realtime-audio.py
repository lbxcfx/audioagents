#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import uuid
import wave

from dotenv import load_dotenv
import websockets


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEXT = (
    "您好，这是一段清晰度对比测试。请确认数字一二三四五，"
    "以及地址北京市朝阳区。谢谢。"
)


async def capture(output: Path, text: str) -> dict[str, object]:
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "qwen-telephony" / "config" / "dev.env", override=False)
    key = os.environ["DASHSCOPE_API_KEY"]
    endpoint = os.getenv(
        "QWEN_AUDIO_REALTIME_URL",
        "wss://llm-vfnjvqxp5829jfc6.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime",
    )
    model = os.getenv("QWEN_AUDIO_REALTIME_MODEL", "qwen-audio-3.0-realtime-plus")
    voice = os.getenv("QWEN_AUDIO_REALTIME_VOICE", "longanqian")
    separator = "&" if "?" in endpoint else "?"
    url = f"{endpoint}{separator}model={model}"
    pcm = bytearray()
    transcript: list[str] = []
    observed: list[str] = []

    async with websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {key}"},
        open_timeout=20,
        ping_interval=20,
        ping_timeout=20,
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "event_id": uuid.uuid4().hex,
                    "type": "session.update",
                    "session": {
                        "modalities": ["text", "audio"],
                        "voice": voice,
                        "input_audio_format": "pcm",
                        "output_audio_format": "pcm",
                        "turn_detection": None,
                    },
                },
                ensure_ascii=False,
            )
        )
        # DashScope requires the session update to be acknowledged before
        # conversation and response events are submitted.
        async with asyncio.timeout(20):
            async for raw in socket:
                event = json.loads(raw)
                event_type = str(event.get("type") or "")
                observed.append(event_type)
                if event_type == "error":
                    raise RuntimeError(json.dumps(event, ensure_ascii=False))
                if event_type == "session.updated":
                    break
        await socket.send(
            json.dumps(
                {
                    "event_id": uuid.uuid4().hex,
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"请只朗读下面这段文字，不要添加内容：{text}",
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )
        )
        await socket.send(
            json.dumps(
                {
                    "event_id": uuid.uuid4().hex,
                    "type": "response.create",
                },
                ensure_ascii=False,
            )
        )
        async with asyncio.timeout(60):
            async for raw in socket:
                event = json.loads(raw)
                event_type = str(event.get("type") or "")
                observed.append(event_type)
                if event_type in {"response.audio.delta", "response.output_audio.delta"}:
                    pcm.extend(base64.b64decode(event.get("delta") or ""))
                elif event_type in {
                    "response.audio_transcript.delta",
                    "response.output_audio_transcript.delta",
                }:
                    transcript.append(str(event.get("delta") or ""))
                elif event_type == "error":
                    raise RuntimeError(json.dumps(event, ensure_ascii=False))
                elif event_type in {"response.done", "response.completed"}:
                    break

    if not pcm:
        raise RuntimeError(f"Qwen returned no audio; observed events: {observed}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(pcm)
    return {
        "file": str(output),
        "model": model,
        "voice": voice,
        "sample_rate": 24_000,
        "duration_seconds": round(len(pcm) / 2 / 24_000, 3),
        "transcript": "".join(transcript),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "qwen-telephony/artifacts/audio-comparison/qwen-realtime-direct.wav",
    )
    parser.add_argument("--text", default=DEFAULT_TEXT)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(capture(args.output, args.text)), ensure_ascii=False))


if __name__ == "__main__":
    main()
