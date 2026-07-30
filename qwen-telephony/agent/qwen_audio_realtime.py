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
            "脉脉公司负责骑手招聘的招聘顾问",
            "您好，我是脉脉招聘顾问。有个职位机会分享给您。",
            """- 入口节点：rider_opening
- 核心目标：了解候选人的实际需要，并在候选人同意后取得可联系的微信号；微信号通常是 11 位手机号。
- 沟通方式：开场白原样播报；开场后每次表达不超过 20 个汉字，一次只问一个问题。根据用户已经提供的信息灵活跳过重复问题，不得连续盘问。
- 真人感：先自然承接用户上一句话，再推进问题。可按语境少量使用“嗯、好、行、对、那、这样啊”等口语词，但要变化且克制；不得每轮固定说“好的”，不得使用客服播报腔。
- 最大有效对话轮数：16
- 默认未识别路由：rider_clarify

节点 rider_opening｜招聘开场｜类型：scene
  对客话术：您好，我是脉脉招聘顾问。有个职位机会分享给您。
  分支路由：方便 -> rider_area；想了解 -> rider_area；直接提问 -> rider_qa；暂时不便 -> rider_callback；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify
  前端配置的意图示例：有意向=[可以, 方便, 你说, 想了解]；暂时不便=[现在忙, 不方便, 晚点]；明确拒绝=[不需要, 没兴趣, 别打了]

节点 rider_area｜了解工作区域｜类型：scene
  对客话术：您想在哪个城市或区域跑单？
  分支路由：提供区域 -> rider_experience；暂未确定 -> rider_experience；询问岗位 -> rider_qa；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify

节点 rider_experience｜关心配送经验｜类型：scene
  对客话术：您之前做过配送吗？没做过也没关系。
  分支路由：有经验 -> rider_vehicle；无经验 -> rider_vehicle；不愿回答 -> rider_schedule；询问问题 -> rider_qa；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify

节点 rider_vehicle｜了解交通工具｜类型：scene
  对客话术：您目前有可用的电动车吗？
  分支路由：有车 -> rider_schedule；没有车 -> rider_schedule；需要租车信息 -> rider_qa；不愿回答 -> rider_schedule；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify

节点 rider_schedule｜了解时间偏好｜类型：scene
  对客话术：您更想全职，还是时间灵活一些？
  分支路由：全职 -> rider_start_time；兼职或灵活 -> rider_start_time；尚未确定 -> rider_start_time；询问问题 -> rider_qa；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify

节点 rider_start_time｜了解到岗时间｜类型：scene
  对客话术：您大概什么时候方便开始？
  分支路由：提供时间 -> rider_concern；暂未确定 -> rider_concern；询问问题 -> rider_qa；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify

节点 rider_concern｜主动关心需求｜类型：scene
  对客话术：您现在最关心收入、时间还是配送距离？
  分支路由：表达关注点 -> rider_qa_then_wechat；没有问题 -> rider_wechat；直接提供微信 -> rider_wechat_collect；明确拒绝 -> rider_end_reject；未识别 -> rider_wechat

节点 rider_qa｜岗位问答｜类型：llm_fallback
  对客话术：先直接回答用户最关心的一点，只说一句短话。资料不足时说“这个要结合您所在区域确认”，再回到被打断前尚未完成的问题。
  分支路由：继续沟通 -> 返回原节点；愿意留微信 -> rider_wechat；要求人工 -> rider_transfer；拒绝 -> rider_end_reject；未识别 -> 返回原节点

节点 rider_qa_then_wechat｜回应关注并衔接微信｜类型：llm_fallback
  对客话术：用一句短话回应关注点；资料不足时明确需要招聘人员确认。下一轮进入 rider_wechat，不要同时追问第二个问题。
  分支路由：已回答 -> rider_wechat；继续提问 -> rider_qa；拒绝 -> rider_end_reject

节点 rider_wechat｜征得同意收集微信｜类型：scene
  对客话术：方便留个微信吗？一般填手机号就可以。
  分支路由：同意提供 -> rider_wechat_collect；直接说号码 -> rider_wechat_collect；微信不是手机号 -> rider_wechat_text；拒绝提供 -> rider_contact_alternative；继续提问 -> rider_qa；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify_wechat

节点 rider_wechat_collect｜收集手机号微信｜类型：scene
  对客话术：好的，您慢慢说，我在听。
  号码规则：把“幺/一”识别为数字 1，把中文数字和逐位口述规范化；忽略空格与连字符后累计数字。只有完整收到 11 位号码才可进入确认节点。
  未说完规则：少于 11 位时绝对不要抢话、猜测、补全或转入其他节点，只说“您接着说，我在听。”并保持 rider_wechat_collect。用户短暂停顿、分段报号或说“嗯”都不代表号码结束。
  分支路由：恰好 11 位 -> rider_wechat_confirm；超过 11 位或边界不清 -> rider_wechat_retry；改用文字微信号 -> rider_wechat_text；拒绝 -> rider_contact_alternative

节点 rider_wechat_retry｜号码澄清｜类型：scene
  对客话术：号码我没确认完整，麻烦您从头慢慢说一遍。
  分支路由：重新报号 -> rider_wechat_collect；改用文字微信号 -> rider_wechat_text；拒绝 -> rider_contact_alternative

节点 rider_wechat_confirm｜复述确认微信｜类型：scene
  对客话术：将完整号码按“前三位、空格、中间四位、空格、后四位”缓慢复述，然后只问“这个微信号对吗？”不得省略或改写数字。
  分支路由：确认正确 -> rider_save；号码有误 -> rider_wechat_retry；补充问题 -> rider_qa；拒绝保存 -> rider_end_reject；未识别 -> rider_wechat_confirm

节点 rider_wechat_text｜收集非手机号微信｜类型：scene
  对客话术：好的，请慢慢说您的微信号。
  分支路由：提供完整微信号 -> rider_wechat_text_confirm；未说完 -> rider_wechat_text；拒绝 -> rider_contact_alternative

节点 rider_wechat_text_confirm｜确认文字微信｜类型：scene
  对客话术：简短复述微信号，并询问是否正确。
  分支路由：确认正确 -> rider_save；有误 -> rider_wechat_text；拒绝保存 -> rider_end_reject

节点 rider_save｜登记结果｜类型：scene
  对客话术：好的，已记下您的微信。
  动作要求：调用 save_call_result，摘要包含区域、经验、交通工具、时间偏好、到岗时间、关注点和已确认微信；未获得的信息写“未提供”，不得编造。
  分支路由：保存成功 -> rider_end_success；保存失败 -> rider_end_manual；继续提问 -> rider_qa

节点 rider_contact_alternative｜尊重隐私｜类型：scene
  对客话术：没关系，您也可以只了解岗位，不用勉强留微信。
  分支路由：继续了解 -> rider_qa；改为提供微信 -> rider_wechat；稍后联系 -> rider_callback；结束 -> rider_end_neutral

节点 rider_callback｜稍后联系｜类型：scene
  对客话术：好的，您什么时候方便联系？
  分支路由：提供时间 -> rider_end_callback；不希望联系 -> rider_end_reject；未识别 -> rider_clarify

节点 rider_clarify｜澄清｜类型：scene
  对客话术：不好意思，您是想继续了解，还是稍后再联系？
  分支路由：继续了解 -> 返回原节点；稍后联系 -> rider_callback；暂不考虑 -> rider_end_reject；未识别 -> rider_clarify

节点 rider_clarify_wechat｜确认微信意愿｜类型：scene
  对客话术：您愿意留个微信，方便招聘人员联系吗？
  分支路由：愿意 -> rider_wechat_collect；拒绝 -> rider_contact_alternative；继续提问 -> rider_qa；未识别 -> rider_clarify_wechat

节点 rider_transfer｜转招聘人员｜类型：scene
  对客话术：好的，我为您联系招聘人员进一步确认，请稍候。
  分支路由：工具成功 -> rider_end_transfer；工具失败 -> rider_end_manual

节点 rider_end_success｜意向登记结束｜类型：end
  对客话术：好的，招聘人员会通过微信联系您。再见。

节点 rider_end_callback｜预约回访结束｜类型：end
  对客话术：好的，我们到时联系您。再见。

节点 rider_end_reject｜拒绝结束｜类型：end
  对客话术：好的，不打扰您了。再见。

节点 rider_end_neutral｜未留微信结束｜类型：end
  对客话术：好的，感谢接听。再见。

节点 rider_end_transfer｜转接结束｜类型：end
  对客话术：已为您转接招聘人员，请稍候。

节点 rider_end_manual｜人工联系失败｜类型：end
  对客话术：暂时无法处理，我已记录您的需求。感谢理解。""",
            """- 岗位性质：骑手配送岗位。
- 工作地点：不同城市和区域的岗位情况可能不同，需要招聘人员结合候选人所在区域确认。
- 工作时间、薪酬、补贴、保险、车辆要求、入职条件和入职时间：当前默认资料未提供，不得给出具体数字或承诺，应交由招聘人员确认。
- 招聘沟通目标：了解区域、经验、交通工具、时间偏好、到岗时间和关注点；最终在候选人同意后获取并确认微信号。
- 微信号：通常是 11 位手机号，也允许非手机号微信号。号码未完整说完时必须继续等待，不得抢话、猜测或补全。
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
    customer_name: str = "",
    customer_company: str = "",
    customer_phone: str = "",
    customer_profile: str = "",
    scene: dict[str, Any] | None = None,
) -> str:
    override = os.getenv("QWEN_AUDIO_REALTIME_INSTRUCTIONS", "").strip()
    if override:
        prompt = override
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

        # DashScope starts the next response itself when a function-call output
        # is appended to the conversation.  The OpenAI adapter defaults this
        # capability to False, which makes LiveKit send an additional
        # response.create after every tool result.  That produces two identical
        # replies for terminal tools such as end_call.
        self._capabilities.auto_tool_reply_generation = True

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
