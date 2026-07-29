#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import audioop
import json
import os
from pathlib import Path
from statistics import mean
from time import perf_counter
import wave

from livekit import rtc
from livekit.agents import stt
from openai import AsyncOpenAI


ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "qwen-telephony" / "agent"
import sys

sys.path.insert(0, str(AGENT_DIR))
from qwen_providers import QwenRealtimeASR, QwenTTS  # noqa: E402


def load_pcm16_mono_16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as reader:
        pcm = reader.readframes(reader.getnframes())
        sample_rate = reader.getframerate()
        channels = reader.getnchannels()
        width = reader.getsampwidth()
    if width != 2:
        raise ValueError("benchmark WAV must use 16-bit PCM")
    if channels == 2:
        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    elif channels != 1:
        raise ValueError("benchmark WAV must be mono or stereo")
    if sample_rate != 16000:
        pcm, _ = audioop.ratecv(pcm, width, 1, sample_rate, 16000, None)
    return pcm


async def benchmark_asr(path: Path, runs: int) -> list[dict]:
    pcm = load_pcm16_mono_16k(path)
    frame_bytes = 320 * 2  # 20 ms at 16 kHz, mono, PCM16
    results: list[dict] = []
    provider = QwenRealtimeASR(language="zh")
    try:
        for run in range(1, runs + 1):
            stream = provider.stream(language="zh")
            first_partial_at: float | None = None
            final_at: float | None = None
            transcripts: list[str] = []

            async def receive() -> None:
                nonlocal first_partial_at, final_at
                async for event in stream:
                    now = perf_counter()
                    if event.type == stt.SpeechEventType.INTERIM_TRANSCRIPT and first_partial_at is None:
                        first_partial_at = now
                    if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                        final_at = now
                        text = event.alternatives[0].text if event.alternatives else ""
                        if text:
                            transcripts.append(text)

            started = perf_counter()
            receive_task = asyncio.create_task(receive())
            for offset in range(0, len(pcm), frame_bytes):
                chunk = pcm[offset : offset + frame_bytes]
                samples = len(chunk) // 2
                if not samples:
                    continue
                stream.push_frame(rtc.AudioFrame(chunk, 16000, 1, samples))
                await asyncio.sleep(samples / 16000)
            send_ended_at = perf_counter()
            stream.flush()
            deadline = perf_counter() + 10
            observed_final_at = final_at
            stable_since = perf_counter()
            while perf_counter() < deadline:
                await asyncio.sleep(0.05)
                if final_at != observed_final_at:
                    observed_final_at = final_at
                    stable_since = perf_counter()
                if final_at is not None and perf_counter() - stable_since >= 1.0:
                    break
            await stream.aclose()
            await asyncio.wait_for(receive_task, timeout=2)
            results.append(
                {
                    "run": run,
                    "audio_seconds": len(pcm) / 2 / 16000,
                    "send_seconds": send_ended_at - started,
                    "first_partial_from_start_ms": None
                    if first_partial_at is None
                    else (first_partial_at - started) * 1000,
                    "final_after_send_ms": (final_at - send_ended_at) * 1000 if final_at else None,
                    "total_ms": (final_at - started) * 1000 if final_at else None,
                    "final_segments": len(transcripts),
                    "transcript": "".join(transcripts),
                }
            )
    finally:
        await provider.aclose()
    return results


async def benchmark_llm(runs: int) -> list[dict]:
    client = AsyncOpenAI(
        api_key=os.environ["DASHSCOPE_API_KEY"],
        base_url=os.getenv(
            "QWEN_LLM_BASE_URL",
            "https://llm-vfnjvqxp5829jfc6.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        ),
    )
    model = os.getenv("QWEN_LLM_MODEL", "qwen-plus")
    results: list[dict] = []
    try:
        for run in range(1, runs + 1):
            started = perf_counter()
            first_text_at: float | None = None
            pieces: list[str] = []
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是中文电话助手，只用一句简短中文回答。"},
                    {"role": "user", "content": "请用一句话说明保持充足睡眠的好处。"},
                ],
                temperature=0,
                max_tokens=80,
                stream=True,
                timeout=30,
                extra_body={"enable_thinking": False},
            )
            async for chunk in response:
                content = chunk.choices[0].delta.content if chunk.choices else None
                if content:
                    if first_text_at is None:
                        first_text_at = perf_counter()
                    pieces.append(content)
            completed_at = perf_counter()
            results.append(
                {
                    "run": run,
                    "model": model,
                    "first_text_ms": (first_text_at - started) * 1000 if first_text_at else None,
                    "complete_ms": (completed_at - started) * 1000,
                    "text": "".join(pieces),
                }
            )
    finally:
        await client.close()
    return results


async def benchmark_tts(runs: int) -> list[dict]:
    os.environ["QWEN_TTS_CACHE_ENABLED"] = "false"
    provider = QwenTTS()
    results: list[dict] = []
    try:
        for run in range(1, runs + 1):
            text = "您好，这是一段用于测试语音合成响应速度的中文句子。"
            started = perf_counter()
            first_audio_at: float | None = None
            chunks = 0
            byte_count = 0
            async for chunk in provider.stream_audio_chunks(text):
                if first_audio_at is None:
                    first_audio_at = perf_counter()
                chunks += 1
                byte_count += len(chunk)
            completed_at = perf_counter()
            results.append(
                {
                    "run": run,
                    "model": provider.model,
                    "first_audio_ms": (first_audio_at - started) * 1000 if first_audio_at else None,
                    "complete_ms": (completed_at - started) * 1000,
                    "chunks": chunks,
                    "bytes": byte_count,
                }
            )
    finally:
        await provider.aclose()
    return results


def summarize(rows: list[dict], fields: list[str]) -> dict:
    summary: dict[str, float | None] = {}
    for field in fields:
        values = [float(row[field]) for row in rows if row[field] is not None]
        summary[field] = round(mean(values), 1) if values else None
    return summary


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--audio",
        type=Path,
        default=ROOT / "qwen-telephony" / "cache" / "greeting.wav",
    )
    args = parser.parse_args()
    asr_rows = await benchmark_asr(args.audio, args.runs)
    llm_rows = await benchmark_llm(args.runs)
    tts_rows = await benchmark_tts(args.runs)
    output = {
        "asr": asr_rows,
        "asr_average": summarize(
            asr_rows,
            ["first_partial_from_start_ms", "final_after_send_ms", "total_ms"],
        ),
        "llm": llm_rows,
        "llm_average": summarize(llm_rows, ["first_text_ms", "complete_ms"]),
        "tts": tts_rows,
        "tts_average": summarize(tts_rows, ["first_audio_ms", "complete_ms"]),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
