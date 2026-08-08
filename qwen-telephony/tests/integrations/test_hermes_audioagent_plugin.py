from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from PIL import Image


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations import hermes_audioagent
from integrations.hermes_audioagent import (
    address_book,
    delivery,
    middleware,
    qwen_asr,
    response_policy,
    result_card,
    schemas,
    tools,
)


class SubmitClient:
    def __init__(self, *, blocked: bool = False) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []
        self.blocked = blocked

    def project_path(self, suffix: str) -> str:
        return suffix

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        self.requests.append((method, path, payload))
        if path == "/telephony/address-book":
            return {"stored": False, "reason": "name_is_not_a_full_person_name"}
        if path == "/telephony/contacts/import":
            return {"items": [{"id": "contact-1"}], "count": 1}
        if path == "/telephony/campaigns" and method == "POST":
            return {"id": "campaign-1", "status": "draft"}
        if path.endswith("/contacts"):
            return {"campaign_id": "campaign-1", "added": 1}
        if path.endswith("/status"):
            if self.blocked:
                return {
                    "id": "campaign-1",
                    "status": "completed",
                    "enqueue_result": {
                        "queued": 0,
                        "blocked": 1,
                        "blocked_reasons": {"consent_missing_or_inactive": 1},
                    },
                }
            return {
                "id": "campaign-1",
                "status": payload["status"],
                "enqueue_result": {"queued": 1, "blocked": 0},
            }
        raise AssertionError((method, path, payload))


def test_plugin_registers_tools_and_bundled_skill(monkeypatch) -> None:
    provider = object()
    monkeypatch.setattr(qwen_asr, "create_provider", lambda: provider)

    class Context:
        def __init__(self) -> None:
            self.tools = []
            self.skills = []
            self.hooks = []
            self.middleware = []
            self.transcription_providers = []

        def register_tool(self, **options) -> None:
            self.tools.append(options)

        def register_skill(self, name, path) -> None:
            self.skills.append((name, path))

        def register_hook(self, name, handler) -> None:
            self.hooks.append((name, handler))

        def register_middleware(self, name, handler) -> None:
            self.middleware.append((name, handler))

        def register_transcription_provider(self, item) -> None:
            self.transcription_providers.append(item)

    context = Context()
    hermes_audioagent.register(context)

    assert {item["name"] for item in context.tools} == {
        "audioagent_resolve_outbound_contact",
        "audioagent_confirm_address_book_contact",
        "audioagent_submit_outbound_task",
        "audioagent_get_outbound_task",
        "audioagent_wait_outbound_task",
        "audioagent_cancel_outbound_task",
        "audioagent_get_latest_call_transcript",
        "audioagent_get_latest_call_recording",
    }
    assert [item[0] for item in context.skills] == [
        "outbound-calling",
        "latest-call-transcript",
        "latest-call-recording",
    ]
    assert all(item[1].is_file() for item in context.skills)
    assert context.hooks == [
        ("transform_llm_output", response_policy.transform_submission_response),
        ("pre_gateway_dispatch", address_book.mark_weixin_voice_input),
    ]
    assert context.transcription_providers == [provider]
    assert context.middleware == [
        ("llm_request", middleware.guide_wechat_address_book_request),
        ("llm_request", middleware.isolate_wechat_outbound_request),
        ("llm_request", middleware.guide_wechat_call_artifact_request),
        ("llm_execution", middleware.return_tool_backed_wechat_response),
    ]


def test_qwen_asr_client_posts_base64_wav(monkeypatch, tmp_path) -> None:
    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"RIFFtest")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": "给任总打电话。"}}]}
            ).encode()

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(qwen_asr, "urlopen", fake_urlopen)
    client = qwen_asr.QwenASRClient(api_key="secret", timeout_seconds=12)

    result = client.transcribe(str(audio_path), language="zh")

    assert result["transcript"] == "给任总打电话。"
    assert captured["timeout"] == 12
    request = captured["request"]
    assert request.full_url.endswith("/compatible-mode/v1/chat/completions")
    assert request.get_header("Authorization") == "Bearer secret"
    payload = json.loads(request.data)
    assert payload["model"] == "qwen3-asr-flash"
    assert payload["asr_options"] == {"enable_itn": True, "language": "zh"}
    data_uri = payload["messages"][0]["content"][0]["input_audio"]["data"]
    assert data_uri == "data:audio/wav;base64,UklGRnRlc3Q="


def test_qwen_asr_rejects_non_aliyuncs_endpoint() -> None:
    try:
        qwen_asr.QwenASRClient(
            api_key="secret",
            base_url="https://example.com/compatible-mode/v1",
        )
    except ValueError as exc:
        assert "aliyuncs.com" in str(exc)
    else:
        raise AssertionError("unsafe Qwen endpoint was accepted")


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
    assert messages[0]["role"] == "system"
    assert "只处理下面这一条用户消息" in messages[0]["content"]
    assert "Hermes system" not in str(messages)
    assert "给任总打电话" in messages[1]["content"]
    assert "以后请先给我看" not in str(messages)
    tool = result["request"]["tools"][0]["function"]
    assert tool["name"] == "tool_call"
    assert tool["parameters"]["properties"]["name"]["pattern"] == (
        "^audioagent_submit_outbound_task$"
    )
    assert "enum" not in tool["parameters"]["properties"]["name"]
    assert tool["parameters"]["properties"]["arguments"] == (
        schemas.SUBMIT_OUTBOUND_TASK["parameters"]
    )
    assert "tool_choice" not in result["request"]
    assert "parallel_tool_calls" not in result["request"]
    assert result["request"]["max_tokens"] == 1800
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
    assert result["request"]["tools"] == []
    assert "tool_choice" not in result["request"]


def test_text_pinyin_without_phone_routes_to_hermes_address_book() -> None:
    address_book.clear_state()
    result = middleware.guide_wechat_address_book_request(
        request={
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "给 lijiakui 打电话，邀请他明天开会"},
            ]
        },
        platform="weixin",
        session_id="wx-address-pinyin",
    )

    assert result is not None
    messages = result["request"]["messages"]
    assert len(messages) == 2
    assert "通讯录外呼任务生成器" in messages[0]["content"]
    assert "任何一句最多说一次" in messages[0]["content"]
    tool = result["request"]["tools"][0]["function"]
    assert tool["name"] == "tool_call"
    assert tool["parameters"]["properties"]["name"]["pattern"] == (
        "^audioagent_resolve_outbound_contact$"
    )
    assert "tool_choice" not in result["request"]
    assert address_book.resolution_context("wx-address-pinyin") == {
        "query": "lijiakui",
        "input_mode": "text",
        "request_text": "给 lijiakui 打电话，邀请他明天开会",
    }


def test_voice_no_phone_always_marks_address_book_confirmation_mode() -> None:
    address_book.clear_state()
    result = middleware.guide_wechat_address_book_request(
        request={
            "messages": [
                {
                    "role": "user",
                    "content": f'"给李家魁打电话"\n{address_book.VOICE_MARKER}',
                }
            ]
        },
        platform="weixin",
        session_id="wx-address-voice",
    )

    assert result is not None
    messages = result["request"]["messages"]
    assert address_book.VOICE_MARKER not in str(messages)
    assert "通讯录外呼任务生成器" in messages[0]["content"]
    tool = result["request"]["tools"][0]["function"]
    assert tool["name"] == "tool_call"
    assert tool["parameters"]["properties"]["name"]["pattern"] == (
        "^audioagent_resolve_outbound_contact$"
    )
    assert "tool_choice" not in result["request"]
    assert address_book.resolution_context("wx-address-voice") == {
        "query": "李家魁",
        "input_mode": "voice",
        "request_text": '"给李家魁打电话"',
    }


def test_weixin_voice_transport_is_tagged_before_central_stt() -> None:
    event = SimpleNamespace(
        source=SimpleNamespace(platform="weixin"),
        message_type="voice",
        text="",
    )

    result = address_book.mark_weixin_voice_input(event=event)

    assert result == {"action": "rewrite", "text": address_book.VOICE_MARKER}


def test_address_book_confirmation_uses_isolated_single_tool() -> None:
    address_book.clear_state()
    address_book.store_pending("wx-confirm", {"candidates": [{"full_name": "李家魁"}]})

    result = middleware.guide_wechat_address_book_request(
        request={
            "messages": [
                {"role": "system", "content": "large hermes system prompt"},
                {"role": "user", "content": "旧消息"},
                {"role": "assistant", "content": "旧回复"},
                {"role": "user", "content": "确认1"},
            ]
        },
        platform="weixin",
        session_id="wx-confirm",
    )

    assert result is not None
    messages = result["request"]["messages"]
    assert len(messages) == 2
    assert "large hermes system prompt" not in str(messages)
    assert messages[-1]["content"] == "确认1"
    tool = result["request"]["tools"][0]["function"]
    assert tool["name"] == "tool_call"
    assert tool["parameters"]["properties"]["name"]["pattern"] == (
        "^audioagent_confirm_address_book_contact$"
    )
    assert "tool_choice" not in result["request"]


def test_address_book_selection_loads_only_current_dialing_context() -> None:
    address_book.clear_state()
    address_book.store_pending(
        "wx-scoped-confirm",
        {
            "request_text": "给常凤香打电话，问一下周末有什么安排？",
            "user_response": (
                "通讯录中找到相似联系人，请确认：\n"
                "1. 常梦香 18001350929\n"
                "2. 常凤玲 13980045107"
            ),
            "candidates": [
                {"full_name": "常梦香", "phone_number": "+8618001350929"},
                {"full_name": "常凤玲", "phone_number": "+8613980045107"},
            ],
        },
    )
    result = middleware.guide_wechat_address_book_request(
        request={
            "messages": [
                {"role": "system", "content": "large hermes system prompt"},
                {"role": "user", "content": "很早以前的消息"},
                {"role": "assistant", "content": "很早以前的回复"},
                {"role": "user", "content": "第二个"},
            ]
        },
        platform="weixin",
        session_id="wx-scoped-confirm",
    )

    assert result is not None
    messages = result["request"]["messages"]
    assert [item["role"] for item in messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert messages[1]["content"] == "给常凤香打电话，问一下周末有什么安排？"
    assert "常梦香" in messages[2]["content"]
    assert messages[3]["content"] == "第二个"
    assert "很早以前" not in str(messages)
    assert "large hermes system prompt" not in str(messages)
    assert result["request"]["tools"][0]["function"]["name"] == "tool_call"
    assert address_book.confirmation_choice("wx-scoped-confirm") == 2


def test_address_book_selection_accepts_ordinal_name_and_phone() -> None:
    address_book.clear_state()
    candidates = [
        {"full_name": "常梦香", "short_name": "梦香", "phone_number": "+8618001350929"},
        {"full_name": "常凤玲", "short_name": "凤玲", "phone_number": "+8613980045107"},
        {"full_name": "张洪强", "short_name": "洪强", "phone_number": "+8613406780567"},
    ]
    for text, expected in (
        ("第一个", 1),
        ("第二个", 2),
        ("选3", 3),
        ("常凤玲", 2),
        ("尾号0567的", 3),
    ):
        address_book.store_pending("wx-selection-forms", {"candidates": candidates})
        assert address_book.note_confirmation("wx-selection-forms", text) is True
        assert address_book.confirmation_choice("wx-selection-forms") == expected


def test_any_pending_contact_followup_never_loads_general_history() -> None:
    address_book.clear_state()
    address_book.store_pending(
        "wx-natural-selection",
        {
            "request_text": "给李家魁打电话",
            "user_response": "1. 李家魁 13070183606\n2. 李家奎 13800000000",
            "candidates": [
                {"full_name": "李家魁", "phone_number": "+8613070183606"},
                {"full_name": "李家奎", "phone_number": "+8613800000000"},
            ],
        },
    )
    result = middleware.guide_wechat_address_book_request(
        request={
            "messages": [
                {"role": "system", "content": "general system with 77000 tokens"},
                {"role": "user", "content": "unrelated old history"},
                {"role": "assistant", "content": "unrelated old answer"},
                {"role": "user", "content": "麻烦选择我刚才说的那位"},
            ]
        },
        platform="weixin",
        session_id="wx-natural-selection",
    )

    assert result is not None
    messages = result["request"]["messages"]
    assert len(messages) == 4
    assert messages[-1]["content"] == "麻烦选择我刚才说的那位"
    assert "77000" not in str(messages)
    assert "unrelated old" not in str(messages)


def test_tool_backed_wechat_response_skips_second_llm_call() -> None:
    response_policy.clear_submission_responses()
    response_policy.remember_user_response("wx-fast-path", "拨号中...")

    def unexpected_provider_call(_request):
        raise AssertionError("the post-tool DeepSeek call must be skipped")

    result = middleware.return_tool_backed_wechat_response(
        request={
            "messages": [
                {"role": "user", "content": "给李总打电话13800000000"},
                {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
                {"role": "tool", "content": '{"ok":true,"queued_count":1}'},
            ]
        },
        next_call=unexpected_provider_call,
        platform="weixin",
        session_id="wx-fast-path",
        model="deepseek-v4-pro",
    )

    assert result.choices[0].message.content == "拨号中..."
    assert result.choices[0].finish_reason == "stop"
    # The normal output hook still owns consumption of the deterministic fact.
    assert response_policy.transform_submission_response(
        response_text="ignored",
        session_id="wx-fast-path",
        platform="weixin",
    ) == "拨号中..."


def test_failed_fast_path_tool_does_not_leak_internal_tool_syntax() -> None:
    response_policy.clear_submission_responses()

    def unexpected_provider_call(_request):
        raise AssertionError("a failed fast-path tool must not reach DeepSeek again")

    result = middleware.return_tool_backed_wechat_response(
        request={
            "messages": [
                {
                    "role": "system",
                    "content": middleware._ADDRESS_BOOK_TASK_SYSTEM_PROMPT,
                },
                {"role": "user", "content": "给常凤香打电话"},
                {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
                {"role": "tool", "content": "tool execution failed"},
            ]
        },
        next_call=unexpected_provider_call,
        platform="weixin",
        session_id="wx-failed-fast-path",
        model="deepseek-v4-pro",
    )

    assert result.choices[0].message.content == "电话未拨出：外呼工具执行失败。"


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


def test_quoted_result_or_dialing_ack_never_launches_another_call() -> None:
    quoted_result = (
        "正在拨打李家魁 13070183606\n"
        "**通话状态：** 已接通\n"
        "**通话摘要：** 李家魁已确认参加。\n"
        "[OutOfBand answer]\n为什么会这样？"
    )

    assert middleware.is_wechat_outbound_request(quoted_result) is False
    assert middleware.is_wechat_outbound_request(
        "拨号中... 13070183606，为什么没响应？"
    ) is False


def test_wechat_latest_call_transcript_request_loads_hermes_skill_and_tool() -> None:
    result = middleware.guide_wechat_call_artifact_request(
        request={
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "请把刚才的通话聊天记录发给我"},
            ]
        },
        platform="weixin",
    )

    assert result is not None
    content = result["request"]["messages"][-1]["content"]
    assert "audioagent:latest-call-transcript" in content
    assert "audioagent_get_latest_call_transcript" in content
    assert "必须由 Hermes 的 audioagent 插件执行" in content
    assert "audioagent_get_latest_call_recording" not in content


def test_wechat_latest_call_recording_request_requires_media_delivery() -> None:
    result = middleware.guide_wechat_call_artifact_request(
        request={"messages": [{"role": "user", "content": "把最后一通电话录音发来"}]},
        platform="weixin",
    )

    assert result is not None
    content = result["request"]["messages"][0]["content"]
    assert "audioagent:latest-call-recording" in content
    assert "audioagent_get_latest_call_recording" in content
    assert "media_directive 原样放在独立一行" in content


def test_wechat_can_request_transcript_and_recording_together() -> None:
    result = middleware.guide_wechat_call_artifact_request(
        request={
            "messages": [
                {"role": "user", "content": "发送最近通话的聊天记录和录音"}
            ]
        },
        platform="weixin",
    )

    assert result is not None
    content = result["request"]["messages"][0]["content"]
    assert "audioagent_get_latest_call_transcript" in content
    assert "audioagent_get_latest_call_recording" in content


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
    assert "严禁原样重复、换词复述或循环播放" in prompt
    assert "客户明确答复或说“再见”后" in prompt


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
                "invitation_content": "下周企业套餐续费确认",
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
        "campaign_name": "续费提醒",
        "invitation_content": "下周企业套餐续费确认",
        "status": "running",
        "customer_count": 1,
        "queued_count": 1,
        "blocked_count": 0,
        "max_concurrency": 2,
        "message": "outbound task accepted",
    }
    campaign_payload = client.requests[0][2]
    assert campaign_payload["name"] == "续费提醒 [wx-task-1]"
    assert campaign_payload["max_attempts"] == 2
    contact_payload = client.requests[1][2]["contacts"][0]
    assert contact_payload["phone_number"] == "+8613800000000"
    assert contact_payload["metadata"] == {
        "company": "示例科技",
        "profile": {"plan": "enterprise"},
    }
    prompt_snapshot = campaign_payload["metadata"]["task"]["prompt_snapshot"]
    assert prompt_snapshot.startswith("# 固定身份与信息规则（最高优先级）")
    assert "我是李宝祥的智能助理" in prompt_snapshot
    assert prompt_snapshot.endswith("请向 {{customer_name}} 确认续费。")
    assert campaign_payload["metadata"]["task"]["scene_id"] == 42
    assert campaign_payload["metadata"]["task"]["display_name"] == "续费提醒"
    assert (
        campaign_payload["metadata"]["task"]["invitation_content"]
        == "下周企业套餐续费确认"
    )
    assert campaign_payload["metadata"]["delivery"] == {
        "hermes_session_id": "hermes-session-1"
    }

    # Even if the LLM fabricates a detailed answer, the Hermes hook replaces
    # the whole response with the only tool-backed dialing state.
    assert response_policy.transform_submission_response(
        response_text="已接通，客户已经同意。 [OutOfBand answer]",
        session_id="hermes-session-1",
        platform="weixin",
    ) == "拨号中..."


def test_submit_reports_all_contacts_blocked_before_dialing(monkeypatch) -> None:
    client = SubmitClient(blocked=True)
    monkeypatch.setattr(tools, "AudioAgentClient", lambda: client)
    monkeypatch.setenv("AUDIOAGENT_AGENT_NAME", "qwen-phone-agent")
    monkeypatch.setenv("AUDIOAGENT_TRUNK_ID", "trunk-1")

    result = json.loads(
        tools.submit_outbound_task(
            {
                "task_id": "wx-task-blocked",
                "task_name": "跑步邀约",
                "prompt": "确认对方是否方便跑步。",
                "customers": [{"phone": "18001350929", "name": "常凤香"}],
            },
            task_id="hermes-session-blocked",
        )
    )

    assert result["ok"] is False
    assert result["status"] == "completed"
    assert result["queued_count"] == 0
    assert result["blocked_count"] == 1
    assert result["error_code"] == "consent_missing_or_inactive"
    assert "电话未拨出" in result["error"]
    failure = response_policy.transform_submission_response(
        response_text="正在拨打，稍后同步结果",
        session_id="hermes-session-blocked",
        platform="weixin",
    )
    assert failure is not None
    assert failure.startswith("电话未拨出：")
    assert result["error"] in failure


class AddressBookClient(SubmitClient):
    def __init__(self, lookup: dict) -> None:
        super().__init__()
        self.lookup = lookup

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if path == "/telephony/address-book/sync":
            self.requests.append((method, path, payload))
            return {"stored": 1, "skipped": 0}
        if path.startswith("/telephony/address-book/lookup?"):
            self.requests.append((method, path, payload))
            return self.lookup
        return super().request(method, path, payload)


def _address_task_args(query: str) -> dict:
    return {
        "query": query,
        "task_name": "会议邀请",
        "invitation_content": "明天下午三点动捕会议",
        "prompt": "邀请 {{customer_name}} 参加明天下午三点动捕会议。",
    }


def test_unique_exact_pinyin_text_match_dials_immediately(monkeypatch) -> None:
    address_book.clear_state()
    response_policy.clear_submission_responses()
    client = AddressBookClient(
        {
            "match_type": "exact",
            "candidates": [
                {"full_name": "李家魁", "phone_number": "+8613070183606"}
            ],
        }
    )
    monkeypatch.setattr(tools, "AudioAgentClient", lambda: client)
    monkeypatch.setenv("AUDIOAGENT_AGENT_NAME", "qwen-phone-agent")
    monkeypatch.setenv("AUDIOAGENT_TRUNK_ID", "trunk-1")
    address_book.mark_resolution_context(
        "wx-exact-pinyin", query="lijiakui", input_mode="text"
    )

    result = json.loads(
        tools.resolve_outbound_contact(
            _address_task_args("模型不应改写这个值"),
            task_id="turn-task-differs-from-session",
            session_id="wx-exact-pinyin",
        )
    )

    assert result["ok"] is True
    assert result["queued_count"] == 1
    lookup_path = next(path for _method, path, _payload in client.requests if "lookup" in path)
    assert "query=lijiakui" in lookup_path
    imported = next(
        payload
        for method, path, payload in client.requests
        if method == "POST" and path == "/telephony/contacts/import"
    )
    assert imported["contacts"][0]["name"] == "李家魁"
    assert imported["contacts"][0]["phone_number"] == "+8613070183606"
    assert response_policy.transform_submission_response(
        response_text="已经接通并同意了",
        session_id="wx-exact-pinyin",
        platform="weixin",
    ) == "拨号中..."


def test_fuzzy_text_match_requires_confirmation_before_dialing(monkeypatch) -> None:
    address_book.clear_state()
    response_policy.clear_submission_responses()
    client = AddressBookClient(
        {
            "match_type": "fuzzy",
            "candidates": [
                {
                    "full_name": "李家魁",
                    "phone_number": "+8613070183606",
                    "score": 0.875,
                }
            ],
        }
    )
    monkeypatch.setattr(tools, "AudioAgentClient", lambda: client)
    monkeypatch.setenv("AUDIOAGENT_AGENT_NAME", "qwen-phone-agent")
    monkeypatch.setenv("AUDIOAGENT_TRUNK_ID", "trunk-1")
    address_book.mark_resolution_context(
        "wx-fuzzy-contact", query="李家奎", input_mode="text"
    )

    resolved = json.loads(
        tools.resolve_outbound_contact(
            _address_task_args("李家奎"), task_id="wx-fuzzy-contact"
        )
    )

    assert resolved["requires_confirmation"] is True
    assert not any(path == "/telephony/campaigns" for _method, path, _ in client.requests)
    confirmation = response_policy.transform_submission_response(
        response_text="我已经替你拨出了",
        session_id="wx-fuzzy-contact",
        platform="weixin",
    )
    assert confirmation is not None
    assert confirmation.startswith("通讯录中找到相似联系人，请确认：")
    assert "李家魁 13070183606" in confirmation

    assert address_book.note_confirmation("wx-fuzzy-contact", "确认1") is True
    confirmed = json.loads(
        tools.confirm_address_book_contact({}, task_id="wx-fuzzy-contact")
    )
    assert confirmed["ok"] is True
    assert confirmed["queued_count"] == 1
    assert response_policy.transform_submission_response(
        response_text="客户答应了",
        session_id="wx-fuzzy-contact",
        platform="weixin",
    ) == "拨号中..."


def test_exact_voice_match_still_requires_confirmation(monkeypatch) -> None:
    address_book.clear_state()
    response_policy.clear_submission_responses()
    client = AddressBookClient(
        {
            "match_type": "exact",
            "candidates": [
                {"full_name": "李家魁", "phone_number": "+8613070183606"}
            ],
        }
    )
    monkeypatch.setattr(tools, "AudioAgentClient", lambda: client)
    address_book.mark_resolution_context(
        "wx-voice-exact", query="李家魁", input_mode="voice"
    )

    resolved = json.loads(
        tools.resolve_outbound_contact(
            _address_task_args("李家魁"), task_id="wx-voice-exact"
        )
    )

    assert resolved["requires_confirmation"] is True
    assert resolved["user_response"].startswith("语音识别结果可能有误，请确认联系人：")
    assert not any(path == "/telephony/campaigns" for _method, path, _ in client.requests)


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
        if path == "/telephony/campaigns/campaign-1/contacts?limit=5000":
            return {
                "items": [
                    {
                        "campaign_id": "campaign-1",
                        "contact_id": "contact-1",
                        "call_id": "call-1",
                        "status": "completed",
                        "failure_reason": "",
                        "phone_number": "+8613800000000",
                        "name": "林经理",
                        "metadata": {},
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


class LatestCallClient:
    downloaded_urls: list[str] = []

    def project_path(self, suffix: str) -> str:
        return suffix

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        assert method == "GET"
        if path == "/telephony/calls?limit=100&direction=outbound":
            return {
                "items": [
                    {
                        "id": "missed-call",
                        "direction": "outbound",
                        "status": "no_answer",
                        "answered_at": None,
                    },
                    {
                        "id": "call-latest",
                        "direction": "outbound",
                        "status": "completed",
                        "destination_number": "+8613800000000",
                        "answered_at": "2026-08-07T04:04:25Z",
                        "ended_at": "2026-08-07T04:04:34Z",
                        "recording_status": "completed",
                        "recording_storage_uri": "s3://recordings/call-latest.mp3",
                        "metadata": {"customer": {"name": "林经理"}},
                    },
                ]
            }
        if path == "/sessions/call-latest":
            return {
                "events": [
                    {
                        "event_type": "agent.response",
                        "payload": {"text": "您好，我是李宝祥的智能助理。"},
                    },
                    {
                        "event_type": "user.transcript",
                        "payload": {"text": "你好，请讲。"},
                    },
                    {
                        "event_type": "call.result",
                        "payload": {"summary": "客户愿意继续沟通。"},
                    },
                ]
            }
        if path == (
            "/telephony/calls/call-latest/recording-access?ttl_seconds=300"
        ):
            return {
                "call_id": "call-latest",
                "status": "completed",
                "url": "http://127.0.0.1:9000/recordings/call-latest.mp3?signature=x",
            }
        raise AssertionError((method, path, payload))

    def download_to_path(
        self, url: str, target: Path, *, max_bytes: int
    ) -> int:
        assert max_bytes >= 1024 * 1024
        self.downloaded_urls.append(url)
        target.write_bytes(b"ID3test-recording")
        return target.stat().st_size


def test_get_latest_call_transcript_returns_exact_weixin_text(monkeypatch) -> None:
    monkeypatch.setattr(tools, "AudioAgentClient", LatestCallClient)

    result = json.loads(tools.get_latest_call_transcript({}))

    assert result["ok"] is True
    assert result["call_id"] == "call-latest"
    assert result["transcript"] == [
        {"role": "assistant", "text": "您好，我是李宝祥的智能助理。"},
        {"role": "user", "text": "你好，请讲。"},
    ]
    assert "AI：您好，我是李宝祥的智能助理。" in result["formatted_text"]
    assert "客户：你好，请讲。" in result["formatted_text"]
    assert "missed-call" not in result["formatted_text"]


def test_get_latest_call_recording_prepares_hermes_media_attachment(
    monkeypatch, tmp_path
) -> None:
    LatestCallClient.downloaded_urls = []
    monkeypatch.setattr(tools, "AudioAgentClient", LatestCallClient)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))

    def normalize(source: Path, target: Path) -> int:
        assert source.read_bytes() == b"ID3test-recording"
        target.write_bytes(b"ID3normalized-recording")
        return target.stat().st_size

    monkeypatch.setattr(tools, "_normalize_mp3_for_delivery", normalize)

    result = json.loads(tools.get_latest_call_recording({}))

    assert result["ok"] is True
    assert result["call_id"] == "call-latest"
    assert result["media_directive"].startswith("MEDIA:")
    media_path = Path(result["media_path"])
    assert media_path.is_file()
    assert media_path.read_bytes() == b"ID3normalized-recording"
    assert result["source_size_bytes"] == len(b"ID3test-recording")
    assert result["size_bytes"] == len(b"ID3normalized-recording")
    assert media_path.parent == tmp_path / "hermes" / "cache" / "documents"
    assert "signature=x" not in json.dumps(result)
    assert LatestCallClient.downloaded_urls == [
        "http://127.0.0.1:9000/recordings/call-latest.mp3?signature=x"
    ]


def test_mp3_normalization_uses_fixed_bitrate_and_xing_header(
    monkeypatch, tmp_path
) -> None:
    source = tmp_path / "source.mp3"
    target = tmp_path / "target.mp3"
    source.write_bytes(b"malformed-short-mp3")
    observed: list[str] = []

    def run(command, **options):
        observed.extend(command)
        assert options["timeout"] == 120
        target.write_bytes(b"normalized-mp3")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(tools.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(tools.subprocess, "run", run)

    assert tools._normalize_mp3_for_delivery(source, target) == len(
        b"normalized-mp3"
    )
    assert "libmp3lame" in observed
    assert observed[observed.index("-b:a") + 1] == "64k"
    assert observed[observed.index("-write_xing") + 1] == "1"
    assert observed[observed.index("-ar") + 1] == "16000"


class BlockedStatusClient:
    def project_path(self, suffix: str) -> str:
        return suffix

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if path == "/telephony/campaigns?limit=500":
            return {
                "items": [
                    {
                        "id": "campaign-blocked",
                        "name": "跑步邀约 [task-blocked]",
                        "status": "completed",
                        "contact_count": 1,
                        "blocked_count": 1,
                        "metadata": {
                            "task": {
                                "id": "task-blocked",
                                "display_name": "跑步邀约",
                            }
                        },
                    }
                ]
            }
        if path == "/telephony/calls?direction=outbound&limit=500":
            return {"items": []}
        if path == "/telephony/campaigns/campaign-blocked/contacts?limit=5000":
            return {
                "items": [
                    {
                        "campaign_id": "campaign-blocked",
                        "contact_id": "contact-blocked",
                        "call_id": "",
                        "status": "blocked",
                        "failure_reason": "consent_missing_or_inactive",
                        "phone_number": "+8618001350929",
                        "name": "常凤香",
                        "metadata": {},
                    }
                ]
            }
        raise AssertionError((method, path, payload))


def test_get_task_returns_blocked_contact_without_call_record(monkeypatch) -> None:
    monkeypatch.setattr(tools, "AudioAgentClient", BlockedStatusClient)

    result = json.loads(
        tools.get_outbound_task(
            {"campaign_id": "campaign-blocked", "include_results": True}
        )
    )

    assert result["campaign_name"] == "跑步邀约"
    assert result["finished"] is True
    assert result["contact_status_counts"] == {"blocked": 1}
    assert result["results"][0]["status"] == "blocked"
    assert result["results"][0]["customer"]["name"] == "常凤香"
    assert "电话未拨出" in result["results"][0]["summary"]


def test_result_forwarder_formats_hangup_without_saved_summary() -> None:
    message = delivery.format_result_message(
        {
            "campaign_name": "产品介绍",
            "invitation_content": "下周产品续费沟通",
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

    assert "**收信人：** 林经理" in message
    assert "**电话：** 13800000000" in message
    assert "**邀请内容：** 下周产品续费沟通" in message
    assert "**发起人：** 李宝祥（智能助理代拨）" in message
    assert "**通话状态：** 已接通" in message
    assert "**通话记录：**" in message
    assert "无（数据库中没有 AI 或客户的文字通话记录）。" in message
    assert "通话摘要" not in message
    assert "MEDIA:" not in message


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

    path = result_card.render_result_card(
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


def test_no_answer_result_has_clear_customer_facing_reason() -> None:
    item = {
        "status": "no_answer",
        "failure_code": "sip_408",
        "failure_detail": "no answer before 45-second ringing timeout",
    }

    assert result_card._status_label("no_answer") == "未接听"
    assert result_card._summary(item) == "客户在响铃时限内未接听，电话未接通。"
    message = delivery.format_result_message(
        {"campaign_name": "跑步邀约", "status": "completed", "results": [item]}
    )
    assert "**通话状态：** 未接通" in message
    assert "**通话记录：**" in message
    assert "无（数据库中没有 AI 或客户的文字通话记录）。" in message
    assert "未形成业务摘要" not in message


def test_result_markdown_matches_requested_business_format() -> None:
    message = delivery.format_result_message(
        {
            "invitation_content": "今晚 8:30 莲花河跑步",
            "results": [
                {
                    "status": "completed",
                    "phone": "+8618001350929",
                    "customer": {"name": "常凤香"},
                    "answered_at": "2026-08-06T09:00:00Z",
                    "summary": "常姐已经同意今晚 8:30 去莲花河跑步。请准时到达。",
                    "transcript": [
                        {"role": "assistant", "text": "今晚八点半一起跑步，您方便吗？"},
                        {"role": "user", "text": "可以。"},
                    ],
                }
            ],
        }
    )

    assert message == (
        "**收信人：** 常凤香\n"
        "**电话：** 18001350929\n"
        "**邀请内容：** 今晚 8:30 莲花河跑步\n"
        "**发起人：** 李宝祥（智能助理代拨）\n"
        "**通话状态：** 已接通\n"
        "**通话记录：**\n"
        "AI：今晚八点半一起跑步，您方便吗？\n"
        "客户：可以。"
    )


def test_result_markdown_ignores_model_summary_and_preserves_exact_transcript() -> None:
    message = delivery.format_result_message(
        {
            "invitation_content": "周末带果果来北京玩",
            "results": [
                {
                    "status": "completed",
                    "phone": "+8618036691828",
                    "customer": {"name": "李艳美"},
                    "answered_at": "2026-08-06T10:34:06Z",
                    "summary": (
                        "李艳美已同意：李姐您好呀！"
                        "宝祥想邀请您周末带果果来北京玩；"
                        "提示：太好啦！那我跟宝祥说一声。"
                    ),
                    "transcript": [
                        {"role": "assistant", "text": "李姐您好呀！宝祥想邀请您周末带果果来北京玩。"},
                        {"role": "user", "text": "好啊。"},
                        {"role": "assistant", "text": "太好啦！那我跟宝祥说一声。"},
                    ],
                }
            ],
        }
    )

    assert "通话摘要" not in message
    assert "李艳美已同意" not in message
    assert "AI：李姐您好呀！宝祥想邀请您周末带果果来北京玩。" in message
    assert "客户：好啊。" in message
    assert "AI：太好啦！那我跟宝祥说一声。" in message


def test_sip_decline_is_displayed_as_rejected() -> None:
    message = delivery.format_result_message(
        {
            "invitation_content": "今晚聚餐",
            "results": [
                {
                    "status": "busy",
                    "failure_code": "sip_603",
                    "phone": "+8613800000000",
                    "customer": {"name": "任总"},
                }
            ],
        }
    )

    assert "**通话状态：** 拒接" in message
    assert "**通话记录：**" in message
    assert "通话摘要" not in message


def test_sip_500_has_specific_carrier_summary() -> None:
    message = delivery.format_result_message(
        {
            "invitation_content": "今晚吃饭",
            "results": [
                {
                    "status": "failed",
                    "failure_code": "sip_500",
                    "phone": "+8618701538360",
                    "customer": {"name": "晓旭老师"},
                }
            ],
        }
    )

    assert "**通话状态：** 未接通" in message
    assert "**通话记录：**" in message
    assert "通话摘要" not in message


def test_result_card_uses_blocked_outcome_when_no_call_was_created() -> None:
    status = {
        "status": "completed",
        "contact_count": 1,
        "blocked_count": 1,
        "results": [
            {
                "status": "blocked",
                "failure_code": "consent_missing_or_inactive",
            }
        ],
    }

    assert result_card.result_outcome(status) == "blocked"
    assert "电话未拨出" in result_card._summary(status["results"][0])
    message = delivery.format_result_message(status)
    assert "**通话状态：** 未接通" in message
    assert "**通话记录：**" in message
    assert "通话摘要" not in message


def test_result_transcript_is_not_truncated() -> None:
    long_text = "完整原话" * 1000
    message = delivery.format_result_message(
        {
            "invitation_content": "测试完整记录",
            "results": [
                {
                    "status": "completed",
                    "answered_at": "2026-08-07T04:00:00Z",
                    "transcript": [
                        {"role": "assistant", "text": long_text},
                        {"role": "user", "text": "收到。"},
                    ],
                }
            ],
        }
    )

    assert long_text in message
    assert message.endswith("客户：收到。")


def test_result_delivery_sends_markdown_text_without_media(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_send(args):
        captured["args"] = args
        return json.dumps({"success": True})

    monkeypatch.setattr(delivery, "_send_via_hermes", fake_send)

    markdown = "**收信人：** 常凤香"
    assert delivery._send_message(markdown) is True
    assert captured["args"] == {
        "action": "send",
        "target": "weixin",
        "message": markdown,
    }
    assert not markdown.startswith("MEDIA:")


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
