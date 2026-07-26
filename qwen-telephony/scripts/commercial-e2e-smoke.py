from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

import httpx


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def bearer_token() -> str:
    token_file = os.getenv("COMMERCIAL_E2E_BEARER_TOKEN_FILE", "").strip()
    if token_file:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    else:
        token = os.getenv("COMMERCIAL_E2E_BEARER_TOKEN", "").strip()
    if not token:
        raise SystemExit("COMMERCIAL_E2E_BEARER_TOKEN or its file is required")
    return token


def main() -> None:
    base_url = required("COMMERCIAL_E2E_BASE_URL").rstrip("/")
    project_id = required("COMMERCIAL_E2E_PROJECT_ID")
    destination = required("COMMERCIAL_E2E_DESTINATION_NUMBER")
    source = required("COMMERCIAL_E2E_SOURCE_NUMBER")
    agent_name = os.getenv("COMMERCIAL_E2E_AGENT_NAME", "commercial-agent").strip()
    timeout_seconds = int(os.getenv("COMMERCIAL_E2E_TIMEOUT_SECONDS", "180"))
    headers = {"Authorization": f"Bearer {bearer_token()}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=10) as client:
        ready = client.get("/api/platform/health/ready")
        ready.raise_for_status()
        trunks = client.get(
            f"/api/platform/projects/{project_id}/telephony/trunks"
        )
        trunks.raise_for_status()
        candidates = [
            item for item in trunks.json().get("items", [])
            if item.get("status") == "active"
            and item.get("direction") in {"outbound", "bidirectional"}
            and item.get("livekit_trunk_id")
            and ("*" in item.get("numbers", []) or source in item.get("numbers", []))
        ]
        if not candidates:
            raise SystemExit("no active outbound trunk allowlists the requested source number")
        call = client.post(
            f"/api/platform/projects/{project_id}/telephony/calls/outbound",
            json={
                "idempotency_key": f"commercial-smoke-{uuid.uuid4().hex}",
                "destination_number": destination,
                "source_number": source,
                "agent_name": agent_name,
                "trunk_id": candidates[0]["id"],
                "priority": 0,
                "max_attempts": 1,
                "metadata": {"test": "commercial-e2e-smoke"},
            },
        )
        call.raise_for_status()
        call_id = call.json()["id"]
        deadline = time.monotonic() + timeout_seconds
        latest = call.json()
        while time.monotonic() < deadline:
            response = client.get(
                f"/api/platform/projects/{project_id}/telephony/calls/{call_id}"
            )
            response.raise_for_status()
            latest = response.json()
            if latest["status"] in {
                "completed", "failed", "busy", "no_answer", "canceled", "blocked"
            }:
                break
            time.sleep(2)
        print(json.dumps(latest, ensure_ascii=False, indent=2))
        if latest.get("status") != "completed":
            raise SystemExit(f"commercial call did not complete successfully: {latest.get('status')}")


if __name__ == "__main__":
    main()
