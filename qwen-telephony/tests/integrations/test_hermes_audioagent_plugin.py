from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations import hermes_audioagent
from integrations.hermes_audioagent import delivery, tools


class SubmitClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []

    def project_path(self, suffix: str) -> str:
        return suffix

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        self.requests.append((method, path, payload))
        if path == "/telephony/contacts/import":
            return {"items": [{"id": "contact-1"}], "count": 1}
        if path == "/telephony/campaigns" and method == "POST":
            return {"id": "campaign-1", "status": "draft"}
        if path.endswith("/contacts"):
            return {"campaign_id": "campaign-1", "added": 1}
        if path.endswith("/status"):
            return {"id": "campaign-1", "status": payload["status"]}
        raise AssertionError((method, path, payload))


def test_plugin_registers_tools_and_bundled_skill() -> None:
    class Context:
        def __init__(self) -> None:
            self.tools = []
            self.skills = []
            self.hooks = []

        def register_tool(self, **options) -> None:
            self.tools.append(options)

        def register_skill(self, name, path) -> None:
            self.skills.append((name, path))

        def register_hook(self, name, handler) -> None:
            self.hooks.append((name, handler))

    context = Context()
    hermes_audioagent.register(context)

    assert {item["name"] for item in context.tools} == {
        "audioagent_submit_outbound_task",
        "audioagent_get_outbound_task",
        "audioagent_wait_outbound_task",
        "audioagent_cancel_outbound_task",
    }
    assert context.skills[0][0] == "outbound-calling"
    assert context.skills[0][1].is_file()
    assert context.hooks == []


def test_submit_requires_explicit_confirmation() -> None:
    result = json.loads(
        tools.submit_outbound_task(
            {
                "task_name": "续费提醒",
                "prompt": "提醒续费",
                "customers": [{"phone": "13800000000"}],
                "confirmed": False,
            }
        )
    )

    assert result["ok"] is False
    assert "confirmation" in result["error"]


def test_submit_creates_campaign_with_immutable_prompt_and_customer_metadata(
    monkeypatch,
) -> None:
    client = SubmitClient()
    monkeypatch.setattr(tools, "AudioAgentClient", lambda: client)
    monkeypatch.setenv("AUDIOAGENT_AGENT_NAME", "qwen-phone-agent")
    monkeypatch.setenv("AUDIOAGENT_TRUNK_ID", "trunk-1")
    monkeypatch.setenv("AUDIOAGENT_SOURCE_NUMBER", "+8610000000000")

    result = json.loads(
        tools.submit_outbound_task(
            {
                "task_id": "wx-task-1",
                "task_name": "续费提醒",
                "prompt": "请向 {{customer_name}} 确认续费。",
                "customers": [
                    {
                        "phone": "13800000000",
                        "name": "林经理",
                        "company": "示例科技",
                        "profile": {"plan": "enterprise"},
                    }
                ],
                "confirmed": True,
                "max_concurrency": 2,
                "scene_id": 42,
            },
            task_id="hermes-session-1",
        )
    )

    assert result == {
        "ok": True,
        "task_id": "wx-task-1",
        "campaign_id": "campaign-1",
        "status": "running",
        "customer_count": 1,
        "max_concurrency": 2,
        "message": "outbound task accepted",
    }
    contact_payload = client.requests[0][2]["contacts"][0]
    assert contact_payload["phone_number"] == "+8613800000000"
    assert contact_payload["metadata"] == {
        "company": "示例科技",
        "profile": {"plan": "enterprise"},
    }
    campaign_payload = client.requests[1][2]
    assert campaign_payload["metadata"]["task"]["prompt_snapshot"].startswith("请向")
    assert campaign_payload["metadata"]["task"]["scene_id"] == 42
    assert campaign_payload["metadata"]["delivery"] == {
        "hermes_session_id": "hermes-session-1"
    }


class StatusClient:
    def project_path(self, suffix: str) -> str:
        return suffix

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if path == "/telephony/campaigns?limit=500":
            return {
                "items": [
                    {
                        "id": "campaign-1",
                        "name": "续费提醒",
                        "status": "completed",
                        "contact_count": 1,
                        "blocked_count": 0,
                        "metadata": {"task": {"id": "wx-task-1"}},
                    }
                ]
            }
        if path == "/telephony/calls?direction=outbound&limit=500":
            return {
                "items": [
                    {
                        "id": "call-1",
                        "campaign_id": "campaign-1",
                        "status": "completed",
                        "destination_number": "+8613800000000",
                        "metadata": {"customer": {"name": "林经理"}},
                        "disposition": "human_answered",
                    }
                ]
            }
        if path == "/sessions/call-1":
            return {
                "events": [
                    {
                        "event_type": "user.transcript",
                        "payload": {"text": "下周续费。"},
                    },
                    {
                        "event_type": "call.result",
                        "payload": {
                            "summary": "客户确认下周续费。",
                            "intent_label": "confirmed",
                        },
                    },
                ]
            }
        raise AssertionError((method, path, payload))


def test_get_task_returns_business_result_without_transcript_by_default(
    monkeypatch,
) -> None:
    monkeypatch.setattr(tools, "AudioAgentClient", StatusClient)

    result = json.loads(
        tools.get_outbound_task(
            {"campaign_id": "campaign-1", "include_results": True}
        )
    )

    assert result["finished"] is True
    assert result["task_id"] == "wx-task-1"
    assert result["call_status_counts"] == {"completed": 1}
    assert result["results"][0]["summary"] == "客户确认下周续费。"
    assert result["results"][0]["intent_label"] == "confirmed"
    assert "transcript" not in result["results"][0]


def test_get_task_can_include_transcript(monkeypatch) -> None:
    monkeypatch.setattr(tools, "AudioAgentClient", StatusClient)

    result = json.loads(
        tools.get_outbound_task(
            {
                "campaign_id": "campaign-1",
                "include_results": True,
                "include_transcript": True,
            }
        )
    )

    assert result["results"][0]["transcript"] == [
        {"role": "user", "text": "下周续费。"}
    ]


def test_result_forwarder_formats_hangup_without_saved_summary() -> None:
    message = delivery.format_result_message(
        {
            "campaign_name": "产品介绍",
            "status": "completed",
            "results": [
                {
                    "status": "completed",
                    "phone": "+8613800000000",
                    "customer": {"name": "林经理"},
                    "answered_at": "2026-08-06T04:00:00Z",
                    "ended_at": "2026-08-06T04:00:42Z",
                    "failure_detail": "room disconnected",
                    "last_user_messages": ["可以", "我再看看"],
                }
            ],
        }
    )

    assert "外呼任务结果" in message
    assert "产品介绍" in message
    assert "0000" in message
    assert "通话时长：42秒" in message
    assert "客户主动挂断" in message
    assert "可以；我再看看" in message
