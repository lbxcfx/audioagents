"""Hermes request guidance for WeChat AudioAgent commands."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from . import address_book


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
工具返回后不自行组织回复；Hermes 插件会根据工具返回值强制生成确定性回执。成功时只能显示“拨号中...”，失败时只显示工具返回的未拨出原因。
任务提交回复只发送简洁文字，不生成、引用或发送图片和其他附件；严禁输出 MEDIA: 指令、本地文件路径或 [Sent image attachment] 等附件占位文字。
通话结果由 AudioAgent 结果转发器另行发送 Markdown 文字，不要预测、模拟或重复结果消息。
"""

_CALL_ARTIFACT_DIRECTIVE = """[本轮 AudioAgent 通话资料发送规则（最高优先级）]
这是微信用户要求立即获取最近通话资料的请求，必须由 Hermes 的 audioagent 插件执行，不得让 Codex、shell 或数据库客户端代替。
{actions}
若用户没有明确说“呼入”，direction 使用 outbound；只有明确要求所有方向时才使用 any。
工具返回 ok=false 时简洁说明 error，不得伪造记录或附件。
聊天文字：直接发送工具返回的 formatted_text，不概括、不改写、不补造客户内容。
通话录音：最终回复必须把工具返回的 media_directive 原样放在独立一行，不加反引号、不放进代码块、不改成本地路径说明或链接；微信网关会据此上传真实音频附件。
不要只告诉用户文件已准备好；必须在同一轮完成文字回复或 MEDIA 附件交付。
"""

_ADDRESS_BOOK_RESOLVE_DIRECTIVE = """[本轮通讯录外呼规则（最高优先级）]
本条微信消息要求拨打联系人，但没有提供电话号码。必须由 Hermes 的 audioagent 插件查询通讯录，严禁猜测、补全或编造电话号码。
联系人查询词已经由 Hermes 确定为“{query}”，输入方式为 {input_mode}；工具侧会再次使用该确定值，不接受模型改写。
仅使用本条消息生成简洁的 task_name、invitation_content 和完整 prompt；身份、开场、业务结果保存及结束通话规则与直接外呼一致。
立即通过 tool_describe 加载 audioagent_resolve_outbound_contact，再通过 tool_call 调用；不要调用 audioagent_submit_outbound_task，也不要向用户自行展示候选或编写回执。
工具会执行以下确定性策略：文字消息唯一精确命中全称、简称、全拼或简拼时立即拨号；模糊匹配、歧义匹配及所有微信语音匹配都先确认；无候选时索要姓名和电话号码。
工具返回后不要自行组织回复；Hermes 插件会强制发送工具生成的确定性文字。
"""

_ADDRESS_BOOK_CONFIRM_DIRECTIVE = """[本轮通讯录确认规则（最高优先级）]
用户正在确认上一轮由 Hermes audioagent 插件返回的通讯录候选。
立即通过 tool_describe 加载 audioagent_confirm_address_book_contact，再通过 tool_call 调用；不得重新查询、不得重新生成任务、不得猜测姓名或电话号码。
工具返回后不要自行组织回复；Hermes 插件会强制发送工具生成的确定性文字。
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


def _with_directive(
    message: dict[str, Any], *, directive: str = _DIRECT_EXECUTION_DIRECTIVE
) -> dict[str, Any]:
    updated = deepcopy(message)
    content = updated.get("content")
    if isinstance(content, str):
        updated["content"] = content.rstrip() + "\n\n" + directive
    elif isinstance(content, list):
        updated["content"] = list(content) + [
            {"type": "text", "text": directive}
        ]
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
    directive = ""
    source = ""
    if address_book.note_confirmation(session_id, clean_text):
        directive = _ADDRESS_BOOK_CONFIRM_DIRECTIVE
        source = "audioagent_wechat_address_book_confirmation"
    elif (
        not _PHONE_PATTERN.search(clean_text)
        and _CALL_INTENT_PATTERN.search(clean_text)
    ):
        query = address_book.extract_query(clean_text)
        if query:
            input_mode = "voice" if is_voice else "text"
            address_book.mark_resolution_context(
                session_id, query=query, input_mode=input_mode
            )
            directive = _ADDRESS_BOOK_RESOLVE_DIRECTIVE.format(
                query=query,
                input_mode="微信语音" if is_voice else "文字消息",
            )
            source = "audioagent_wechat_address_book_resolution"

    if not directive and not is_voice:
        return None
    system_messages = [
        deepcopy(item)
        for item in messages[:current_user_index]
        if isinstance(item, dict) and str(item.get("role") or "") == "system"
    ]
    current_turn = [deepcopy(item) for item in messages[current_user_index:]]
    current_turn[0] = _without_voice_marker(current_turn[0])
    if directive:
        current_turn[0] = _with_directive(current_turn[0], directive=directive)
        updated_messages = system_messages + current_turn
    else:
        # Voice commands that already contain a phone still use the existing
        # direct-submit path; only remove the private transport marker.
        updated_messages = [deepcopy(item) for item in messages]
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
