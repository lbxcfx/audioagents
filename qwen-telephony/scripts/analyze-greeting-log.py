from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--started-utc", required=True)
    parser.add_argument("--max-first-frame-seconds", type=float, default=3.0)
    args = parser.parse_args()

    started = datetime.fromisoformat(args.started_utc)
    events: list[tuple[datetime, str, str]] = []

    for line in Path(args.log).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            ts = datetime.fromisoformat(obj["timestamp"])
        except Exception:
            continue
        if ts < started:
            continue
        msg = obj.get("message", "")
        if any(
            token in msg
            for token in (
                "received job request",
                "PhoneAgent.on_enter",
                "Greeting direct playback",
                "Greeting audio first frame decoded",
                "Greeting audio file decoded",
                "LLM warm-up",
            )
        ):
            events.append((ts, msg, obj.get("job_id", "")))

    for ts, msg, job in events:
        print(f"{ts.isoformat()} {job} {msg}")

    on_enter = next(
        (ts for ts, msg, _ in events if "PhoneAgent.on_enter: scheduling greeting" in msg),
        None,
    )
    direct_completed = next(
        (ts for ts, msg, _ in events if "Greeting direct playback completed" in msg),
        None,
    )
    direct_first = next(
        (ts for ts, msg, _ in events if "Greeting direct playback first frame queued" in msg),
        None,
    )
    if direct_first and direct_completed:
        delay = (direct_first - events[0][0]).total_seconds() if events else 0.0
        print(f"RESULT direct_first_frame_after_job={delay:.3f}s")
        return 0 if delay <= args.max_first_frame_seconds else 3

    first = next(
        (ts for ts, msg, _ in events if "Greeting audio first frame decoded" in msg),
        None,
    )
    if not on_enter or not first:
        print("RESULT fail missing greeting timing")
        return 2

    delay = (first - on_enter).total_seconds()
    print(f"RESULT first_frame_after_on_enter={delay:.3f}s")
    return 0 if delay <= args.max_first_frame_seconds else 3


if __name__ == "__main__":
    sys.exit(main())
