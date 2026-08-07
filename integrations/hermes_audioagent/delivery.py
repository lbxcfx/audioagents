"""Background delivery of terminal AudioAgent results to a Hermes target."""

from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from .client import AudioAgentClient
from .tools import _task_status


logger = logging.getLogger("hermes.plugins.audioagent.delivery")
_start_lock = threading.Lock()
_started = False
_last_delivery_attempt: dict[str, float] = {}


def _enabled() -> bool:
    if os.getenv("AUDIOAGENT_RESULT_FORWARDER_CHILD", "").strip() == "1":
        return False
    return os.getenv("AUDIOAGENT_RESULT_FORWARDING", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _state_path() -> Path:
    configured = os.getenv("AUDIOAGENT_DELIVERY_STATE_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))
    return hermes_home / "audioagent-deliveries.json"


def _load_delivered() -> set[str]:
    try:
        payload = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    values = list(payload.get("delivered_campaign_ids") or [])
    values.extend(payload.get("attempted_campaign_ids") or [])
    return {str(item) for item in values if str(item).strip()}


def _state_lock_path() -> Path:
    target = _state_path()
    return target.with_suffix(target.suffix + ".lock")


def _read_delivery_state() -> tuple[set[str], set[str]]:
    try:
        payload = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    delivered = {
        str(item)
        for item in payload.get("delivered_campaign_ids") or []
        if str(item).strip()
    }
    attempted = {
        str(item)
        for item in payload.get("attempted_campaign_ids") or []
        if str(item).strip()
    }
    return delivered, attempted


def _write_delivery_state(delivered: set[str], attempted: set[str]) -> None:
    target = _state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "delivered_campaign_ids": sorted(delivered)[-1000:],
                "attempted_campaign_ids": sorted(attempted)[-1000:],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(target)


def _claim_delivery(campaign_id: str) -> bool:
    """Persist an at-most-once claim before the external Weixin side effect."""

    lock_path = _state_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        lock_path.chmod(0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        delivered, attempted = _read_delivery_state()
        if campaign_id in delivered or campaign_id in attempted:
            return False
        attempted.add(campaign_id)
        _write_delivery_state(delivered, attempted)
        return True


def _mark_delivered(campaign_id: str) -> None:
    lock_path = _state_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        lock_path.chmod(0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        delivered, attempted = _read_delivery_state()
        attempted.add(campaign_id)
        delivered.add(campaign_id)
        _write_delivery_state(delivered, attempted)


def _single_line(value: Any, *, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _display_phone(value: Any) -> str:
    phone = re.sub(r"[^0-9+]", "", str(value or ""))
    if phone.startswith("+86") and len(phone) == 14:
        return phone[3:]
    return phone or "未提供"


def _display_call_status(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "").strip().lower()
    failure_code = str(item.get("failure_code") or "").strip().lower()
    disposition = str(item.get("disposition") or "").strip().lower()
    if failure_code in {"sip_600", "sip_603"} or disposition in {
        "declined",
        "rejected",
    }:
        return "拒接"
    if status == "completed" or item.get("answered_at"):
        return "已接通"
    return "未接通"


def _transcript_lines(item: dict[str, Any]) -> list[str]:
    lines = ["**通话记录：**"]
    for turn in item.get("transcript") or []:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        if role not in {"assistant", "user"}:
            continue
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        label = "AI" if role == "assistant" else "客户"
        lines.append(f"{label}：{text}")
    if len(lines) == 1:
        lines.append("无（数据库中没有 AI 或客户的文字通话记录）。")
    return lines


def format_result_message(status: dict[str, Any]) -> str:
    invitation_content = _single_line(
        status.get("invitation_content")
        or status.get("campaign_name")
        or status.get("task_id")
        or "未提供"
    )
    blocks: list[str] = []
    for index, item in enumerate(status.get("results") or [], start=1):
        if not isinstance(item, dict):
            continue
        customer = item.get("customer") if isinstance(item.get("customer"), dict) else {}
        name = _single_line(customer.get("name") or f"客户{index}", limit=100)
        call_status = _display_call_status(item)
        blocks.append(
            "\n".join(
                [
                    f"**收信人：** {name}",
                    f"**电话：** {_display_phone(item.get('phone'))}",
                    f"**邀请内容：** {invitation_content}",
                    "**发起人：** 李宝祥（智能助理代拨）",
                    f"**通话状态：** {call_status}",
                    *_transcript_lines(item),
                ]
            )
        )
    if not blocks:
        blocks.append(
            "\n".join(
                [
                    "**收信人：** 未提供",
                    "**电话：** 未提供",
                    f"**邀请内容：** {invitation_content}",
                    "**发起人：** 李宝祥（智能助理代拨）",
                    "**通话状态：** 未接通",
                    "**通话记录：**",
                    "无（数据库中没有 AI 或客户的文字通话记录）。",
                ]
            )
        )
    return "\n\n---\n\n".join(blocks)


def _send_message(message: str) -> bool:
    hermes = shutil.which("hermes") or "/usr/local/bin/hermes"
    target = os.getenv("AUDIOAGENT_RESULT_TARGET", "weixin").strip() or "weixin"
    child_environment = os.environ.copy()
    # `hermes send` loads enabled plugins. Without this override its child
    # process starts another result forwarder and recursively sends the same
    # campaign before the parent can persist success.
    child_environment["AUDIOAGENT_RESULT_FORWARDING"] = "false"
    child_environment["AUDIOAGENT_RESULT_FORWARDER_CHILD"] = "1"
    try:
        completed = subprocess.run(
            [hermes, "send", "--quiet", "--to", target, message],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=child_environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("AudioAgent result delivery failed: %s", type(exc).__name__)
        return False
    if completed.returncode != 0:
        logger.warning("AudioAgent result delivery returned exit code %s", completed.returncode)
        return False
    return True


def _scan_once(delivered: set[str]) -> bool:
    client = AudioAgentClient()
    campaigns = client.request(
        "GET", client.project_path("/telephony/campaigns?limit=500")
    ).get("items") or []
    changed = False
    for campaign in campaigns:
        if not isinstance(campaign, dict):
            continue
        campaign_id = str(campaign.get("id") or "")
        metadata = campaign.get("metadata") if isinstance(campaign.get("metadata"), dict) else {}
        if (
            not campaign_id
            or campaign_id in delivered
            or metadata.get("integration") != "hermes"
        ):
            continue
        status = _task_status(
            client,
            campaign_id,
            include_results=True,
            include_transcript=True,
        )
        if not status.get("finished"):
            continue
        retry_seconds = min(
            300.0,
            max(10.0, float(os.getenv("AUDIOAGENT_RESULT_RETRY_SECONDS", "30"))),
        )
        now = time.monotonic()
        if now - _last_delivery_attempt.get(campaign_id, 0.0) < retry_seconds:
            continue
        _last_delivery_attempt[campaign_id] = now
        if not _claim_delivery(campaign_id):
            delivered.add(campaign_id)
            continue
        message = format_result_message(status)
        if _send_message(message):
            _mark_delivered(campaign_id)
            delivered.add(campaign_id)
            changed = True
            logger.info("Delivered AudioAgent result: campaign_id=%s", campaign_id)
        else:
            # The claim intentionally remains durable. We prefer a missing
            # notification over duplicate customer-facing cards.
            delivered.add(campaign_id)
            logger.warning(
                "AudioAgent result delivery will not retry automatically: campaign_id=%s",
                campaign_id,
            )
    return changed


def _forwarder_loop() -> None:
    interval = min(
        60.0,
        max(2.0, float(os.getenv("AUDIOAGENT_RESULT_POLL_SECONDS", "3"))),
    )
    delivered = _load_delivered()
    while True:
        try:
            _scan_once(delivered)
        except Exception as exc:
            logger.warning("AudioAgent result scan failed: %s", type(exc).__name__)
        time.sleep(interval)


def start_result_forwarder() -> None:
    global _started
    if not _enabled():
        return
    with _start_lock:
        if _started:
            return
        _started = True
        threading.Thread(
            target=_forwarder_loop,
            name="audioagent-result-forwarder",
            daemon=True,
        ).start()
        logger.info("AudioAgent result forwarder started")
