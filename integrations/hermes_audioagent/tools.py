"""Hermes tool handlers for AudioAgent."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import time
from typing import Any
from urllib.parse import quote
import uuid

from .client import AudioAgentClient, AudioAgentError


TERMINAL_CALL_STATUSES = {
    "completed",
    "failed",
    "busy",
    "no_answer",
    "canceled",
    "blocked",
}
TERMINAL_CAMPAIGN_STATUSES = {"completed", "canceled"}


def is_configured() -> bool:
    return bool(os.getenv("AUDIOAGENT_PROJECT_ID", "").strip())


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _handler(function):
    def wrapped(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            return _json_result(function(args or {}, **kwargs))
        except (AudioAgentError, ValueError) as exc:
            return _json_result({"ok": False, "error": str(exc)})
        except Exception as exc:  # Hermes handlers must never raise.
            return _json_result(
                {
                    "ok": False,
                    "error": f"unexpected {type(exc).__name__}: {exc}",
                }
            )

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
                user_messages.append(text[:1000])
            if include_transcript and text and len(transcript) < 100:
                transcript.append(
                    {
                        "role": "user" if event_type == "user.transcript" else "assistant",
                        "text": text[:2000],
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
    counts: dict[str, int] = {}
    for call in calls:
        status = str(call.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    total = int(campaign.get("contact_count") or 0)
    terminal = sum(
        count for status, count in counts.items() if status in TERMINAL_CALL_STATUSES
    ) + int(campaign.get("blocked_count") or 0)
    finished = str(campaign.get("status") or "") in TERMINAL_CAMPAIGN_STATUSES
    if total > 0 and terminal >= total:
        finished = True
    response: dict[str, Any] = {
        "ok": True,
        "task_id": (campaign.get("metadata") or {}).get("task", {}).get("id")
        if isinstance(campaign.get("metadata"), dict)
        and isinstance((campaign.get("metadata") or {}).get("task"), dict)
        else "",
        "campaign_id": campaign_id,
        "campaign_name": campaign.get("name"),
        "status": campaign.get("status"),
        "finished": finished,
        "contact_count": total,
        "blocked_count": int(campaign.get("blocked_count") or 0),
        "call_status_counts": counts,
    }
    if include_results:
        response["results"] = [
            _timeline_result(client, call, include_transcript=include_transcript)
            for call in sorted(calls, key=lambda item: str(item.get("created_at") or ""))
        ]
    return response


@_handler
def submit_outbound_task(args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    task_name = _required_text(args, "task_name", 200)
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
    max_attempts = int(args.get("max_attempts") or 1)
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

    imported = client.request(
        "POST",
        client.project_path("/telephony/contacts/import"),
        {"contacts": contact_payloads},
    )
    contacts = imported.get("items") or []
    if len(contacts) != len(contact_payloads):
        raise AudioAgentError("AudioAgent did not import every task contact")

    task_snapshot: dict[str, Any] = {
        "id": identifier,
        "prompt_snapshot": prompt,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    }
    if args.get("scene_id") not in {None, ""}:
        task_snapshot["scene_id"] = int(args["scene_id"])
    hermes_session_id = str(kwargs.get("task_id") or "")[:200]
    campaign_payload: dict[str, Any] = {
        "name": task_name,
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
    return {
        "ok": True,
        "task_id": identifier,
        "campaign_id": campaign_id,
        "status": running.get("status"),
        "customer_count": len(contact_payloads),
        "max_concurrency": max_concurrency,
        "message": "outbound task accepted",
    }


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
