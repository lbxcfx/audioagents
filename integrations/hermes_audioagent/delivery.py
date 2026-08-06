"""Background delivery of terminal AudioAgent results to a Hermes target."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any

from .client import AudioAgentClient
from .result_card import render_result_card
from .tools import _task_status


logger = logging.getLogger("hermes.plugins.audioagent.delivery")
_start_lock = threading.Lock()
_started = False
_last_delivery_attempt: dict[str, float] = {}


def _enabled() -> bool:
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
    values = payload.get("delivered_campaign_ids") if isinstance(payload, dict) else []
    return {str(item) for item in values or [] if str(item).strip()}


def _save_delivered(values: set[str]) -> None:
    target = _state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {"delivered_campaign_ids": sorted(values)[-1000:]},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(target)


def _duration_seconds(item: dict[str, Any]) -> int | None:
    try:
        answered = datetime.fromisoformat(
            str(item.get("answered_at") or "").replace("Z", "+00:00")
        )
        ended = datetime.fromisoformat(
            str(item.get("ended_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return None
    return max(0, int((ended - answered).total_seconds()))


def format_result_message(status: dict[str, Any]) -> str:
    lines = [
        "☎️ 外呼任务结果",
        f"任务：{status.get('campaign_name') or status.get('task_id') or '未命名任务'}",
        f"状态：{status.get('status') or '未知'}",
    ]
    for index, item in enumerate(status.get("results") or [], start=1):
        if not isinstance(item, dict):
            continue
        customer = item.get("customer") if isinstance(item.get("customer"), dict) else {}
        name = str(customer.get("name") or f"客户{index}")
        phone = str(item.get("phone") or "")
        masked_phone = ("*" * max(0, len(phone) - 4) + phone[-4:]) if phone else ""
        lines.append(f"{name}（{masked_phone}）：{item.get('status') or '未知'}")
        duration = _duration_seconds(item)
        if duration is not None:
            lines.append(f"通话时长：{duration}秒")
        summary = str(item.get("summary") or "").strip()
        if summary:
            lines.append(f"摘要：{summary}")
        else:
            detail = str(item.get("failure_detail") or "")
            if "room disconnected" in detail.lower():
                lines.append("摘要：客户主动挂断，未形成完整业务摘要。")
            else:
                lines.append("摘要：本次通话未形成业务摘要。")
        recent = [str(value).strip() for value in item.get("last_user_messages") or []]
        if recent:
            lines.append("客户最后回应：" + "；".join(recent[-3:]))
    return "\n".join(lines)[:3500]


def format_card_caption(status: dict[str, Any]) -> str:
    task = str(status.get("campaign_name") or status.get("task_id") or "未命名任务")
    state = str(status.get("status") or "未知")
    return f"外呼任务「{task}」已结束（{state}），详情见结果卡片。"[:500]


def _send_message(message: str, *, card_path: Path | None = None) -> bool:
    hermes = shutil.which("hermes") or "/usr/local/bin/hermes"
    target = os.getenv("AUDIOAGENT_RESULT_TARGET", "weixin").strip() or "weixin"
    payload = f"MEDIA:{card_path}\n{message}" if card_path else message
    try:
        completed = subprocess.run(
            [hermes, "send", "--quiet", "--to", target, payload],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
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
            include_transcript=False,
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
        card_path: Path | None = None
        message = format_result_message(status)
        try:
            card_path = render_result_card(status, campaign_id=campaign_id)
            message = format_card_caption(status)
        except Exception as exc:
            logger.warning(
                "AudioAgent result card rendering failed; using text fallback: %s",
                type(exc).__name__,
            )
        if _send_message(message, card_path=card_path):
            delivered.add(campaign_id)
            changed = True
            logger.info("Delivered AudioAgent result: campaign_id=%s", campaign_id)
    return changed


def _forwarder_loop() -> None:
    interval = min(
        60.0,
        max(2.0, float(os.getenv("AUDIOAGENT_RESULT_POLL_SECONDS", "3"))),
    )
    delivered = _load_delivered()
    while True:
        try:
            if _scan_once(delivered):
                _save_delivered(delivered)
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
