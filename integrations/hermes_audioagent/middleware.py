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
Prompt 必须规定第一句为“您好，我是李宝祥的智能助理，请问您是{{customer_name}}吗？”，客户回应后再说事情。
下发工具时，customers 中每位客户的 name 必须填写本条微信提供的真实称呼（如“李总”）；不得固定写某个人名，也不得把星号或占位符作为 name。
下发工具时必须填写 invitation_content，用一句简洁短语概括事项、时间和地点（如“今晚 8:30 莲花河跑步”），不要包含收信人或发起人姓名。
全程使用热情、自然、口语化的真人助理口吻，适当使用“好的呀、明白了、没问题”等语气词，避免冷漠机械。
客户明确答复后，Prompt 必须要求立即调用 save_call_result 保存业务结论，再调用 end_call；摘要写清客户已同意、未同意或待确认的具体事项和必要提示，不得写沉默计时或系统挂机原因。
Prompt 控制在 1500 个汉字以内，使用简洁分段规则，不嵌套复杂引号或重复大段示例，降低工具参数 JSON 损坏概率。
立即通过 tool_describe 加载 audioagent_submit_outbound_task，再通过 tool_call 下发；不得等待用户再次回复。
工具返回后必须检查 ok、queued_count 和 blocked_count；只有 ok=true 且 queued_count>0 才能说“正在拨打”。若 ok=false 或 queued_count=0，必须明确告诉用户电话未拨出及返回原因。
任务提交回复只发送简洁文字，不生成、引用或发送图片和其他附件；严禁输出 MEDIA: 指令、本地文件路径或 [Sent image attachment] 等附件占位文字。
通话结果由 AudioAgent 结果转发器另行发送 Markdown 文字，不要预测、模拟或重复结果消息。
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
