"""Hermes request guidance for WeChat AudioAgent commands."""

from __future__ import annotations

from copy import deepcopy
import logging
import re
import time
from types import SimpleNamespace
from typing import Any

from . import address_book, response_policy, schemas


logger = logging.getLogger("hermes.plugins.audioagent.middleware")


_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1\d{10}(?!\d)")
_CALL_INTENT_PATTERN = re.compile(
    r"(?:打(?:个)?电话|拨打|拨号|外呼|致电|电话联系|电话沟通|联系.*电话)"
)
_RESULT_CARD_PATTERN = re.compile(
    r"(?:^|\n)\s*\*{0,2}(?:通话状态|通话摘要|通话记录)\*{0,2}\s*[:：]",
    re.M,
)
_DIALING_ACK_PATTERN = re.compile(r"^\s*(?:正在拨打|拨号中)")
_OUT_OF_BAND_PATTERN = re.compile(r"\[\s*out[\s-]*of[\s-]*band\b", re.I)
_TRANSCRIPT_ARTIFACT_PATTERN = re.compile(
    r"(?:聊天|对话|通话).{0,8}(?:记录|文字|文本|转写)|"
    r"(?:记录|文字|文本|转写).{0,8}(?:聊天|对话|通话)"
)
_RECORDING_ARTIFACT_PATTERN = re.compile(
    r"(?:通话|电话).{0,6}(?:录音|音频)|(?:录音|音频).{0,6}(?:通话|电话)|"
    r"(?:发送|发出|发来|上传|导出|给我).{0,8}(?:录音|音频)"
)
_DIRECT_TASK_SYSTEM_PROMPT = """你是微信外呼任务生成器，只处理下面这一条用户消息，禁止参考任何历史对话或补造信息。
必须通过唯一可用的 tool_call 调用 audioagent_submit_outbound_task，不能输出普通文本；tool_call.name 固定为 audioagent_submit_outbound_task。工具参数要求：
1. customers 只使用本条消息明确给出的电话号码和称呼；不得猜测号码。
2. invitation_content 用一句短语概括事项、时间、地点，不含收信人和发起人姓名。
3. prompt 是给电话 AI 的完整指令，1500 个汉字以内；身份固定为李宝祥的智能助理，第一句固定为“您好，我是李宝祥的智能助理，请问您是{{customer_name}}吗？”，客户确认身份后再说明事情。
4. prompt 只使用用户提供的事实，语气热情自然；任何一句最多说一次，禁止复述或循环。
5. 客户明确答复或说“再见”后，立即调用 save_call_result 保存具体业务结论，再调用 end_call；不得继续确认，也不得把沉默计时或系统挂机写成业务结论。
不要预览，不要追问，不要预测通话状态或结果。"""

_ADDRESS_BOOK_TASK_SYSTEM_PROMPT = """你是微信通讯录外呼任务生成器，只处理下面这一条用户消息，禁止参考任何历史对话或猜测电话号码。
必须通过唯一可用的 tool_call 调用 audioagent_resolve_outbound_contact，不能输出普通文本；tool_call.name 固定为 audioagent_resolve_outbound_contact。query 使用消息中的联系人称呼；工具会在数据库中确定电话号码以及是否需要用户确认。
task_name 和 invitation_content 简洁概括当前事项。prompt 是给电话 AI 的完整指令，1500 个汉字以内：身份固定为李宝祥的智能助理，第一句固定为“您好，我是李宝祥的智能助理，请问您是{{customer_name}}吗？”，确认身份后再说明事情；只使用当前消息事实；任何一句最多说一次；客户明确答复或说“再见”后立即调用 save_call_result，再调用 end_call。"""

_ADDRESS_BOOK_CONFIRM_SYSTEM_PROMPT = """这是一个独立的通讯录候选确认任务。上下文只包含本次原始拨号指令、通讯录候选回复和用户当前选择，禁止参考其他历史。
用户可能用序号、中文顺序、姓名、电话号码或其他自然说法选择候选。选择明确时，必须通过唯一可用的 tool_call 调用 audioagent_confirm_address_book_contact；tool_call.name 固定为 audioagent_confirm_address_book_contact。选择不明确或用户说候选都不对时，不得调用工具，只回复“请明确选择候选编号、姓名或电话号码。”。不能重新查询、猜测联系人、修改原始拨号事项或生成新任务。"""

_CALL_ARTIFACT_DIRECTIVE = """[本轮 AudioAgent 通话资料发送规则（最高优先级）]
这是微信用户要求立即获取最近通话资料的请求，必须由 Hermes 的 audioagent 插件执行，不得让 Codex、shell 或数据库客户端代替。
{actions}
若用户没有明确说“呼入”，direction 使用 outbound；只有明确要求所有方向时才使用 any。
工具返回 ok=false 时简洁说明 error，不得伪造记录或附件。
聊天文字：直接发送工具返回的 formatted_text，不概括、不改写、不补造客户内容。
通话录音：最终回复必须把工具返回的 media_directive 原样放在独立一行，不加反引号、不放进代码块、不改成本地路径说明或链接；微信网关会据此上传真实音频附件。
不要只告诉用户文件已准备好；必须在同一轮完成文字回复或 MEDIA 附件交付。
"""

def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in {"text", "input_text"}:
            parts.append(str(item.get("text") or item.get("content") or ""))
    return "\n".join(parts).strip()


def is_wechat_outbound_request(text: str) -> bool:
    compact = str(text or "").strip()
    # A prior result card, an acknowledgement, or a leaked marker is never a
    # new command. This prevents questions quoting old output from redialing.
    if (
        _RESULT_CARD_PATTERN.search(compact)
        or _DIALING_ACK_PATTERN.search(compact)
        or _OUT_OF_BAND_PATTERN.search(compact)
    ):
        return False
    return bool(
        _PHONE_PATTERN.search(compact) and _CALL_INTENT_PATTERN.search(compact)
    )


def _tool_definition(schema: dict[str, Any]) -> dict[str, Any]:
    """Expose one deferred plugin tool through Hermes' executable bridge."""

    target_name = str(schema["name"])
    return {
        "type": "function",
        "function": {
            "name": "tool_call",
            # Keep the target name out of the top-level bridge description.
            # DeepSeek streaming otherwise substitutes it for `tool_call`.
            "description": "调用系统指令中指定的唯一 Hermes 延迟工具。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        # DeepSeek's thinking-mode streaming endpoint promotes
                        # a nested single-value enum to the top-level function
                        # name. A fixed pattern preserves the same constraint
                        # while keeping the emitted function name `tool_call`.
                        "pattern": f"^{re.escape(target_name)}$",
                        "description": f"必须固定为 {target_name}。",
                    },
                    "arguments": deepcopy(schema["parameters"]),
                },
                "required": ["name", "arguments"],
            },
        },
    }


def _with_directive(message: dict[str, Any], *, directive: str) -> dict[str, Any]:
    updated = deepcopy(message)
    content = updated.get("content")
    if isinstance(content, str):
        updated["content"] = content.rstrip() + "\n\n" + directive
    elif isinstance(content, list):
        updated["content"] = list(content) + [{"type": "text", "text": directive}]
    return updated


def _minimal_tool_request(
    request: dict[str, Any],
    *,
    field: str,
    current_turn: list[dict[str, Any]],
    system_prompt: str,
    tool_schema: dict[str, Any],
) -> dict[str, Any]:
    """Build one isolated DeepSeek request with exactly one visible tool."""

    updated = deepcopy(request)
    updated[field] = [
        {"role": "system", "content": system_prompt},
        *deepcopy(current_turn),
    ]
    has_tool_result = any(
        str(item.get("role") or "") == "tool"
        or (
            str(item.get("role") or "") == "assistant"
            and bool(item.get("tool_calls"))
        )
        for item in current_turn[1:]
        if isinstance(item, dict)
    )
    if has_tool_result:
        # The execution middleware returns the tool-backed acknowledgement
        # before another provider request is made.
        updated["tools"] = []
        updated.pop("tool_choice", None)
        updated.pop("parallel_tool_calls", None)
    else:
        updated["tools"] = [_tool_definition(tool_schema)]
        # DeepSeek thinking mode rejects a forced named tool_choice. Keeping
        # only Hermes' executable bridge, narrowed to one plugin target, gives
        # the model an unambiguous route without the incompatible parameter.
        updated.pop("tool_choice", None)
        updated.pop("parallel_tool_calls", None)
        updated["max_tokens"] = min(int(updated.get("max_tokens") or 1800), 1800)
    return updated


def _without_voice_marker(message: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(message)
    content = updated.get("content")
    if isinstance(content, str):
        updated["content"] = content.replace(address_book.VOICE_MARKER, "").strip()
    elif isinstance(content, list):
        cleaned = []
        for item in content:
            copied = deepcopy(item)
            if isinstance(copied, dict) and copied.get("type") in {"text", "input_text"}:
                field = "text" if "text" in copied else "content"
                copied[field] = str(copied.get(field) or "").replace(
                    address_book.VOICE_MARKER, ""
                ).strip()
            cleaned.append(copied)
        updated["content"] = cleaned
    return updated


def guide_wechat_address_book_request(**kwargs: Any) -> dict[str, Any] | None:
    """Route no-phone calls and candidate confirmations through Hermes tools."""

    if str(kwargs.get("platform") or "").strip().lower() != "weixin":
        return None
    request = kwargs.get("request")
    if not isinstance(request, dict):
        return None
    field = "messages" if isinstance(request.get("messages"), list) else "input"
    messages = request.get(field)
    if not isinstance(messages, list):
        return None
    current_user_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if isinstance(item, dict) and str(item.get("role") or "") == "user":
            current_user_index = index
            break
    if current_user_index is None:
        return None

    current = messages[current_user_index]
    text = _message_text(current.get("content"))
    is_voice = address_book.VOICE_MARKER in text
    clean_text = text.replace(address_book.VOICE_MARKER, "").strip()
    session_id = kwargs.get("session_id") or kwargs.get("task_id")
    pending_task = address_book.pending(session_id)
    system_prompt = ""
    tool_schema: dict[str, Any] | None = None
    source = ""
    if pending_task is not None:
        # A pending address-book choice defines a small task-local context.
        # Route every follow-up through it so natural selection wording never
        # falls back to the full Hermes conversation. Deterministic forms are
        # recorded here; other clear forms can supply `choice` through the
        # tool schema, while ambiguous replies are explicitly not executed.
        address_book.note_confirmation(session_id, clean_text)
        system_prompt = _ADDRESS_BOOK_CONFIRM_SYSTEM_PROMPT
        tool_schema = schemas.CONFIRM_ADDRESS_BOOK_CONTACT
        source = "audioagent_wechat_address_book_confirmation"
    elif (
        not _PHONE_PATTERN.search(clean_text)
        and _CALL_INTENT_PATTERN.search(clean_text)
    ):
        query = address_book.extract_query(clean_text)
        if query:
            input_mode = "voice" if is_voice else "text"
            address_book.mark_resolution_context(
                session_id,
                query=query,
                input_mode=input_mode,
                request_text=clean_text,
            )
            system_prompt = _ADDRESS_BOOK_TASK_SYSTEM_PROMPT
            tool_schema = schemas.RESOLVE_OUTBOUND_CONTACT
            source = "audioagent_wechat_address_book_resolution"

    if tool_schema is None and not is_voice:
        return None
    current_turn = [deepcopy(item) for item in messages[current_user_index:]]
    current_turn[0] = _without_voice_marker(current_turn[0])
    if source == "audioagent_wechat_address_book_confirmation":
        pending = pending_task or {}
        scoped_turn: list[dict[str, Any]] = []
        original_request = str(pending.get("request_text") or "").strip()
        candidate_response = str(pending.get("user_response") or "").strip()
        if original_request:
            scoped_turn.append({"role": "user", "content": original_request})
        if candidate_response:
            scoped_turn.append({"role": "assistant", "content": candidate_response})
        scoped_turn.append(current_turn[0])
        current_turn = scoped_turn
    if tool_schema is not None:
        updated_request = _minimal_tool_request(
            request,
            field=field,
            current_turn=current_turn,
            system_prompt=system_prompt,
            tool_schema=tool_schema,
        )
        return {"request": updated_request, "source": source}
    else:
        # Voice commands that already contain a phone still use the existing
        # direct-submit path; only remove the private transport marker.
        updated_messages = deepcopy(messages)
        updated_messages[current_user_index] = current_turn[0]
        source = "audioagent_wechat_voice_marker_removed"
    updated_request = deepcopy(request)
    updated_request[field] = updated_messages
    return {"request": updated_request, "source": source}


def isolate_wechat_outbound_request(**kwargs: Any) -> dict[str, Any] | None:
    """Send DeepSeek only the current outbound request and its in-turn results."""
    if str(kwargs.get("platform") or "").strip().lower() != "weixin":
        return None
    request = kwargs.get("request")
    if not isinstance(request, dict):
        return None
    field = "messages" if isinstance(request.get("messages"), list) else "input"
    messages = request.get(field)
    if not isinstance(messages, list):
        return None

    current_user_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if isinstance(item, dict) and str(item.get("role") or "") == "user":
            current_user_index = index
            break
    if current_user_index is None:
        return None
    current = messages[current_user_index]
    if not is_wechat_outbound_request(_message_text(current.get("content"))):
        return None

    current_turn = [deepcopy(item) for item in messages[current_user_index:]]
    current_turn[0] = _without_voice_marker(current_turn[0])
    updated_request = _minimal_tool_request(
        request,
        field=field,
        current_turn=current_turn,
        system_prompt=_DIRECT_TASK_SYSTEM_PROMPT,
        tool_schema=schemas.SUBMIT_OUTBOUND_TASK,
    )
    return {
        "request": updated_request,
        "source": "audioagent_wechat_outbound_isolation",
    }


def _synthetic_chat_response(text: str, model: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id="audioagent-deterministic-response",
        object="chat.completion",
        created=int(time.time()),
        model=str(model or "audioagent"),
        choices=[
            SimpleNamespace(
                index=0,
                finish_reason="stop",
                message=SimpleNamespace(
                    role="assistant",
                    content=text,
                    tool_calls=None,
                    function_call=None,
                    reasoning_content=None,
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        ),
    )


def return_tool_backed_wechat_response(**kwargs: Any) -> Any:
    """Skip the post-tool DeepSeek call and return the deterministic result."""

    next_call = kwargs["next_call"]
    if str(kwargs.get("platform") or "").strip().lower() != "weixin":
        return next_call(kwargs["request"])
    request = kwargs.get("request")
    if not isinstance(request, dict):
        return next_call(request)
    messages = request.get("messages") or request.get("input")
    if not isinstance(messages, list) or not any(
        isinstance(item, dict) and str(item.get("role") or "") == "tool"
        for item in messages
    ):
        return next_call(request)
    response = response_policy.pending_user_response(kwargs.get("session_id"))
    if response is None:
        # Never make a second provider call with a failed fast-path tool
        # result. Besides adding latency, models can leak their internal tool
        # syntax as plain chat. No stored response means no call was accepted.
        system_text = next(
            (
                _message_text(item.get("content"))
                for item in messages
                if isinstance(item, dict) and item.get("role") == "system"
            ),
            "",
        )
        if system_text in {
            _DIRECT_TASK_SYSTEM_PROMPT,
            _ADDRESS_BOOK_TASK_SYSTEM_PROMPT,
            _ADDRESS_BOOK_CONFIRM_SYSTEM_PROMPT,
        }:
            tool_content = next(
                (
                    _message_text(item.get("content"))
                    for item in reversed(messages)
                    if isinstance(item, dict) and item.get("role") == "tool"
                ),
                "",
            )
            logger.warning(
                "AudioAgent fast-path tool produced no verified response: %s",
                tool_content[:1000] or "empty tool result",
            )
            return _synthetic_chat_response(
                "电话未拨出：外呼工具执行失败。", kwargs.get("model")
            )
        return next_call(request)
    return _synthetic_chat_response(response, kwargs.get("model"))


def _artifact_actions(text: str) -> list[str]:
    actions: list[str] = []
    if _TRANSCRIPT_ARTIFACT_PATTERN.search(text):
        actions.append(
            "先调用 skill_view 加载 audioagent:latest-call-transcript，再通过 "
            "tool_describe 和 tool_call 调用 audioagent_get_latest_call_transcript。"
        )
    if _RECORDING_ARTIFACT_PATTERN.search(text):
        actions.append(
            "先调用 skill_view 加载 audioagent:latest-call-recording，再通过 "
            "tool_describe 和 tool_call 调用 audioagent_get_latest_call_recording。"
        )
    return actions


def guide_wechat_call_artifact_request(**kwargs: Any) -> dict[str, Any] | None:
    """Force Weixin transcript/recording requests through the Hermes plugin."""

    if str(kwargs.get("platform") or "").strip().lower() != "weixin":
        return None
    request = kwargs.get("request")
    if not isinstance(request, dict):
        return None
    field = "messages" if isinstance(request.get("messages"), list) else "input"
    messages = request.get(field)
    if not isinstance(messages, list):
        return None

    current_user_index: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if isinstance(item, dict) and str(item.get("role") or "") == "user":
            current_user_index = index
            break
    if current_user_index is None:
        return None
    current = messages[current_user_index]
    actions = _artifact_actions(_message_text(current.get("content")))
    if not actions:
        return None

    directive = _CALL_ARTIFACT_DIRECTIVE.format(
        actions="\n".join(f"- {action}" for action in actions)
    )
    updated_request = deepcopy(request)
    updated_messages = [deepcopy(item) for item in messages]
    updated_messages[current_user_index] = _with_directive(
        updated_messages[current_user_index], directive=directive
    )
    updated_request[field] = updated_messages
    return {
        "request": updated_request,
        "source": "audioagent_wechat_call_artifact_guidance",
    }
