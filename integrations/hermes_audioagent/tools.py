"""Hermes tool handlers for AudioAgent."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any
from urllib.parse import quote, urlsplit
import uuid

from .client import AudioAgentClient, AudioAgentError
from . import address_book


TERMINAL_CALL_STATUSES = {
    "completed",
    "failed",
    "busy",
    "no_answer",
    "canceled",
    "blocked",
}
TERMINAL_CAMPAIGN_STATUSES = {"completed", "canceled"}

_BLOCKED_REASON_SUMMARIES = {
    "consent_missing_or_inactive": "目标号码缺少有效或未过期的外呼授权，电话未拨出。",
    "do_not_call": "目标号码在禁止呼叫名单中，电话未拨出。",
    "daily_number_attempt_limit": "目标号码已达到当日呼叫次数上限，电话未拨出。",
    "outbound_paused": "外呼功能当前已暂停，电话未拨出。",
}
logger = logging.getLogger("hermes.plugins.audioagent.tools")


def is_configured() -> bool:
    return bool(os.getenv("AUDIOAGENT_PROJECT_ID", "").strip())


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _handler(function):
    @wraps(function)
    def wrapped(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            payload = function(args or {}, **kwargs)
        except (AudioAgentError, ValueError) as exc:
            payload = {"ok": False, "error": str(exc)}
        except Exception as exc:  # Hermes handlers must never raise.
            payload = {
                "ok": False,
                "error": f"unexpected {type(exc).__name__}: {exc}",
            }
        if function.__name__ == "submit_outbound_task":
            # The final Weixin hook consumes this exact tool-derived fact, so
            # an LLM cannot paraphrase or embellish submission state.
            from .response_policy import remember_submission_response

            remember_submission_response(
                kwargs.get("session_id") or kwargs.get("task_id"), payload
            )
        elif function.__name__ in {
            "resolve_outbound_contact",
            "confirm_address_book_contact",
        }:
            response = str(payload.get("user_response") or "").strip()
            if response:
                from .response_policy import remember_user_response

                remember_user_response(
                    kwargs.get("session_id") or kwargs.get("task_id"), response
                )
        return _json_result(payload)

    return wrapped


def _required_text(args: dict[str, Any], key: str, limit: int) -> str:
    value = str(args.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    if len(value) > limit:
        raise ValueError(f"{key} exceeds {limit} characters")
    return value


_FIXED_CALLER_POLICY = """# 固定身份与信息规则（最高优先级）
- 你的身份固定为李宝祥的智能助理。
- 对客自我介绍时统一使用“我是李宝祥的智能助理”，不得使用其他身份。
- 第一句开场必须说“您好，我是李宝祥的智能助理，请问您是{{customer_name}}吗？”。
- {{customer_name}} 运行时必须使用当前 customers[].name 动态替换；不得固定为某位客户，也不得朗读花括号、星号或占位符。
- 客户确认身份后再说明事情；语气要热情、自然、口语化，像真人助理沟通。
- 客户明确答复后立即保存业务结论并结束；摘要写清同意、不同意或待确认的事项和必要提示，不写沉默计时或系统挂机原因。
- 仅使用微信任务已经提供的信息；缺失信息直接省略，不询问任务发起人。
- 不朗读 XXX、方括号等未填写占位符，也不得编造缺失信息。

# 微信任务
"""


def _prepare_prompt_snapshot(prompt: str) -> str:
    """Apply invariant caller policy without blocking direct WeChat execution."""
    normalized = re.sub(
        r"我是\s*(?<![A-Za-z])X{2,}(?![A-Za-z])\s*的(?:智能)?助理",
        "我是李宝祥的智能助理",
        prompt,
        flags=re.I,
    )
    normalized = re.sub(
        r"(?<![A-Za-z])X{2,}(?![A-Za-z])", "李宝祥", normalized, flags=re.I
    )
    return _FIXED_CALLER_POLICY + normalized.strip()


def _normalize_phone(value: Any) -> str:
    raw = str(value or "").strip().replace(" ", "").replace("-", "")
    if re.fullmatch(r"1\d{10}", raw):
        raw = "+86" + raw
    elif raw.startswith("00"):
        raw = "+" + raw[2:]
    if not re.fullmatch(r"\+[1-9]\d{7,14}", raw):
        raise ValueError(f"invalid E.164 phone number: {value}")
    return raw


def _task_id(value: Any) -> str:
    candidate = str(value or "").strip() or f"hermes-{uuid.uuid4().hex}"
    candidate = re.sub(r"[^A-Za-z0-9_.:-]+", "-", candidate).strip("-.")
    if not candidate:
        raise ValueError("task_id is invalid")
    return candidate[:120]


def _campaign_resource_name(task_name: str, identifier: str) -> str:
    """Keep the database key unique while preserving a human display name."""
    suffix = f" [{identifier[-12:]}]"
    return task_name[: 200 - len(suffix)].rstrip() + suffix


def _setting(args: dict[str, Any], key: str, environment: str) -> str:
    return str(args.get(key) or os.getenv(environment, "")).strip()


def _campaign(client: AudioAgentClient, campaign_id: str) -> dict[str, Any]:
    result = client.request(
        "GET", client.project_path("/telephony/campaigns?limit=500")
    )
    for item in result.get("items") or []:
        if isinstance(item, dict) and str(item.get("id")) == campaign_id:
            return item
    raise AudioAgentError("AudioAgent campaign not found")


def _campaign_calls(
    client: AudioAgentClient, campaign_id: str
) -> list[dict[str, Any]]:
    result = client.request(
        "GET", client.project_path("/telephony/calls?direction=outbound&limit=500")
    )
    return [
        item
        for item in result.get("items") or []
        if isinstance(item, dict) and str(item.get("campaign_id") or "") == campaign_id
    ]


def _call_query_path(direction: str, *, limit: int = 100) -> str:
    normalized = str(direction or "outbound").strip().lower()
    if normalized not in {"outbound", "inbound", "any"}:
        raise ValueError("direction must be outbound, inbound, or any")
    suffix = "" if normalized == "any" else f"&direction={normalized}"
    return f"/telephony/calls?limit={limit}{suffix}"


def _latest_answered_call(
    client: AudioAgentClient,
    *,
    direction: str,
    require_recording: bool = False,
) -> dict[str, Any]:
    response = client.request("GET", client.project_path(_call_query_path(direction)))
    calls = [item for item in response.get("items") or [] if isinstance(item, dict)]
    for call in calls:
        if not str(call.get("answered_at") or "").strip():
            continue
        if require_recording and (
            str(call.get("recording_status") or "").strip().lower() != "completed"
            or not str(call.get("recording_storage_uri") or "").strip()
        ):
            continue
        return call
    detail = " with a completed recording" if require_recording else ""
    raise AudioAgentError(f"No recent answered call{detail} was found")


def _call_customer(call: dict[str, Any]) -> dict[str, Any]:
    metadata = call.get("metadata") if isinstance(call.get("metadata"), dict) else {}
    customer = metadata.get("customer") if isinstance(metadata.get("customer"), dict) else {}
    return {
        "name": str(customer.get("name") or "").strip(),
        "phone": str(call.get("destination_number") or "").strip(),
    }


def _format_latest_transcript(
    call: dict[str, Any], transcript: list[dict[str, str]]
) -> str:
    customer = _call_customer(call)
    lines = ["最近一通已接通电话的聊天记录"]
    if customer["name"]:
        lines.append(f"联系人：{customer['name']}")
    if customer["phone"]:
        lines.append(f"电话：{customer['phone']}")
    if call.get("answered_at"):
        lines.append(f"接通时间：{call['answered_at']}")
    lines.append("")
    if transcript:
        for item in transcript:
            label = "客户" if item["role"] == "user" else "AI"
            lines.append(f"{label}：{item['text']}")
    else:
        lines.append("本通电话没有可用的客户或助理文字记录。")
    return "\n".join(lines)


def _recording_cache_path(call_id: str, recording_url: str) -> Path:
    hermes_home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    cache_dir = hermes_home / "cache" / "documents"
    safe_call_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", call_id).strip("-.")
    if not safe_call_id:
        raise ValueError("call ID is invalid")
    suffix = Path(urlsplit(recording_url).path).suffix.lower()
    if suffix not in {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac"}:
        suffix = ".mp3"
    return cache_dir / f"audioagent-call-{safe_call_id}{suffix}"


def _normalize_mp3_for_delivery(source: Path, target: Path) -> int:
    """Write a seekable CBR MP3 whose duration is reliable in chat clients."""

    ffmpeg_name = os.getenv("AUDIOAGENT_FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg"
    ffmpeg = shutil.which(ffmpeg_name)
    if not ffmpeg:
        raise AudioAgentError(
            f"recording delivery requires ffmpeg ({ffmpeg_name!r} was not found)"
        )
    timeout_seconds = min(
        300,
        max(
            10,
            int(
                os.getenv(
                    "AUDIOAGENT_RECORDING_NORMALIZE_TIMEOUT_SECONDS",
                    "120",
                )
            ),
        ),
    )
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        "-ar",
        "16000",
        "-ac",
        "2",
        "-write_xing",
        "1",
        "-id3v2_version",
        "3",
        "-threads",
        "1",
        str(target),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioAgentError("recording MP3 normalization timed out") from exc
    except OSError as exc:
        raise AudioAgentError(
            f"recording MP3 normalization failed to start: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "unknown ffmpeg error").strip()[-1000:]
        raise AudioAgentError(f"recording MP3 normalization failed: {detail}")
    try:
        size_bytes = target.stat().st_size
    except OSError as exc:
        raise AudioAgentError("recording MP3 normalization produced no file") from exc
    if size_bytes <= 0:
        raise AudioAgentError("recording MP3 normalization produced an empty file")
    target.chmod(0o600)
    return size_bytes


def _campaign_contacts(
    client: AudioAgentClient, campaign_id: str
) -> list[dict[str, Any]]:
    result = client.request(
        "GET",
        client.project_path(
            f"/telephony/campaigns/{quote(campaign_id, safe='')}/contacts?limit=5000"
        ),
    )
    return [item for item in result.get("items") or [] if isinstance(item, dict)]


def _campaign_contact_result(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    customer: dict[str, Any] = {}
    for field in ("name", "company", "profile"):
        value = item.get(field) if field == "name" else metadata.get(field)
        if value is not None and value != "":
            customer[field] = value
    status = str(item.get("status") or "blocked")
    reason = str(item.get("failure_reason") or "").strip()
    summary = _BLOCKED_REASON_SUMMARIES.get(
        reason,
        f"电话未拨出：{reason}。" if reason else "电话未进入拨号流程。",
    )
    return {
        "call_id": item.get("call_id") or "",
        "status": status,
        "phone": item.get("phone_number") or "",
        "customer": customer,
        "failure_code": reason,
        "failure_detail": reason,
        "summary": summary,
        "summary_missing": False,
        "result_pending": False,
    }


def _timeline_result(
    client: AudioAgentClient,
    call: dict[str, Any],
    *,
    include_transcript: bool,
) -> dict[str, Any]:
    metadata = call.get("metadata") if isinstance(call.get("metadata"), dict) else {}
    customer = (
        metadata.get("customer") if isinstance(metadata.get("customer"), dict) else {}
    )
    result: dict[str, Any] = {
        "call_id": call.get("id"),
        "status": call.get("status"),
        "phone": call.get("destination_number"),
        "customer": customer,
        "answering_machine_category": call.get("answering_machine_category") or "",
        "disposition": call.get("disposition") or "",
        "failure_code": call.get("failure_code") or "",
        "failure_detail": call.get("failure_detail") or "",
        "started_at": call.get("started_at"),
        "answered_at": call.get("answered_at"),
        "ended_at": call.get("ended_at"),
        "recording_status": call.get("recording_status") or "",
    }
    try:
        timeline = client.request(
            "GET",
            client.project_path(
                f"/sessions/{quote(str(call.get('id') or ''), safe='')}"
            ),
        )
    except AudioAgentError:
        result["result_pending"] = True
        return result

    transcript: list[dict[str, str]] = []
    user_messages: list[str] = []
    for event in timeline.get("events") or []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "call.result":
            result["summary"] = str(payload.get("summary") or "")
            result["intent_label"] = str(payload.get("intent_label") or "")
        elif event_type == "call.intent_label":
            result["intent_label"] = str(payload.get("label") or "")
            result["intent_evidence"] = str(payload.get("evidence") or "")
        elif event_type in {"user.transcript", "agent.response"}:
            text = str(payload.get("text") or "").strip()
            if event_type == "user.transcript" and text:
                user_messages.append(text)
            if include_transcript and text:
                transcript.append(
                    {
                        "role": "user" if event_type == "user.transcript" else "assistant",
                        "text": text,
                    }
                )
    if include_transcript:
        result["transcript"] = transcript
    if not result.get("summary"):
        result["summary_missing"] = True
        if user_messages:
            result["last_user_messages"] = user_messages[-5:]
    else:
        result["summary_missing"] = False
    result["result_pending"] = False
    return result


def _task_status(
    client: AudioAgentClient,
    campaign_id: str,
    *,
    include_results: bool,
    include_transcript: bool,
) -> dict[str, Any]:
    campaign = _campaign(client, campaign_id)
    calls = _campaign_calls(client, campaign_id)
    campaign_contacts = _campaign_contacts(client, campaign_id)
    counts: dict[str, int] = {}
    for call in calls:
        status = str(call.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    contact_counts: dict[str, int] = {}
    for contact in campaign_contacts:
        status = str(contact.get("status") or "unknown")
        contact_counts[status] = contact_counts.get(status, 0) + 1
    total = int(campaign.get("contact_count") or 0)
    terminal = sum(
        count for status, count in counts.items() if status in TERMINAL_CALL_STATUSES
    ) + int(campaign.get("blocked_count") or 0)
    finished = str(campaign.get("status") or "") in TERMINAL_CAMPAIGN_STATUSES
    if total > 0 and terminal >= total:
        finished = True
    task_metadata = (
        (campaign.get("metadata") or {}).get("task", {})
        if isinstance(campaign.get("metadata"), dict)
        and isinstance((campaign.get("metadata") or {}).get("task"), dict)
        else {}
    )
    response: dict[str, Any] = {
        "ok": True,
        "task_id": task_metadata.get("id") or "",
        "campaign_id": campaign_id,
        "campaign_name": task_metadata.get("display_name") or campaign.get("name"),
        "invitation_content": (
            task_metadata.get("invitation_content")
            or task_metadata.get("display_name")
            or campaign.get("name")
        ),
        "status": campaign.get("status"),
        "finished": finished,
        "contact_count": total,
        "blocked_count": int(campaign.get("blocked_count") or 0),
        "call_status_counts": counts,
        "contact_status_counts": contact_counts,
    }
    if include_results:
        results = [
            _timeline_result(client, call, include_transcript=include_transcript)
            for call in sorted(calls, key=lambda item: str(item.get("created_at") or ""))
        ]
        known_call_ids = {str(call.get("id") or "") for call in calls}
        results.extend(
            _campaign_contact_result(contact)
            for contact in campaign_contacts
            if str(contact.get("status") or "") in TERMINAL_CALL_STATUSES
            and str(contact.get("call_id") or "") not in known_call_ids
        )
        response["results"] = results
    return response


@_handler
def submit_outbound_task(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    task_name = _required_text(args, "task_name", 200)
    invitation_content = str(args.get("invitation_content") or task_name).strip()
    invitation_content = re.sub(r"\s+", " ", invitation_content)[:300]
    prompt = _prepare_prompt_snapshot(_required_text(args, "prompt", 24_000))
    if len(prompt.encode("utf-8")) > 24_000:
        raise ValueError("prompt exceeds 24000 UTF-8 bytes")
    client = AudioAgentClient()
    customers = args.get("customers")
    if not isinstance(customers, list) or not 1 <= len(customers) <= 100:
        raise ValueError("customers must contain between 1 and 100 entries")
    identifier = _task_id(args.get("task_id"))
    agent_name = _setting(args, "agent_name", "AUDIOAGENT_AGENT_NAME")
    trunk_id = _setting(args, "trunk_id", "AUDIOAGENT_TRUNK_ID")
    source_number = _setting(args, "source_number", "AUDIOAGENT_SOURCE_NUMBER")
    if not agent_name:
        raise ValueError("agent_name or AUDIOAGENT_AGENT_NAME is required")
    if not trunk_id:
        raise ValueError("trunk_id or AUDIOAGENT_TRUNK_ID is required")
    if source_number:
        source_number = _normalize_phone(source_number)
    max_concurrency = int(args.get("max_concurrency") or 1)
    max_attempts = int(args.get("max_attempts") or 2)
    if not 1 <= max_concurrency <= 100:
        raise ValueError("max_concurrency must be between 1 and 100")
    if not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be between 1 and 10")

    contact_payloads = []
    for index, item in enumerate(customers, start=1):
        if not isinstance(item, dict):
            raise ValueError("each customer must be an object")
        phone = _normalize_phone(item.get("phone"))
        external_id = str(item.get("external_id") or f"{identifier}-{index}").strip()
        external_id = re.sub(r"[^A-Za-z0-9_.:-]+", "-", external_id)[:200]
        profile = item.get("profile", "")
        profile_json = json.dumps(
            profile, ensure_ascii=False, separators=(",", ":")
        )
        if len(profile_json.encode("utf-8")) > 4_000:
            raise ValueError(f"customer {index} profile exceeds 4000 UTF-8 bytes")
        contact_payloads.append(
            {
                "external_id": external_id,
                "phone_number": phone,
                "name": str(item.get("name") or "")[:200],
                "status": "active",
                "metadata": {
                    "company": str(item.get("company") or "")[:200],
                    "profile": profile,
                },
            }
        )

    task_snapshot: dict[str, Any] = {
        "id": identifier,
        "display_name": task_name,
        "invitation_content": invitation_content,
        "prompt_snapshot": prompt,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    }
    if args.get("scene_id") not in {None, ""}:
        task_snapshot["scene_id"] = int(args["scene_id"])
    hermes_session_id = str(
        kwargs.get("session_id") or kwargs.get("task_id") or ""
    )[:200]
    campaign_payload: dict[str, Any] = {
        "name": _campaign_resource_name(task_name, identifier),
        "agent_name": agent_name,
        "trunk_id": trunk_id,
        "source_number": source_number,
        "priority": 100,
        "max_attempts": max_attempts,
        "max_concurrent_calls": max_concurrency,
        "metadata": {
            "integration": "hermes",
            "task": task_snapshot,
            "delivery": {"hermes_session_id": hermes_session_id},
        },
    }
    if args.get("scheduled_at"):
        campaign_payload["scheduled_at"] = str(args["scheduled_at"])
    campaign = client.request(
        "POST", client.project_path("/telephony/campaigns"), campaign_payload
    )
    campaign_id = str(campaign.get("id") or "")
    if not campaign_id:
        raise AudioAgentError("AudioAgent did not return a campaign ID")
    try:
        imported = client.request(
            "POST",
            client.project_path("/telephony/contacts/import"),
            {"contacts": contact_payloads},
        )
        contacts = imported.get("items") or []
        if len(contacts) != len(contact_payloads):
            raise AudioAgentError("AudioAgent did not import every task contact")
        client.request(
            "POST",
            client.project_path(
                f"/telephony/campaigns/{quote(campaign_id, safe='')}/contacts"
            ),
            {"contact_ids": [str(item["id"]) for item in contacts]},
        )
        running = client.request(
            "PUT",
            client.project_path(
                f"/telephony/campaigns/{quote(campaign_id, safe='')}/status"
            ),
            {"status": "running"},
        )
    except Exception:
        try:
            client.request(
                "PUT",
                client.project_path(
                    f"/telephony/campaigns/{quote(campaign_id, safe='')}/status"
                ),
                {"status": "canceled"},
            )
        except Exception:
            pass
        raise

    # Persist only valid real names. The server deterministically rejects
    # honorifics such as “李总”, so address-book ingestion can never block a
    # successfully submitted call.
    for contact in contact_payloads:
        try:
            client.request(
                "POST",
                client.project_path("/telephony/address-book"),
                {
                    "full_name": contact["name"],
                    "phone_number": contact["phone_number"],
                    "source": "hermes_weixin",
                },
            )
        except Exception as exc:
            logger.warning("address-book auto-ingest failed: %s", exc)
    enqueue_result = (
        running.get("enqueue_result")
        if isinstance(running.get("enqueue_result"), dict)
        else {}
    )
    queued_count = int(enqueue_result.get("queued") or 0)
    blocked_count = int(enqueue_result.get("blocked") or 0)
    blocked_reasons = dict(enqueue_result.get("blocked_reasons") or {})
    all_blocked = blocked_count > 0 and queued_count == 0
    response: dict[str, Any] = {
        "ok": not all_blocked,
        "task_id": identifier,
        "campaign_id": campaign_id,
        "campaign_name": task_name,
        "invitation_content": invitation_content,
        "status": running.get("status"),
        "customer_count": len(contact_payloads),
        "queued_count": queued_count,
        "blocked_count": blocked_count,
        "max_concurrency": max_concurrency,
        "message": (
            "outbound task was not dialed"
            if all_blocked
            else "outbound task accepted"
        ),
    }
    if blocked_reasons:
        response["blocked_reasons"] = blocked_reasons
    if all_blocked:
        primary_reason = next(iter(blocked_reasons), "")
        response["error_code"] = primary_reason or "all_contacts_blocked"
        response["error"] = _BLOCKED_REASON_SUMMARIES.get(
            primary_reason,
            "所有客户均在拨号前被策略拦截，电话未拨出。",
        )
    elif blocked_count:
        response["partial"] = True
    return response


def _hermes_session(args: dict[str, Any], kwargs: dict[str, Any]) -> str:
    candidates = (
        kwargs.get("session_id"),
        kwargs.get("task_id"),
        args.get("hermes_session_id"),
        args.get("task_id"),
    )
    for candidate in candidates:
        identifier = str(candidate or "").strip()[:200]
        if identifier:
            return identifier
    return ""


def _submission_args(args: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "task_name": _required_text(args, "task_name", 200),
        "prompt": _required_text(args, "prompt", 24_000),
    }
    for key in (
        "invitation_content",
        "task_id",
        "scene_id",
        "max_concurrency",
        "max_attempts",
        "scheduled_at",
        "agent_name",
        "trunk_id",
        "source_number",
    ):
        if args.get(key) not in {None, ""}:
            result[key] = args[key]
    return result


def _display_phone(value: Any) -> str:
    phone = str(value or "").strip()
    return phone[3:] if phone.startswith("+86") and len(phone) == 14 else phone


def _confirmation_text(
    candidates: list[dict[str, Any]], *, input_mode: str
) -> str:
    if input_mode == "voice":
        heading = "语音识别结果可能有误，请确认联系人："
    else:
        heading = "通讯录中找到相似联系人，请确认："
    lines = [heading]
    for index, candidate in enumerate(candidates, start=1):
        lines.append(
            f"{index}. {candidate.get('full_name') or '未知姓名'} "
            f"{_display_phone(candidate.get('phone_number'))}"
        )
    lines.append("请回复“确认1”后拨号；不正确请提供姓名和电话号码。")
    return "\n".join(lines)


def _submit_address_book_candidate(
    submission: dict[str, Any],
    candidate: dict[str, Any],
    *,
    session_id: str,
) -> dict[str, Any]:
    phone = str(candidate.get("phone_number") or "").strip()
    name = str(candidate.get("full_name") or "").strip()
    if not phone or not name:
        raise AudioAgentError("通讯录候选缺少姓名或电话号码，电话未拨出")
    payload = {
        **submission,
        "customers": [{"name": name, "phone": phone}],
    }
    result = submit_outbound_task.__wrapped__(payload, task_id=session_id)
    from .response_policy import remember_submission_response

    remember_submission_response(session_id, result)
    return result


@_handler
def resolve_outbound_contact(
    args: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    """Resolve a no-phone Hermes request and dial only deterministic exact text matches."""

    session_id = _hermes_session(args, kwargs)
    context = address_book.resolution_context(session_id) or {}
    query = str(context.get("query") or args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    input_mode = str(context.get("input_mode") or args.get("input_mode") or "text")
    input_mode = "voice" if input_mode == "voice" else "text"
    submission = _submission_args(args)
    client = AudioAgentClient()
    try:
        client.request("POST", client.project_path("/telephony/address-book/sync"), {})
    except Exception as exc:
        logger.warning("address-book history sync failed: %s", exc)
    lookup = client.request(
        "GET",
        client.project_path(
            "/telephony/address-book/lookup"
            f"?query={quote(query, safe='')}&limit=3"
        ),
    )
    candidates = [
        item for item in lookup.get("candidates") or [] if isinstance(item, dict)
    ][:3]
    match_type = str(lookup.get("match_type") or "none")

    if match_type == "exact" and len(candidates) == 1 and input_mode == "text":
        return _submit_address_book_candidate(
            submission, candidates[0], session_id=session_id
        )
    if candidates:
        if not session_id:
            raise AudioAgentError("Hermes 会话标识缺失，无法安全确认联系人")
        address_book.store_pending(
            session_id,
            {
                "query": query,
                "input_mode": input_mode,
                "submission": submission,
                "candidates": candidates,
            },
        )
        return {
            "ok": True,
            "requires_confirmation": True,
            "match_type": match_type,
            "candidates": candidates,
            "user_response": _confirmation_text(candidates, input_mode=input_mode),
        }

    return {
        "ok": False,
        "requires_phone": True,
        "match_type": "none",
        "candidates": [],
        "user_response": f"通讯录中未找到“{query}”，请提供姓名和电话号码。",
    }


@_handler
def confirm_address_book_contact(
    args: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    """Submit a previously resolved candidate after a deterministic user confirmation."""

    session_id = _hermes_session(args, kwargs)
    stored = address_book.pop_pending(session_id)
    if stored is None:
        return {
            "ok": False,
            "user_response": "没有等待确认的联系人，请重新发送拨号指令。",
        }
    choice = address_book.confirmation_choice(
        session_id, fallback=int(args.get("choice") or 1)
    )
    candidates = stored.get("candidates") or []
    if not 1 <= choice <= len(candidates):
        address_book.store_pending(session_id, stored)
        return {
            "ok": False,
            "user_response": f"请选择 1 到 {len(candidates)} 之间的联系人编号。",
        }
    return _submit_address_book_candidate(
        stored["submission"], candidates[choice - 1], session_id=session_id
    )


@_handler
def get_outbound_task(args: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    campaign_id = _required_text(args, "campaign_id", 200)
    return _task_status(
        AudioAgentClient(),
        campaign_id,
        include_results=args.get("include_results", True) is not False,
        include_transcript=args.get("include_transcript") is True,
    )


@_handler
def get_latest_call_transcript(
    args: dict[str, Any], **_kwargs: Any
) -> dict[str, Any]:
    """Return the exact assistant/customer text from the latest answered call."""

    client = AudioAgentClient()
    direction = str(args.get("direction") or "outbound")
    call = _latest_answered_call(client, direction=direction)
    call_id = str(call.get("id") or "").strip()
    if not call_id:
        raise AudioAgentError("AudioAgent returned a call without an ID")
    timeline = client.request(
        "GET",
        client.project_path(f"/sessions/{quote(call_id, safe='')}"),
    )
    transcript: list[dict[str, str]] = []
    for event in timeline.get("events") or []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type not in {"user.transcript", "agent.response"}:
            continue
        text = str(payload.get("text") or "").strip()
        if not text:
            continue
        transcript.append(
            {
                "role": "user" if event_type == "user.transcript" else "assistant",
                "text": text,
            }
        )
    customer = _call_customer(call)
    return {
        "ok": True,
        "call_id": call_id,
        "direction": call.get("direction") or direction,
        "customer": customer,
        "answered_at": call.get("answered_at"),
        "ended_at": call.get("ended_at"),
        "transcript": transcript,
        "formatted_text": _format_latest_transcript(call, transcript),
    }


@_handler
def get_latest_call_recording(
    args: dict[str, Any], **_kwargs: Any
) -> dict[str, Any]:
    """Cache the latest recording for Hermes MEDIA delivery to Weixin."""

    client = AudioAgentClient()
    direction = str(args.get("direction") or "outbound")
    call = _latest_answered_call(
        client,
        direction=direction,
        require_recording=True,
    )
    call_id = str(call.get("id") or "").strip()
    if not call_id:
        raise AudioAgentError("AudioAgent returned a call without an ID")
    access = client.request(
        "GET",
        client.project_path(
            f"/telephony/calls/{quote(call_id, safe='')}/recording-access?ttl_seconds=300"
        ),
    )
    recording_url = str(access.get("url") or "").strip()
    if not recording_url:
        raise AudioAgentError("AudioAgent did not return a recording download URL")

    target = _recording_cache_path(call_id, recording_url)
    max_bytes = min(
        512 * 1024 * 1024,
        max(
            1024 * 1024,
            int(os.getenv("AUDIOAGENT_RECORDING_MAX_BYTES", str(64 * 1024 * 1024))),
        ),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    downloaded = target.with_name(
        f".{target.stem}.{token}.download{target.suffix}"
    )
    normalized = target.with_name(f".{target.stem}.{token}.normalized.mp3")
    try:
        source_size_bytes = client.download_to_path(
            recording_url,
            downloaded,
            max_bytes=max_bytes,
        )
        downloaded.chmod(0o600)
        if target.suffix.lower() == ".mp3":
            size_bytes = _normalize_mp3_for_delivery(downloaded, normalized)
            normalized.replace(target)
        else:
            size_bytes = source_size_bytes
            downloaded.replace(target)
    finally:
        for temporary in (downloaded, normalized):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    customer = _call_customer(call)
    return {
        "ok": True,
        "call_id": call_id,
        "direction": call.get("direction") or direction,
        "customer": customer,
        "answered_at": call.get("answered_at"),
        "ended_at": call.get("ended_at"),
        "source_size_bytes": source_size_bytes,
        "size_bytes": size_bytes,
        "media_path": str(target.resolve()),
        "media_directive": f"MEDIA:{target.resolve()}",
        "message": "录音已准备好；请在本轮最终回复中原样输出 media_directive。",
    }


@_handler
def wait_outbound_task(args: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    campaign_id = _required_text(args, "campaign_id", 200)
    timeout_seconds = min(3600, max(10, int(args.get("timeout_seconds") or 900)))
    poll_seconds = min(30.0, max(1.0, float(args.get("poll_seconds") or 3)))
    client = AudioAgentClient()
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = _task_status(
            client,
            campaign_id,
            include_results=False,
            include_transcript=False,
        )
        if status["finished"]:
            return _task_status(
                client,
                campaign_id,
                include_results=True,
                include_transcript=args.get("include_transcript") is True,
            )
        if time.monotonic() >= deadline:
            status["timed_out"] = True
            status["message"] = "task is still running; query it again later"
            return status
        time.sleep(poll_seconds)


@_handler
def cancel_outbound_task(args: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    if args.get("confirmed") is not True:
        raise ValueError("explicit operator confirmation is required to cancel a task")
    campaign_id = _required_text(args, "campaign_id", 200)
    client = AudioAgentClient()
    campaign = client.request(
        "PUT",
        client.project_path(
            f"/telephony/campaigns/{quote(campaign_id, safe='')}/status"
        ),
        {"status": "canceled"},
    )
    return {
        "ok": True,
        "campaign_id": campaign_id,
        "status": campaign.get("status"),
    }
