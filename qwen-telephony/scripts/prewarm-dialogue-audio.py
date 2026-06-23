from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]
QWEN_DIR = ROOT / "qwen-telephony"
DB_PATH = QWEN_DIR / "data" / "ops.sqlite3"

sys.path.insert(0, str(QWEN_DIR / "agent"))

from qwen_providers import QwenTTS  # noqa: E402


def load_env() -> None:
    load_dotenv(ROOT / ".env")
    load_dotenv(QWEN_DIR / "config" / "local.env", override=False)
    os.environ.setdefault("QWEN_TTS_USE_SSE", "false")
    os.environ.setdefault("QWEN_TTS_CACHE_ENABLED", "true")


def scene_texts(scene_id: int | None) -> list[tuple[int, str, str, str]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        params: tuple[int, ...] = (scene_id,) if scene_id else ()
        where = "WHERE s.id = ?" if scene_id else ""
        rows = conn.execute(
            f"""
            SELECT s.id AS scene_id, s.name AS scene_name, v.flow_json
            FROM dialogue_scenes s
            JOIN dialogue_versions v ON v.id = s.active_version_id
            {where}
            ORDER BY s.id
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    texts: list[tuple[int, str, str, str]] = []
    for row in rows:
        flow = json.loads(row["flow_json"])
        for node in flow.get("nodes", []):
            text = (node.get("text") or "").strip()
            if text:
                texts.append((row["scene_id"], row["scene_name"], node.get("id", ""), text))
    return texts


async def prewarm(scene_id: int | None) -> int:
    load_env()
    texts = scene_texts(scene_id)
    if not texts:
        print("No dialogue texts found.")
        return 0

    tts = QwenTTS()
    try:
        created = 0
        for index, (sid, scene_name, node_id, text) in enumerate(texts, start=1):
            cached = tts.cached_audio_bytes(text)
            if cached:
                print(f"[{index}/{len(texts)}] cache hit scene={sid} node={node_id}")
                continue
            await tts.synthesize_audio_bytes(text)
            created += 1
            print(f"[{index}/{len(texts)}] generated scene={sid} node={node_id} name={scene_name}")
        print(f"Dialogue audio prewarm finished. generated={created}, total={len(texts)}")
        return created
    finally:
        await tts.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene-id", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(prewarm(args.scene_id))


if __name__ == "__main__":
    main()
