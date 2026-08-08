#!/usr/bin/env python3
"""Idempotently initialize one Compose deployment.

The generated IDs live in Docker volumes, not in the image or repository.
Re-running this script updates the configured trunk and policies without
creating duplicate projects or WeChat notification state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CONTROL_URL = os.environ.get("CONTROL_URL", "http://control-plane:8091").rstrip("/")
ADMIN_USER = os.environ.get("AUDIOAGENT_ADMIN_USER_ID", "audioagent-admin").strip()
WORKER_USER = os.environ.get("AUDIOAGENT_WORKER_USER_ID", "telephony-worker").strip()


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(
        f"{CONTROL_URL}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json", "X-User-ID": ADMIN_USER},
    )
    try:
        with urlopen(req, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc


def wait_for_control_plane() -> None:
    for attempt in range(60):
        try:
            request("GET", "/api/platform/health/ready")
            return
        except (RuntimeError, URLError):
            if attempt == 59:
                raise RuntimeError("control plane did not become ready")
            time.sleep(2)


def ensure_project() -> dict[str, Any]:
    slug = os.environ.get("AUDIOAGENT_PROJECT_SLUG", "wechat-audioagent").strip()
    projects = request("GET", "/api/platform/projects").get("items", [])
    for project in projects:
        if project.get("slug") == slug:
            return project
    return request(
        "POST",
        "/api/platform/projects",
        {
            "name": os.environ.get("AUDIOAGENT_PROJECT_NAME", "WeChat AudioAgent"),
            "slug": slug,
            "owner_id": ADMIN_USER,
            "retention_days": 90,
        },
    )


def configure_project(project_id: str) -> dict[str, Any]:
    request(
        "PUT",
        f"/api/platform/projects/{project_id}/members/{WORKER_USER}",
        {"user_id": WORKER_USER, "role": "worker"},
    )

    max_concurrent = int(os.environ.get("AUDIOAGENT_MAX_CONCURRENT_CALLS", "8"))
    max_cps = int(os.environ.get("AUDIOAGENT_MAX_CALLS_PER_SECOND", "2"))
    request(
        "PUT",
        f"/api/platform/projects/{project_id}/telephony/limits",
        {
            "max_concurrent_calls": max_concurrent,
            "max_outbound_calls": max_concurrent,
            "max_inbound_calls": max_concurrent,
            "max_calls_per_minute": max(60, max_cps * 60),
            "lease_seconds": 30,
        },
    )
    request(
        "PUT",
        f"/api/platform/projects/{project_id}/telephony/policy",
        {
            "outbound_enabled": True,
            "timezone": "Asia/Shanghai",
            "allowed_weekdays": [0, 1, 2, 3, 4, 5, 6],
            "calling_window_start": "00:00",
            "calling_window_end": "00:00",
            "require_consent": False,
            "consent_purpose": "outbound",
            "max_attempts_per_number_per_day": 0,
            "inbound_overflow_mode": "reject",
            "inbound_overflow_destination_name": "",
            "recording_mode": "always",
            "recording_disclosure_text": "",
        },
    )

    trunk_name = os.environ.get("AUDIOAGENT_TRUNK_NAME", "primary-outbound").strip()
    source_number = os.environ.get("AUDIOAGENT_SOURCE_NUMBER", "").strip()
    return request(
        "PUT",
        f"/api/platform/projects/{project_id}/telephony/trunks/{trunk_name}",
        {
            "name": trunk_name,
            "direction": "outbound",
            "provider": os.environ.get("AUDIOAGENT_TRUNK_PROVIDER", "external-sip"),
            "livekit_trunk_id": required("LIVEKIT_SIP_TRUNK_ID"),
            "secret_name": "external-compose-config",
            "status": "active",
            "numbers": [source_number] if source_number else [],
            "max_concurrent_calls": max_concurrent,
            "max_calls_per_second": max_cps,
        },
    )


def dotenv_value(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def update_dotenv(path: Path, values: dict[str, Any]) -> None:
    existing: dict[str, str] = {}
    order: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                continue
            key, old_value = line.split("=", 1)
            key = key.strip()
            if key:
                existing[key] = old_value
                order.append(key)
    for key, value in values.items():
        if key not in existing:
            order.append(key)
        existing[key] = dotenv_value(value)
    unique_order = list(dict.fromkeys(order))
    content = "\n".join(f"{key}={existing[key]}" for key in unique_order) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def seed_hermes_config(path: Path) -> None:
    if path.exists():
        return
    model = required("HERMES_MODEL")
    base_url = required("HERMES_MODEL_BASE_URL")
    content = f"""model:
  default: {dotenv_value(model)}
  provider: deepseek
  base_url: {dotenv_value(base_url)}
session_reset:
  mode: none
group_sessions_per_user: true
platform_toolsets:
  weixin:
    - audioagent
    - skills
    - no_mcp
plugins:
  enabled:
    - audioagent
  disabled: []
  entries:
    audioagent:
      allow_tool_override: false
weixin:
  gateway_restart_notification: false
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)


def main() -> None:
    required("DEEPSEEK_API_KEY")
    wait_for_control_plane()
    project = ensure_project()
    project_id = str(project["id"])
    trunk = configure_project(project_id)
    trunk_id = str(trunk["id"])

    update_dotenv(
        Path("/runtime/audioagent.env"),
        {"CLOUD_PARITY_TELEPHONY_PROJECT_IDS": project_id},
    )
    update_dotenv(
        Path("/hermes/.env"),
        {
            "DEEPSEEK_API_KEY": required("DEEPSEEK_API_KEY"),
            "AUDIOAGENT_BASE_URL": CONTROL_URL,
            "AUDIOAGENT_PROJECT_ID": project_id,
            "AUDIOAGENT_AGENT_NAME": os.environ.get(
                "AUDIOAGENT_AGENT_NAME", "commercial-agent"
            ),
            "AUDIOAGENT_TRUNK_ID": trunk_id,
            "AUDIOAGENT_SOURCE_NUMBER": os.environ.get("AUDIOAGENT_SOURCE_NUMBER", ""),
            "AUDIOAGENT_USER_ID": ADMIN_USER,
            "AUDIOAGENT_RESULT_FORWARDING": "true",
            "AUDIOAGENT_RESULT_TARGET": "weixin",
            "WEIXIN_DM_POLICY": os.environ.get("WEIXIN_DM_POLICY", "open"),
            "WEIXIN_ALLOWED_USERS": os.environ.get("WEIXIN_ALLOWED_USERS", ""),
            "WEIXIN_GROUP_POLICY": "disabled",
        },
    )
    seed_hermes_config(Path("/hermes/config.yaml"))
    print(
        json.dumps(
            {"status": "ready", "project_id": project_id, "trunk_id": trunk_id},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
