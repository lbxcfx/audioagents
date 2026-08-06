from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Iterator, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from livekit import rtc
from livekit.agents import llm, utils
from livekit.plugins import openai
from livekit.plugins.openai.realtime.realtime_model import RealtimeSession
from openai.types.realtime.realtime_audio_input_turn_detection import ServerVad
from pydantic import BaseModel


PIPELINE_ENV = "QWEN_VOICE_PIPELINE"
CLASSIC_PIPELINE = "classic"
REALTIME_PIPELINE = "realtime"
SUPPORTED_PIPELINES = {CLASSIC_PIPELINE, REALTIME_PIPELINE}

DEFAULT_REALTIME_URL = (
    "wss://llm-vfnjvqxp5829jfc6.cn-beijing.maas.aliyuncs.com/"
    "api-ws/v1/realtime"
)
DEFAULT_REALTIME_MODEL = "qwen-audio-3.0-realtime-flash"
DEFAULT_REALTIME_VOICE = "longanqian"
QWEN_INPUT_SAMPLE_RATE = 16000
DEFAULT_REALTIME_OPENINGS = (
    "您好，我是脉脉招聘专员，有个骑手职位机会分享给您，请问您现在方便吗？",
    "您好，我是脉脉招聘专员，有个骑手岗位想和您聊聊，请问现在方便吗？",
    "您好，这里是脉脉招聘，有个骑手职位想给您介绍一下，您现在方便吗？",
    "您好，脉脉招聘这边有个骑手机会，您这会儿方便聊两句吗？",
)


def _scene_identity(scene: dict[str, Any] | None) -> str:
    if not scene:
        return "企业语音 AI 服务的中文电话客服助手"
    ui = scene.get("ui") if isinstance(scene.get("ui"), dict) else {}
    explicit = str(
        ui.get("agent_identity")
        or ui.get("identity")
        or ui.get("role_prompt")
        or ""
    ).strip()
    if explicit:
        return explicit
    parts = [str(scene.get("name") or "").strip()]
    industry = str(scene.get("industry") or "").strip()
    business_type = str(scene.get("business_type") or "").strip()
    if industry:
        parts.append(f"所属行业：{industry}")
    if business_type:
        parts.append(f"业务类型：{business_type}")
    description = "；".join(part for part in parts if part)
    return f"负责“{description}”话术的中文电话客服助手" if description else "中文电话客服助手"


def _scene_prompt_parts(scene: dict[str, Any] | None) -> tuple[str, str, str, str]:
    """Compile the front-end scene graph into prompt-readable dynamic rules."""
    if not scene:
        return (
            "脉脉公司负责骑手招聘的招聘专员",
            DEFAULT_REALTIME_OPENINGS[0],
            """- 入口节点：rider_opening
- 核心目标：先确认客户是否方便沟通，再了解在职状态；根据在职或离职分支介绍美团骑手机会，确认意向区域和入职时间，最后完成微信跟进或预约回访。
- 主动开场：电话一接通就主动说开场白，绝对不要等待客户先说话。每通电话必须从 rider_opening 的 4 种等义话术中自然选择一句，避免每次固定使用同一句；必须同时包含“脉脉招聘身份、骑手职位机会、现在是否方便”三个信息点。
- 沟通方式：开场白可以超过 20 个汉字；其余每次表达尽量不超过 25 个汉字，一次只问一个问题。根据用户已经提供的信息跳过重复问题，不得连续盘问。
- 真人感：先自然承接用户上一句话，再推进问题。可按语境少量使用“嗯、好、行、对、那、这样啊”等口语词，但要变化且克制；不得每轮固定说“好的”，不得使用客服播报腔。
- 禁止问题：不得询问全职或兼职偏好；不得询问“最关心收入、时间还是配送距离”；不得在客户回答没有配送经验之前主动说“没做过也没关系”。
- 最大有效对话轮数：14
- 默认未识别路由：当前节点对应的 clarify 节点

节点 rider_opening｜主动招聘开场｜类型：scene
  开场话术四选一：
  1. 您好，我是脉脉招聘专员，有个骑手职位机会分享给您，请问您现在方便吗？
  2. 您好，我是脉脉招聘专员，有个骑手岗位想和您聊聊，请问现在方便吗？
  3. 您好，这里是脉脉招聘，有个骑手职位想给您介绍一下，您现在方便吗？
  4. 您好，脉脉招聘这边有个骑手机会，您这会儿方便聊两句吗？
  选择规则：每通只选择一句，不拼接多句，避免连续通话固定使用同一句；不得省略身份、骑手职位或方便性确认；不得使用“智能助手”等身份。
  分支路由：方便或愿意听 -> rider_employment；询问什么职位 -> rider_opening_qa；暂时不便 -> rider_callback；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify_opening
  前端配置的意图示例：有意向=[可以, 方便, 你说, 想了解]；暂时不便=[现在忙, 不方便, 晚点]；明确拒绝=[不需要, 没兴趣, 别打了]

节点 rider_opening_qa｜开场岗位说明｜类型：scene
  对客话术：是美团骑手岗位。您现在方便了解吗？
  分支路由：方便或愿意听 -> rider_employment；暂时不便 -> rider_callback；明确拒绝 -> rider_end_reject；继续提问 -> rider_qa；未识别 -> rider_clarify_opening

节点 rider_employment｜确认当前状态｜类型：scene
  对客话术：您现在是在职，还是已经离职？
  分支路由：在职 -> rider_employed_interest；已经离职或待业 -> rider_unemployed_pitch；状态不清楚 -> rider_clarify_employment；询问岗位 -> rider_qa；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify_employment

节点 rider_employed_interest｜在职客户确认兴趣｜类型：scene
  对客话术：美团骑手机会收入比较高，您有兴趣了解吗？
  分支路由：有兴趣 -> rider_area；没兴趣 -> rider_end_reject；暂时不便 -> rider_callback；继续提问 -> rider_qa；未识别 -> rider_clarify_interest

节点 rider_unemployed_pitch｜离职客户推荐岗位｜类型：scene
  对客话术：这边有个美团骑手机会。您想在哪个区域工作？
  分支路由：提供区域 -> rider_start_time；区域未定 -> rider_area；暂时不便 -> rider_callback；继续提问 -> rider_qa；明确拒绝 -> rider_end_reject；未识别 -> rider_area

节点 rider_area｜确认意向地点｜类型：scene
  对客话术：您想在哪个城市或区域工作？
  分支路由：提供区域 -> rider_start_time；暂未确定 -> rider_start_time；询问岗位 -> rider_qa；暂时不便 -> rider_callback；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify_area

节点 rider_start_time｜了解到岗时间｜类型：scene
  对客话术：您大概什么时候可以入职？
  分支路由：提供时间 -> rider_wechat_request；暂未确定 -> rider_wechat_request；询问问题 -> rider_qa；暂时不便 -> rider_callback；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify_start_time

节点 rider_qa｜岗位问答｜类型：llm_fallback
  对客话术：只回答客户当前问题；资料不足时说“这个要结合您所在区域确认”。回答后回到被打断前尚未完成的节点，不得自行增加经验、车辆、兼职偏好或泛泛需求问题。
  分支路由：继续沟通 -> 返回原节点；愿意留微信 -> rider_wechat_phone_check；要求人工 -> rider_transfer；拒绝 -> rider_end_reject；未识别 -> 返回原节点

节点 rider_wechat_request｜提出添加微信｜类型：scene
  对客话术：方便后续给您分享机会，可以加您微信吗？
  分支路由：同意 -> rider_wechat_phone_check；主动提供微信号 -> rider_wechat_text；不同意 -> rider_phone_followup；继续提问 -> rider_qa；未识别 -> rider_clarify_wechat_request

节点 rider_wechat_phone_check｜确认手机号微信｜类型：scene
  对客话术：您这个手机号是微信号吗？
  分支路由：是手机号微信 -> rider_wechat_add；不是 -> rider_wechat_text；拒绝提供 -> rider_contact_alternative；继续提问 -> rider_qa；未识别 -> rider_clarify_wechat

节点 rider_wechat_add｜添加手机号微信｜类型：scene
  动作要求：手机号确认为微信号后进入 rider_save；客户更换微信号时进入 rider_wechat_text。
  分支路由：手机号微信已确认 -> rider_save；客户更换微信号 -> rider_wechat_text；拒绝 -> rider_end_neutral

节点 rider_wechat_retry｜号码澄清｜类型：scene
  对客话术：号码我没确认完整，麻烦您从头慢慢说一遍。
  分支路由：重新报号 -> rider_wechat_text；拒绝 -> rider_contact_alternative

节点 rider_wechat_text｜收集非手机号微信｜类型：scene
  对客话术：请告诉我您的微信号，我会认真听。
  字符规则：用户可能分段说字母、数字或中文；用户没有明确说完前不得抢话、猜测或补全。
  分支路由：提供完整微信号 -> rider_wechat_text_confirm；未说完 -> rider_wechat_text；拒绝 -> rider_contact_alternative

节点 rider_wechat_text_confirm｜确认文字微信｜类型：scene
  对客话术：简短复述微信号，并询问是否正确。
  分支路由：确认正确 -> rider_save；有误 -> rider_wechat_text；拒绝保存 -> rider_end_reject

节点 rider_save｜登记结果｜类型：scene
  对客话术：好的，已经加您了，请您通过一下。
  动作要求：本轮只调用 complete_wechat_followup，不得在同一轮调用 end_call；摘要只包含在职状态、意向区域、预计入职时间、回访时间和已确认微信；未获得的信息写“未提供”，不得编造。程序会立即播放上述对客话术，模型不得自行播报或重复播报。
  收尾等待：说完后最多等待客户 3 秒。客户回答“好的”“好”“行”“知道了”“会通过”等确认语时，立即说“祝您生活愉快，再见”；3 秒内没有任何回复时，也立即说“祝您生活愉快，再见”。客户在 3 秒内提出其他问题时，先处理问题，不得强行结束。
  分支路由：客户确认 -> rider_end_success；3 秒无回复 -> rider_end_success；客户提问 -> rider_qa

节点 rider_save_pending｜登记待添加微信｜类型：scene
  对客话术：稍后招聘专员会添加您，请注意通过。
  动作要求：调用 save_call_result，注明手机号是微信号但尚未确认添加成功。
  分支路由：保存成功 -> rider_end_success；保存失败 -> rider_end_manual

节点 rider_contact_alternative｜尊重隐私｜类型：scene
  对客话术：没关系，您什么时候方便，我再给您电话？
  分支路由：改为提供微信 -> rider_wechat_phone_check；提供时间 -> rider_end_callback；不希望联系 -> rider_end_reject；未识别 -> rider_clarify_phone_followup

节点 rider_phone_followup｜微信拒绝后提出电话回访｜类型：scene
  对客话术：没关系，您什么时候方便，我再给您电话？
  分支路由：提供时间 -> rider_end_callback；改为同意加微信 -> rider_wechat_phone_check；不希望联系 -> rider_end_reject；未识别 -> rider_clarify_phone_followup

节点 rider_callback｜稍后联系｜类型：scene
  对客话术：您什么时候方便，我再联系您？
  分支路由：提供时间 -> rider_callback_wechat_request；不希望联系 -> rider_end_reject；未识别 -> rider_clarify_callback

节点 rider_callback_wechat_request｜预约后提出添加微信｜类型：scene
  对客话术：方便后续给您分享机会，可以加您微信吗？
  分支路由：同意 -> rider_wechat_phone_check；主动提供微信号 -> rider_wechat_text；不同意 -> rider_end_callback；未识别 -> rider_clarify_wechat_request

节点 rider_clarify_opening｜澄清是否方便｜类型：scene
  对客话术：请问您现在方便聊一下吗？
  分支路由：方便 -> rider_employment；不方便 -> rider_callback；拒绝 -> rider_end_reject；未识别 -> rider_clarify_opening

节点 rider_clarify_employment｜澄清在职状态｜类型：scene
  对客话术：请问您现在是在职，还是已离职？
  分支路由：在职 -> rider_employed_interest；已离职 -> rider_unemployed_pitch；拒绝 -> rider_end_reject；未识别 -> rider_clarify_employment

节点 rider_clarify_interest｜澄清岗位兴趣｜类型：scene
  对客话术：您愿意了解这个美团骑手机会吗？
  分支路由：愿意 -> rider_area；不愿意 -> rider_end_reject；不方便 -> rider_callback；未识别 -> rider_clarify_interest

节点 rider_clarify_area｜澄清工作地点｜类型：scene
  对客话术：您希望在哪个区域工作？
  分支路由：提供区域 -> rider_start_time；暂未确定 -> rider_start_time；拒绝 -> rider_end_reject；未识别 -> rider_clarify_area

节点 rider_clarify_start_time｜澄清入职时间｜类型：scene
  对客话术：您预计什么时候可以入职？
  分支路由：提供时间 -> rider_wechat_request；暂未确定 -> rider_wechat_request；拒绝 -> rider_end_reject；未识别 -> rider_clarify_start_time

节点 rider_clarify_callback｜澄清回访时间｜类型：scene
  对客话术：您希望我哪天、什么时间联系？
  分支路由：提供时间 -> rider_callback_wechat_request；拒绝 -> rider_end_reject；未识别 -> rider_clarify_callback

节点 rider_clarify_phone_followup｜澄清电话回访时间｜类型：scene
  对客话术：您方便我什么时候再打电话？
  分支路由：提供时间 -> rider_end_callback；改为同意加微信 -> rider_wechat_phone_check；拒绝 -> rider_end_reject；未识别 -> rider_clarify_phone_followup

节点 rider_clarify_wechat_request｜澄清添加微信意愿｜类型：scene
  对客话术：可以加您微信，后续分享机会吗？
  分支路由：同意 -> rider_wechat_phone_check；主动提供微信号 -> rider_wechat_text；不同意且已预约 -> rider_end_callback；不同意且未预约 -> rider_phone_followup；未识别 -> rider_clarify_wechat_request

节点 rider_clarify_wechat｜确认微信意愿｜类型：scene
  对客话术：请问这个手机号是您的微信号吗？
  分支路由：是 -> rider_wechat_add；不是 -> rider_wechat_text；拒绝 -> rider_contact_alternative；未识别 -> rider_clarify_wechat

节点 rider_transfer｜转招聘人员｜类型：scene
  对客话术：好的，我为您联系招聘人员进一步确认，请稍候。
  分支路由：工具成功 -> rider_end_transfer；工具失败 -> rider_end_manual

节点 rider_end_success｜意向登记结束｜类型：end
  对客话术：祝您生活愉快，再见。

节点 rider_end_callback｜预约回访结束｜类型：end
  对客话术：祝您生活愉快，再见。

节点 rider_end_reject｜拒绝结束｜类型：end
  对客话术：祝您生活愉快，再见。

节点 rider_end_neutral｜未留微信结束｜类型：end
  对客话术：祝您生活愉快，再见。

节点 rider_end_transfer｜转接结束｜类型：end
  对客话术：已为您转接招聘人员，请稍候。祝您生活愉快，再见。

节点 rider_end_manual｜人工联系失败｜类型：end
  对客话术：暂时无法处理，请稍后再试。祝您生活愉快，再见。

统一收尾规则：进入任意 end 节点时直接调用 end_call，不要自行播报 end 节点话术。程序会停止模型输出、完整播放“祝您生活愉快，再见”，确认播放结束后再挂机。""",
            """- 岗位性质：美团骑手配送岗位，只有全职岗位，不提供兼职或灵活用工选项。
- 工作地点：不同城市和区域的岗位情况可能不同，需要招聘人员结合候选人所在区域确认。
- 工作时间、薪酬、补贴、保险、车辆要求、入职条件和入职时间：当前默认资料未提供，不得给出具体数字或承诺，应交由招聘人员确认。
- 招聘沟通目标：确认客户是否方便、在职状态、意向区域和预计入职时间；最后确认当前手机号是否为微信号，或收集其他微信号。
- 在职客户：先说明美团骑手机会收入比较高，再询问是否有兴趣；不得承诺具体收入。
- 已离职客户：直接介绍美团骑手机会，然后询问意向地点和入职时间。
- 微信号：先征得添加微信的同意，再确认当前手机号是否为微信号；不是时再收集并复述确认其他微信号。微信号确认后按 rider_save 的固定话术告知客户通过。
- 信息收集：只收集完成招聘跟进所必需的信息；客户不愿提供时不得强迫或反复追问。""",
        )

    flow = scene.get("flow") if isinstance(scene.get("flow"), dict) else {}
    raw_nodes = flow.get("nodes") if isinstance(flow.get("nodes"), list) else []
    nodes = [node for node in raw_nodes if isinstance(node, dict) and node.get("id")]
    entry_id = str(flow.get("entry_node") or (nodes[0].get("id") if nodes else "")).strip()
    entry = next((node for node in nodes if str(node.get("id")) == entry_id), None)
    opening = str((entry or {}).get("text") or "").strip()
    if not opening:
        opening = "您好，我是智能客服助手。请问有什么可以帮助您？"

    lines = [
        f"- 入口节点：{entry_id or '未指定'}",
        f"- 最大有效对话轮数：{flow.get('max_turns') or 10}",
        f"- 默认未识别路由：{flow.get('unknown_route') or '由当前节点规则决定'}",
    ]
    for node in nodes:
        node_id = str(node.get("id"))
        node_type = str(node.get("type") or "scene")
        name = str(node.get("name") or node_id)
        speech = str(node.get("text") or "").strip() or "（根据上下文自然回答，不使用固定话术）"
        lines.append(f"\n节点 {node_id}｜{name}｜类型：{node_type}")
        lines.append(f"  对客话术：{speech}")
        routes = node.get("routes") if isinstance(node.get("routes"), dict) else {}
        if routes:
            lines.append("  分支路由：" + "；".join(f"{intent} -> {target}" for intent, target in routes.items()))
        keywords = node.get("intent_keywords") if isinstance(node.get("intent_keywords"), dict) else {}
        if keywords:
            rendered = []
            for intent, values in keywords.items():
                items = values if isinstance(values, list) else [values]
                rendered.append(f"{intent}=[{', '.join(str(item) for item in items if str(item).strip())}]")
            lines.append("  前端配置的意图示例：" + "；".join(rendered))

    raw_knowledge = scene.get("knowledge") if isinstance(scene.get("knowledge"), list) else []
    knowledge_lines = []
    for item in raw_knowledge:
        if not isinstance(item, dict) or item.get("enabled") in {0, False}:
            continue
        title = str(item.get("title") or "未命名问题").strip()
        answer = str(item.get("answer") or "").strip()
        keywords = str(item.get("keywords") or "").strip()
        if answer:
            knowledge_lines.append(f"- {title}（关键词：{keywords or '无'}）：{answer}")

    return (
        _scene_identity(scene),
        opening,
        "\n".join(lines),
        "\n".join(knowledge_lines) or "前端未配置知识库；对资料外问题明确说明不知道，必要时建议转人工。",
    )


class QwenSmartTurn(BaseModel):
    type: Literal["smart_turn"] = "smart_turn"


def voice_pipeline(value: str | None = None) -> Literal["classic", "realtime"]:
    selected = (value if value is not None else os.getenv(PIPELINE_ENV, CLASSIC_PIPELINE))
    selected = selected.strip().lower()
    if selected not in SUPPORTED_PIPELINES:
        choices = ", ".join(sorted(SUPPORTED_PIPELINES))
        raise ValueError(f"{PIPELINE_ENV} must be one of: {choices}")
    return selected  # type: ignore[return-value]


def load_realtime_instructions(
    *,
    root: Path,
    session_id: str,
    scene_id: int | None,
    prompt_override: str = "",
    customer_name: str = "",
    customer_company: str = "",
    customer_phone: str = "",
    customer_profile: str = "",
    scene: dict[str, Any] | None = None,
) -> str:
    task_prompt = prompt_override.strip()
    environment_override = os.getenv("QWEN_AUDIO_REALTIME_INSTRUCTIONS", "").strip()
    if task_prompt:
        prompt = (
            "# 电话沟通硬约束（最高优先级）\n"
            "- 接通后直接进入本任务正题，不播报录音、系统测试等说明。\n"
            "- 不使用其他场景的固定开场，不猜测或重复确认无关身份。\n"
            "- 每次只说一句，每句最多24个汉字（标点不计），然后等待客户。\n"
            "- 一次只表达一个信息点或询问一个问题，禁止长段介绍和连续提问。\n"
            "- 整通电话最多8轮；达到上限时立即结束，不延长对话。\n"
            "- 正常结束时不要自行扩写告别语，调用 end_call；程序固定播放“感谢您的时间，再见。”。\n"
            "- 客户明确拒绝时立即礼貌结束；客户沉默时由程序在3秒后挂机。\n"
            "- 必须根据已提供的真实资料沟通；禁止朗读方括号占位符或编造产品信息。\n\n"
            "# 本次任务\n"
            + task_prompt
        )
    elif environment_override:
        prompt = environment_override
    else:
        configured = os.getenv("QWEN_AUDIO_REALTIME_PROMPT_FILE", "").strip()
        path = (
            Path(configured).expanduser()
            if configured
            else root
            / "qwen-telephony"
            / "docs"
            / "qwen-audio-realtime-dialogue-prompt.md"
        )
        text = path.read_text(encoding="utf-8")
        match = re.search(r"```text\s*\n(.*?)\n```", text, flags=re.DOTALL)
        if not match:
            raise ValueError(f"Realtime prompt file has no ```text block: {path}")
        prompt = match.group(1).strip()

    identity, opening, state_machine, knowledge = _scene_prompt_parts(scene)
    values = {
        "session_id": session_id,
        "scene_id": str(scene_id) if scene_id else "未知",
        "customer_name": customer_name.strip() or "未知",
        "customer_company": customer_company.strip() or "未知",
        "customer_phone": customer_phone.strip() or "未知",
        "customer_profile": customer_profile.strip() or "未知",
        "agent_identity": identity,
        "opening_greeting": opening,
        "state_machine": state_machine,
        "scene_knowledge": knowledge,
    }
    for name, value in values.items():
        prompt = prompt.replace("{{" + name + "}}", value)
    return prompt


class QwenAudioRealtimeModel(openai.realtime.RealtimeModel):
    """LiveKit Realtime adapter for the DashScope Qwen Audio protocol.

    Qwen Audio currently uses the OpenAI Realtime beta-style flat session
    schema and event names. LiveKit's pinned OpenAI plugin already implements
    this compatibility path for Azure beta endpoints; the adapter selects that
    wire format while retaining DashScope Bearer authentication and URL.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        voice: str | None = None,
        turn_detection: BaseModel | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("DASHSCOPE_API_KEY is required for realtime pipeline")

        endpoint = (base_url or os.getenv("QWEN_AUDIO_REALTIME_URL") or DEFAULT_REALTIME_URL).strip()
        selected_model = (model or os.getenv("QWEN_AUDIO_REALTIME_MODEL") or DEFAULT_REALTIME_MODEL).strip()
        selected_voice = (voice or os.getenv("QWEN_AUDIO_REALTIME_VOICE") or DEFAULT_REALTIME_VOICE).strip()
        endpoint_parts = urlsplit(endpoint)
        endpoint_query = dict(parse_qsl(endpoint_parts.query, keep_blank_values=True))
        endpoint_query.setdefault("model", selected_model)
        endpoint = urlunsplit(
            (
                endpoint_parts.scheme,
                endpoint_parts.netloc,
                endpoint_parts.path,
                urlencode(endpoint_query),
                endpoint_parts.fragment,
            )
        )
        if turn_detection is None:
            detection_type = os.getenv(
                "QWEN_AUDIO_REALTIME_TURN_DETECTION", "server_vad"
            ).strip()
            if detection_type == "smart_turn":
                turn_detection = QwenSmartTurn()
            elif detection_type == "server_vad":
                turn_detection = ServerVad(
                    type="server_vad",
                    threshold=float(os.getenv("QWEN_AUDIO_REALTIME_VAD_THRESHOLD", "0.65")),
                    prefix_padding_ms=int(
                        os.getenv("QWEN_AUDIO_REALTIME_PREFIX_PADDING_MS", "200")
                    ),
                    silence_duration_ms=int(
                        os.getenv("QWEN_AUDIO_REALTIME_SILENCE_DURATION_MS", "650")
                    ),
                )
            else:
                raise ValueError(
                    "QWEN_AUDIO_REALTIME_TURN_DETECTION must be smart_turn or server_vad"
                )

        super().__init__(
            model=selected_model,
            voice=selected_voice,
            modalities=["audio", "text"],
            api_key=api_key,
            base_url=endpoint,
            turn_detection=turn_detection,
        )

        # Qwen-Audio requires an explicit response.create after the client
        # appends a function_call_output. Keep this False so LiveKit schedules
        # that second inference instead of waiting for a reply that never
        # arrives. Deterministic terminal transitions may cancel that reply in
        # their function_tools_executed handler and play fixed audio directly.
        self._capabilities.auto_tool_reply_generation = False

        # DashScope speaks the beta-style flat protocol. The pinned LiveKit
        # plugin's compatibility mode also normalizes conversation.item.created
        # and response.audio.* events, which Qwen emits.
        self._opts.is_azure = True
        self._opts.entra_token = api_key
        self._opts.api_key = None

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "dashscope-qwen-audio-realtime"

    def session(self) -> "QwenAudioRealtimeSession":
        session = QwenAudioRealtimeSession(self)
        self._sessions.add(session)
        return session


class QwenAudioRealtimeSession(RealtimeSession):
    """Protocol differences not covered by LiveKit's beta event adapter."""

    def __init__(self, realtime_model: QwenAudioRealtimeModel) -> None:
        super().__init__(realtime_model)
        # Qwen input is PCM16 mono at 16 kHz; its output remains 24 kHz and is
        # decoded by the base implementation at the correct output rate.
        self._bstream = utils.audio.AudioByteStream(
            QWEN_INPUT_SAMPLE_RATE,
            1,
            samples_per_channel=QWEN_INPUT_SAMPLE_RATE // 10,
        )

    def _create_session_update_event(self) -> dict:
        turn_detection = (
            self._opts.turn_detection.model_dump(exclude_none=True)
            if self._opts.turn_detection is not None
            else None
        )
        return {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": self._opts.voice,
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "input_audio_transcription": {"model": "fun-asr"},
                "turn_detection": turn_detection,
            },
        }

    def _resample_audio(self, frame: rtc.AudioFrame) -> Iterator[rtc.AudioFrame]:
        if self._input_resampler and frame.sample_rate != self._input_resampler._input_rate:
            self._input_resampler = None
        if self._input_resampler is None and (
            frame.sample_rate != QWEN_INPUT_SAMPLE_RATE or frame.num_channels != 1
        ):
            self._input_resampler = rtc.AudioResampler(
                input_rate=frame.sample_rate,
                output_rate=QWEN_INPUT_SAMPLE_RATE,
                num_channels=1,
            )
        if self._input_resampler:
            yield from self._input_resampler.push(frame)
        else:
            yield frame

    def _create_update_chat_ctx_events(self, chat_ctx):
        events = super()._create_update_chat_ctx_events(chat_ctx)
        # OpenAI uses the synthetic id "root" for insertion at the beginning;
        # DashScope requires previous_item_id to be omitted in that case.
        for event in events:
            if getattr(event, "previous_item_id", None) == "root":
                event.previous_item_id = None
        return events

    def _handle_response_created(self, event) -> None:
        # DashScope doesn't echo response.create.metadata. Correlate an
        # explicitly requested response with the oldest pending LiveKit future;
        # VAD-created responses have no pending future and remain automatic.
        if not event.response.metadata and self._response_created_futures:
            event.response.metadata = {
                "client_event_id": next(iter(self._response_created_futures))
            }
        super()._handle_response_created(event)

    async def update_tools(self, tools: list[llm.Tool]) -> None:
        qwen_tools: list[dict] = []
        retained: list[llm.Tool] = []
        for tool in tools:
            if isinstance(tool, llm.FunctionTool):
                schema = llm.utils.build_legacy_openai_schema(
                    tool, internally_tagged=True
                )
            elif isinstance(tool, llm.RawFunctionTool):
                schema = dict(tool.info.raw_schema)
                schema.pop("meta", None)
                schema["type"] = "function"
            else:
                continue
            function = {
                key: value
                for key, value in schema.items()
                if key in {"name", "description", "parameters"}
            }
            qwen_tools.append({"type": "function", "function": function})
            retained.append(tool)
        self.send_event(
            {
                "type": "session.update",
                "session": {"tools": qwen_tools, "tool_choice": "auto"},
            }
        )
        self._tools = llm.ToolContext(retained)
