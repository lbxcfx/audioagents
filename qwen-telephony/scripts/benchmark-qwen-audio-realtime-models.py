#!/usr/bin/env python3
"""Compare Qwen Audio Realtime Plus and Flash response latency.

The benchmark opens a fresh WebSocket session for every sample to match the
startup path of a phone call. It sends the same text prompt, uses the same
voice, and records connection, session setup, first-text, first-audio, and
completion latency. Models are alternated between rounds to reduce ordering
bias.

Examples:
    python qwen-telephony/scripts/benchmark-qwen-audio-realtime-models.py
    python qwen-telephony/scripts/benchmark-qwen-audio-realtime-models.py \
        --runs 5 --json-out /tmp/qwen-realtime-benchmark.json
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
from statistics import mean, median, pstdev
import sys
from time import perf_counter
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

from dotenv import load_dotenv
import websockets


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENDPOINT = (
    "wss://llm-vfnjvqxp5829jfc6.cn-beijing.maas.aliyuncs.com/"
    "api-ws/v1/realtime"
)
DEFAULT_MODELS = (
    "qwen-audio-3.0-realtime-flash",
    "qwen-audio-3.0-realtime-plus",
)
DEFAULT_PROMPT = "请严格只回答四个字：测试完成"
MILLISECONDS = 1_000.0


def load_environment() -> None:
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "qwen-telephony" / "config" / "local.env", override=False)
    load_dotenv(ROOT / "qwen-telephony" / "config" / "dev.env", override=False)


def model_url(endpoint: str, model: str) -> str:
    parts = urlsplit(endpoint)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["model"] = model
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def elapsed_ms(started: float, ended: float | None) -> float | None:
    if ended is None:
        return None
    return round((ended - started) * MILLISECONDS, 1)


async def receive_until(
    socket: Any,
    *,
    expected: set[str],
    timeout: float,
) -> tuple[dict[str, Any], list[str]]:
    observed: list[str] = []
    async with asyncio.timeout(timeout):
        async for raw in socket:
            event = json.loads(raw)
            event_type = str(event.get("type") or "")
            observed.append(event_type)
            if event_type == "error":
                raise RuntimeError(json.dumps(event, ensure_ascii=False))
            if event_type in expected:
                return event, observed
    raise RuntimeError(f"connection ended before {sorted(expected)}; observed={observed}")


async def benchmark_once(
    *,
    model: str,
    run: int,
    endpoint: str,
    api_key: str,
    voice: str,
    prompt: str,
    timeout: float,
) -> dict[str, Any]:
    started_at = perf_counter()
    connected_at: float | None = None
    session_ready_at: float | None = None
    request_started_at: float | None = None
    first_text_at: float | None = None
    first_audio_at: float | None = None
    completed_at: float | None = None
    transcript: list[str] = []
    audio_bytes = 0
    response_events: list[str] = []

    try:
        async with websockets.connect(
            model_url(endpoint, model),
            additional_headers={"Authorization": f"Bearer {api_key}"},
            open_timeout=timeout,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        ) as socket:
            connected_at = perf_counter()
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
                            "instructions": (
                                "这是响应速度测试。必须严格按照用户要求回答，"
                                "不要解释，不要添加任何其他内容。"
                            ),
                        },
                    },
                    ensure_ascii=False,
                )
            )
            _, setup_events = await receive_until(
                socket, expected={"session.updated"}, timeout=timeout
            )
            session_ready_at = perf_counter()

            await socket.send(
                json.dumps(
                    {
                        "event_id": uuid.uuid4().hex,
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": prompt}],
                        },
                    },
                    ensure_ascii=False,
                )
            )
            request_started_at = perf_counter()
            await socket.send(
                json.dumps(
                    {
                        "event_id": uuid.uuid4().hex,
                        "type": "response.create",
                    }
                )
            )

            async with asyncio.timeout(timeout):
                async for raw in socket:
                    event = json.loads(raw)
                    event_type = str(event.get("type") or "")
                    response_events.append(event_type)
                    if event_type == "error":
                        raise RuntimeError(json.dumps(event, ensure_ascii=False))
                    if event_type in {
                        "response.audio_transcript.delta",
                        "response.output_audio_transcript.delta",
                        "response.text.delta",
                        "response.output_text.delta",
                    }:
                        delta = str(event.get("delta") or "")
                        if delta:
                            first_text_at = first_text_at or perf_counter()
                            transcript.append(delta)
                    elif event_type in {
                        "response.audio.delta",
                        "response.output_audio.delta",
                    }:
                        delta = event.get("delta") or ""
                        if delta:
                            first_audio_at = first_audio_at or perf_counter()
                            audio_bytes += len(base64.b64decode(delta))
                    elif event_type in {"response.done", "response.completed"}:
                        completed_at = perf_counter()
                        break

            if completed_at is None:
                raise RuntimeError(
                    "connection ended before response.done; "
                    f"observed={response_events}"
                )
            if first_audio_at is None:
                raise RuntimeError(
                    f"response contained no audio; observed={response_events}"
                )

        assert connected_at is not None
        assert session_ready_at is not None
        assert request_started_at is not None
        return {
            "run": run,
            "model": model,
            "ok": True,
            "connection_ms": elapsed_ms(started_at, connected_at),
            "session_setup_ms": elapsed_ms(connected_at, session_ready_at),
            "first_text_ms": elapsed_ms(request_started_at, first_text_at),
            "first_audio_ms": elapsed_ms(request_started_at, first_audio_at),
            "ready_to_first_audio_ms": elapsed_ms(started_at, first_audio_at),
            "complete_ms": elapsed_ms(request_started_at, completed_at),
            "audio_seconds": round(audio_bytes / 2 / 24_000, 3),
            "transcript": "".join(transcript).strip(),
            "setup_events": setup_events,
            "error": None,
        }
    except Exception as exc:
        return {
            "run": run,
            "model": model,
            "ok": False,
            "connection_ms": elapsed_ms(started_at, connected_at),
            "session_setup_ms": elapsed_ms(connected_at, session_ready_at)
            if connected_at is not None
            else None,
            "first_text_ms": elapsed_ms(request_started_at, first_text_at)
            if request_started_at is not None
            else None,
            "first_audio_ms": elapsed_ms(request_started_at, first_audio_at)
            if request_started_at is not None
            else None,
            "ready_to_first_audio_ms": elapsed_ms(started_at, first_audio_at),
            "complete_ms": elapsed_ms(request_started_at, completed_at)
            if request_started_at is not None
            else None,
            "audio_seconds": round(audio_bytes / 2 / 24_000, 3),
            "transcript": "".join(transcript).strip(),
            "setup_events": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def metric_summary(rows: list[dict[str, Any]], field: str) -> dict[str, float] | None:
    values = [float(row[field]) for row in rows if row.get("ok") and row.get(field) is not None]
    if not values:
        return None
    return {
        "mean": round(mean(values), 1),
        "median": round(median(values), 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "stddev": round(pstdev(values), 1),
    }


def summarize(rows: list[dict[str, Any]], models: tuple[str, ...]) -> dict[str, Any]:
    fields = (
        "connection_ms",
        "session_setup_ms",
        "first_text_ms",
        "first_audio_ms",
        "ready_to_first_audio_ms",
        "complete_ms",
    )
    result: dict[str, Any] = {}
    for model in models:
        model_rows = [row for row in rows if row["model"] == model]
        result[model] = {
            "successful_runs": sum(bool(row.get("ok")) for row in model_rows),
            "failed_runs": sum(not bool(row.get("ok")) for row in model_rows),
            "metrics_ms": {
                field: metric_summary(model_rows, field) for field in fields
            },
        }
    return result


def compact_model_name(model: str) -> str:
    return model.removeprefix("qwen-audio-3.0-realtime-")


def print_run(row: dict[str, Any]) -> None:
    label = compact_model_name(str(row["model"]))
    if not row["ok"]:
        print(f"[{label:5}] 第 {row['run']} 次：失败 - {row['error']}", flush=True)
        return
    print(
        f"[{label:5}] 第 {row['run']} 次："
        f"首音频 {row['first_audio_ms']:>7.1f} ms，"
        f"首文本 {str(row['first_text_ms']):>7} ms，"
        f"完整响应 {row['complete_ms']:>7.1f} ms，"
        f"回答={row['transcript']!r}",
        flush=True,
    )


def print_summary(summary: dict[str, Any], models: tuple[str, ...]) -> None:
    print("\n汇总（毫秒，多次结果以中位数为主）")
    print("模型   成功/总数   首音频中位数   首音频平均值   完整响应中位数")
    print("-----  ---------  -------------  -------------  ----------------")
    for model in models:
        item = summary[model]
        first_audio = item["metrics_ms"]["first_audio_ms"] or {}
        complete = item["metrics_ms"]["complete_ms"] or {}
        total = item["successful_runs"] + item["failed_runs"]
        print(
            f"{compact_model_name(model):5}  "
            f"{item['successful_runs']:>2}/{total:<6}  "
            f"{str(first_audio.get('median', '-')):>13}  "
            f"{str(first_audio.get('mean', '-')):>13}  "
            f"{str(complete.get('median', '-')):>16}"
        )

    if len(models) == 2:
        first = summary[models[0]]["metrics_ms"]["first_audio_ms"]
        second = summary[models[1]]["metrics_ms"]["first_audio_ms"]
        if first and second:
            delta = second["median"] - first["median"]
            percent = delta / first["median"] * 100 if first["median"] else 0.0
            faster = compact_model_name(models[0] if delta > 0 else models[1])
            print(
                f"\n首音频中位数差值：{abs(delta):.1f} ms；"
                f"{faster} 快 {abs(percent):.1f}%。"
            )


async def async_main(args: argparse.Namespace) -> int:
    load_environment()
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        print("错误：未配置 DASHSCOPE_API_KEY", file=sys.stderr)
        return 2

    endpoint = (args.endpoint or os.getenv("QWEN_AUDIO_REALTIME_URL") or DEFAULT_ENDPOINT).strip()
    voice = (args.voice or os.getenv("QWEN_AUDIO_REALTIME_VOICE") or "longanqian").strip()
    models = tuple(args.models)
    rows: list[dict[str, Any]] = []

    # Swap model order on even rounds to reduce transient network/load bias.
    for run in range(1, args.runs + 1):
        round_models = models if run % 2 else tuple(reversed(models))
        for model in round_models:
            row = await benchmark_once(
                model=model,
                run=run,
                endpoint=endpoint,
                api_key=api_key,
                voice=voice,
                prompt=args.prompt,
                timeout=args.timeout,
            )
            rows.append(row)
            print_run(row)

    result = {
        "configuration": {
            "runs_per_model": args.runs,
            "models": models,
            "voice": voice,
            "prompt": args.prompt,
            "method": "fresh WebSocket per sample; alternating model order",
        },
        "runs": rows,
        "summary": summarize(rows, models),
    }
    print_summary(result["summary"], models)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\n完整结果已写入：{args.json_out}")

    return 0 if all(row["ok"] for row in rows) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对比 Qwen Audio Realtime Flash 与 Plus 的响应速度"
    )
    parser.add_argument("--runs", type=int, default=5, help="每个模型测试次数（默认：5）")
    parser.add_argument(
        "--models",
        nargs="+",
        default=list(DEFAULT_MODELS),
        help="待比较的模型 ID",
    )
    parser.add_argument("--endpoint", help="Realtime WebSocket 地址；默认读取环境变量")
    parser.add_argument("--voice", help="输出音色；默认读取环境变量或 longanqian")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="每次发送的固定测试文本")
    parser.add_argument("--timeout", type=float, default=30.0, help="单阶段超时秒数")
    parser.add_argument("--json-out", type=Path, help="可选：保存完整 JSON 结果")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs 必须大于 0")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    return args


def main() -> None:
    raise SystemExit(asyncio.run(async_main(parse_args())))


if __name__ == "__main__":
    main()
