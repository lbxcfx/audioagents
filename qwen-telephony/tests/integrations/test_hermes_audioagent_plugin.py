from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from PIL import Image


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations import hermes_audioagent
from integrations.hermes_audioagent import (
    delivery,
    middleware,
    result_card,
    schemas,
    tools,
)


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
            self.middleware = []

        def register_tool(self, **options) -> None:
            self.tools.append(options)

        def register_skill(self, name, path) -> None:
            self.skills.append((name, path))

        def register_hook(self, name, handler) -> None:
            self.hooks.append((name, handler))

        def register_middleware(self, name, handler) -> None:
            self.middleware.append((name, handler))

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
    assert context.middleware == [
        ("llm_request", middleware.isolate_wechat_outbound_request)
    ]


def test_wechat_outbound_request_drops_history_and_injects_direct_execution() -> None:
    request = {
        "messages": [
            {"role": "system", "content": "Hermes system"},
            {"role": "user", "content": "以后请先给我看 Prompt。"},
            {"role": "assistant", "content": "好的，会先确认。"},
            {
                "role": "user",
                "content": "给任总打电话，18332362029，问今晚是否吃饭。",
            },
        ],
        "model": "deepseek-v4-pro",
    }

    result = middleware.isolate_wechat_outbound_request(
        request=request,
        platform="weixin",
    )

    assert result is not None
    messages = result["request"]["messages"]
    assert len(messages) == 2
    assert messages[0] == {"role": "system", "content": "Hermes system"}
    assert "给任总打电话" in messages[1]["content"]
    assert "不得参考或延续任何历史对话" in messages[1]["content"]
    assert "不预览、不要求确认" in messages[1]["content"]
    assert "以后请先给我看" not in str(messages)
    assert request["messages"][3]["content"].endswith("是否吃饭。")


def test_wechat_outbound_request_preserves_current_tool_loop() -> None:
    request = {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "旧任务"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "user", "content": "外呼13800000000提醒续费"},
            {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "content": "schema loaded"},
        ]
    }

    result = middleware.isolate_wechat_outbound_request(
        request=request,
        platform="weixin",
    )

    assert result is not None
    messages = result["request"]["messages"]
    assert [item["role"] for item in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert messages[-1]["content"] == "schema loaded"


def test_non_outbound_or_non_weixin_request_is_unchanged() -> None:
    request = {"messages": [{"role": "user", "content": "查询13800000000"}]}

    assert (
        middleware.isolate_wechat_outbound_request(
            request=request,
            platform="weixin",
        )
        is None
    )
    assert (
        middleware.isolate_wechat_outbound_request(
            request={
                "messages": [
                    {"role": "user", "content": "打电话给13800000000"}
                ]
            },
            platform="cli",
        )
        is None
    )


def test_submit_schema_requires_no_confirmation() -> None:
    schema = schemas.SUBMIT_OUTBOUND_TASK["parameters"]

    assert "confirmed" not in schema["properties"]
    assert schema["required"] == ["task_name", "prompt", "customers"]


def test_prompt_preparation_fixes_identity_without_missing_fact_check() -> None:
    prompt = tools._prepare_prompt_snapshot(
        "我是XXX的助理；餐厅如知道请补充，根据实际情况沟通。"
    )

    assert prompt.startswith("# 固定身份与信息规则（最高优先级）")
    assert "我是李宝祥的智能助理" in prompt
    task_prompt = prompt.split("# 微信任务\n", 1)[1]
    assert "XXX" not in task_prompt
    assert task_prompt.startswith("我是李宝祥的智能助理")
    assert "餐厅如知道请补充，根据实际情况沟通" in prompt


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
    prompt_snapshot = campaign_payload["metadata"]["task"]["prompt_snapshot"]
    assert prompt_snapshot.startswith("# 固定身份与信息规则（最高优先级）")
    assert "我是李宝祥的智能助理" in prompt_snapshot
    assert prompt_snapshot.endswith("请向 {{customer_name}} 确认续费。")
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


def test_result_card_renders_clear_customer_summary(monkeypatch, tmp_path) -> None:
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    bold_font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    monkeypatch.setenv("AUDIOAGENT_CARD_FONT", font)
    monkeypatch.setenv("AUDIOAGENT_CARD_FONT_BOLD", bold_font)
    status = {
        "campaign_name": "产品介绍",
        "status": "completed",
        "results": [
            {
                "status": "completed",
                "phone": "+8613800000000",
                "customer": {"name": "林经理"},
                "answered_at": "2026-08-06T04:00:00Z",
                "ended_at": "2026-08-06T04:00:42Z",
                "summary": "客户希望下周再次联系。",
            }
        ],
    }

    path = delivery.render_result_card(
        status,
        campaign_id="campaign-1",
        output_path=tmp_path / "result.png",
    )

    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.width == 1080
        assert image.height >= 600


def test_result_card_uses_failed_business_outcome_for_terminal_campaign() -> None:
    assert result_card.result_outcome(
        {
            "status": "completed",
            "results": [{"status": "failed", "failure_code": "sip_500"}],
        }
    ) == "failed"


def test_result_delivery_sends_card_as_weixin_media(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}
    card = tmp_path / "result.png"
    card.write_bytes(b"png")

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(delivery.shutil, "which", lambda _name: "/usr/bin/hermes")
    monkeypatch.setattr(delivery.subprocess, "run", fake_run)

    assert delivery._send_message("任务已结束", card_path=card) is True
    command = captured["command"]
    assert command[-1] == f"MEDIA:{card}"
    assert command[command.index("--to") + 1] == "weixin"
    assert captured["kwargs"]["env"]["AUDIOAGENT_RESULT_FORWARDING"] == "false"
    assert captured["kwargs"]["env"]["AUDIOAGENT_RESULT_FORWARDER_CHILD"] == "1"


def test_result_delivery_claim_is_persistent_and_at_most_once(monkeypatch, tmp_path) -> None:
    state_file = tmp_path / "deliveries.json"
    monkeypatch.setenv("AUDIOAGENT_DELIVERY_STATE_FILE", str(state_file))

    assert delivery._claim_delivery("campaign-1") is True
    assert delivery._claim_delivery("campaign-1") is False

    delivery._mark_delivered("campaign-1")
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["attempted_campaign_ids"] == ["campaign-1"]
    assert payload["delivered_campaign_ids"] == ["campaign-1"]


def test_result_forwarder_is_disabled_inside_hermes_send_child(monkeypatch) -> None:
    monkeypatch.setenv("AUDIOAGENT_RESULT_FORWARDING", "true")
    monkeypatch.setenv("AUDIOAGENT_RESULT_FORWARDER_CHILD", "1")

    assert delivery._enabled() is False
