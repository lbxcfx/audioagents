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
            "陌陌公司负责骑手招聘的招聘专员",
            "您好，我是陌陌公司HR，这边有一个骑手的工作机会，想和您简单介绍一下，请问您现在方便吗？",
            """- 入口节点：rider_opening
- 最大有效对话轮数：10
- 默认未识别路由：rider_clarify

节点 rider_opening｜招聘开场｜类型：scene
  对客话术：您好，我是陌陌公司HR，这边有一个骑手的工作机会，想和您简单介绍一下，请问您现在方便吗？
  分支路由：有意向 -> rider_intro；想了解 -> rider_intro；暂时不便 -> rider_callback；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify
  前端配置的意图示例：有意向=[可以, 方便, 你说, 想了解]；暂时不便=[现在忙, 不方便, 晚点]；明确拒绝=[不需要, 没兴趣, 别打了]

节点 rider_intro｜岗位介绍｜类型：scene
  对客话术：这个岗位主要是骑手配送工作。具体工作地点、时间安排、薪酬和入职要求需要结合您所在区域由招聘人员确认。您愿意进一步了解吗？
  分支路由：愿意了解 -> rider_collect；询问岗位 -> rider_qa；暂不考虑 -> rider_end_reject；要求人工 -> rider_transfer；未识别 -> rider_clarify

节点 rider_collect｜意向确认｜类型：scene
  对客话术：好的。为了安排招聘人员和您联系，请问您目前所在的城市或区域是哪里？
  分支路由：提供区域 -> rider_confirm；拒绝提供 -> rider_transfer；询问问题 -> rider_qa；明确拒绝 -> rider_end_reject；未识别 -> rider_clarify

节点 rider_confirm｜确认跟进｜类型：scene
  对客话术：好的，我会按您提供的信息提交招聘咨询。具体岗位情况请以后续招聘人员确认为准。
  分支路由：确认 -> rider_end_success；继续提问 -> rider_qa；取消 -> rider_end_reject；要求人工 -> rider_transfer

节点 rider_qa｜岗位问答｜类型：llm_fallback
  对客话术：仅根据本 Prompt 的招聘资料简短回答；没有明确资料时说明需要招聘人员结合所在区域确认，然后询问是否继续登记意向。
  分支路由：继续 -> rider_collect；要求人工 -> rider_transfer；拒绝 -> rider_end_reject；未识别 -> rider_clarify

节点 rider_callback｜稍后联系｜类型：scene
  对客话术：好的，请问什么时间联系您比较方便？
  分支路由：提供时间 -> rider_end_callback；不希望联系 -> rider_end_reject；未识别 -> rider_clarify

节点 rider_clarify｜澄清｜类型：scene
  对客话术：不好意思，我想确认一下，您是愿意了解骑手岗位、希望稍后联系，还是暂时不考虑呢？
  分支路由：愿意了解 -> rider_intro；稍后联系 -> rider_callback；暂不考虑 -> rider_end_reject；未识别 -> rider_clarify

节点 rider_transfer｜转招聘人员｜类型：scene
  对客话术：好的，我为您联系招聘人员进一步确认，请稍候。
  分支路由：工具成功 -> rider_end_transfer；工具失败 -> rider_end_manual

节点 rider_end_success｜意向登记结束｜类型：end
  对客话术：感谢您的了解，具体岗位信息以后续招聘人员与您确认为准，祝您生活愉快，再见。

节点 rider_end_callback｜预约回访结束｜类型：end
  对客话术：好的，我们会按您方便的时间再联系，感谢接听，再见。

节点 rider_end_reject｜拒绝结束｜类型：end
  对客话术：好的，了解了，那就不打扰您了，祝您生活愉快，再见。

节点 rider_end_transfer｜转接结束｜类型：end
  对客话术：已为您转接招聘人员，请稍候。

节点 rider_end_manual｜人工联系失败｜类型：end
  对客话术：暂时无法为您转接，具体岗位信息请以后续招聘人员联系为准，感谢您的理解。""",
            """- 岗位性质：骑手配送岗位。
- 工作地点：不同城市和区域的岗位情况可能不同，需要招聘人员结合候选人所在区域确认。
- 工作时间、薪酬、补贴、保险、车辆要求、入职条件和入职时间：当前默认资料未提供，不得给出具体数字或承诺，应交由招聘人员确认。
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
