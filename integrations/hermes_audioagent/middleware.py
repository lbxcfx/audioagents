"""Hermes request isolation for direct WeChat outbound-call commands."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1\d{10}(?!\d)")
_CALL_INTENT_PATTERN = re.compile(
    r"(?:打(?:个)?电话|拨打|拨号|外呼|致电|电话联系|电话沟通|联系.*电话)"
)
_DIRECT_EXECUTION_DIRECTIVE = """[本轮外呼执行规则（最高优先级）]
这是一个独立的微信外呼任务，不得参考或延续任何历史对话、历史偏好、历史 Prompt 或历史确认流程。
仅使用本条微信消息中的号码和业务信息生成 Prompt；缺失信息直接省略，不追问、不预览、不要求确认。
身份固定为“我是李宝祥的智能助理”。
立即通过 tool_describe 加载 audioagent_submit_outbound_task，再通过 tool_call 下发；不得等待用户再次回复。
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
    return bool(
        _PHONE_PATTERN.search(compact) and _CALL_INTENT_PATTERN.search(compact)
    )


def _with_directive(message: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(message)
    content = updated.get("content")
    if isinstance(content, str):
        updated["content"] = content.rstrip() + "\n\n" + _DIRECT_EXECUTION_DIRECTIVE
    elif isinstance(content, list):
        updated["content"] = list(content) + [
            {"type": "text", "text": _DIRECT_EXECUTION_DIRECTIVE}
        ]
    return updated


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

    system_messages = [
        deepcopy(item)
        for item in messages[:current_user_index]
        if isinstance(item, dict) and str(item.get("role") or "") == "system"
    ]
    current_turn = [deepcopy(item) for item in messages[current_user_index:]]
    current_turn[0] = _with_directive(current_turn[0])
    updated_request = deepcopy(request)
    updated_request[field] = system_messages + current_turn
    return {
        "request": updated_request,
        "source": "audioagent_wechat_outbound_isolation",
    }
