#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
from statistics import mean, median
from time import perf_counter

from openai import AsyncOpenAI


PROMPT = "请用一句话说明保持充足睡眠的好处。"
SYSTEM_PROMPT = "你是中文电话助手，只用一句简短中文回答，不要展示思考过程。"


async def run_case(
    *,
    name: str,
    model: str,
    base_url: str,
    runs: int,
    extra_body: dict | None = None,
) -> dict:
    client = AsyncOpenAI(api_key=os.environ["DASHSCOPE_API_KEY"], base_url=base_url)
    rows: list[dict] = []
    try:
        for run in range(1, runs + 1):
            started = perf_counter()
            first_content_at: float | None = None
            first_reasoning_at: float | None = None
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            stream = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": PROMPT},
                ],
                temperature=0,
                max_tokens=80,
                stream=True,
                timeout=30,
                extra_body=extra_body or {},
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    if first_reasoning_at is None:
                        first_reasoning_at = perf_counter()
                    reasoning_parts.append(reasoning)
                if delta.content:
                    if first_content_at is None:
                        first_content_at = perf_counter()
                    content_parts.append(delta.content)
            completed_at = perf_counter()
            rows.append(
                {
                    "run": run,
                    "first_content_ms": None
                    if first_content_at is None
                    else round((first_content_at - started) * 1000, 1),
                    "complete_ms": round((completed_at - started) * 1000, 1),
                    "reasoning_returned": bool(reasoning_parts),
                    "first_reasoning_ms": None
                    if first_reasoning_at is None
                    else round((first_reasoning_at - started) * 1000, 1),
                    "text": "".join(content_parts),
                }
            )
    finally:
        await client.close()

    first_values = [row["first_content_ms"] for row in rows if row["first_content_ms"] is not None]
    complete_values = [row["complete_ms"] for row in rows]
    return {
        "name": name,
        "model": model,
        "runs": rows,
        "average_first_content_ms": round(mean(first_values), 1),
        "median_first_content_ms": round(median(first_values), 1),
        "average_complete_ms": round(mean(complete_values), 1),
        "median_complete_ms": round(median(complete_values), 1),
    }


async def main() -> None:
    runs = int(os.getenv("LLM_BENCHMARK_RUNS", "5"))
    current = await run_case(
        name="current",
        model=os.getenv("QWEN_LLM_MODEL", "qwen-plus"),
        base_url=os.getenv(
            "QWEN_LLM_BASE_URL",
            "https://llm-vfnjvqxp5829jfc6.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        ),
        runs=runs,
    )
    flash = await run_case(
        name="flash-thinking-disabled",
        model="qwen3.7-flash",
        base_url="https://llm-vfnjvqxp5829jfc6.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        runs=runs,
        extra_body={"enable_thinking": False},
    )
    print(json.dumps({"prompt": PROMPT, "results": [current, flash]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
