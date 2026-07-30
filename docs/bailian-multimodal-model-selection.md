# 百炼多模态模型选择与降级策略

更新时间：2026-07-30

## 当前选择

- 现有知识语音客服继续使用 `qwen-audio-3.0-realtime-plus`，避免在知识与工具质量门完成前替换语音主链路。
- 需要原生连续视频理解时，首选 `qwen3.5-omni-plus-realtime`；成本敏感项目可配置 `qwen3.5-omni-flash-realtime`。
- 首期视觉采用 LiveKit 摄像头轨道加按需关键帧识别。视觉不可用、超时或低置信度时继续纯语音，并建议转人工。
- 不启用模型联网搜索。百炼官方说明 Omni Realtime 的联网搜索与 Function Calling 不能同时开启，而企业客服必须保留受控工具调用。

## 官方能力依据

百炼全模态文档将 Qwen3.5-Omni Realtime 定位为实时语音/视频对话模型，支持图片或视频输入与工具调用。实时接口文档列出的当前型号包括
`qwen3.5-omni-plus-realtime` 和 `qwen3.5-omni-flash-realtime`；视频建议按约 1 帧/秒输入，模型上下文中的视频历史存在时长上限。

## 上线配置

```text
INBOUND_VISION_ENABLED=false
INBOUND_VISION_MODEL=qwen3.5-omni-flash
INBOUND_OMNI_REALTIME_MODEL=qwen3.5-omni-plus-realtime
INBOUND_VISION_TIMEOUT_SECONDS=12
INBOUND_VISION_MAX_IMAGE_BYTES=5000000
```

视觉能力默认关闭。启用前必须完成摄像头用途告知、项目灰度、成本限额、低置信度转人工与默认不录制视频的检查。

## 视频客服与数字人选型补充

视频客服的首要目标是“向客户展示”，不是持续分析客户摄像头。前端因此采用数字人主持、审核素材大屏、讲解流程和实时问答四部分。

- 实时问答继续使用 `qwen-audio-3.0-realtime-plus`，企业知识和 MCP 工具由现有 Agent Function Calling 提供。
- 实时数字人优先接阿里云数字人开放平台的实时流，或通过当前 Avatar Provider 适配成 LiveKit 远端参与者。
- 百炼 `Wan-Digital Human` 适合用人像和讲解音频异步生成高质量口播视频，不作为实时会话模型。
- `LivePortrait` 适合超过 20 秒、动作较简单且更关注成本的预生成讲解。
- 预生成视频必须进入素材审核和发布流程，再由 `show_content` 向客户展示。

交互和工程参考：

- `livekit/livekit`：继续作为音视频与数据传输底座，Apache-2.0。
- `lipku/LiveTalking`：参考可打断数字人、闲置动作和内容编排，不直接复制其模型或带水印资产。
- `OpenTalker/SadTalker`：参考异步人像口播生产流程，Apache-2.0；不用于低延迟互动。
- `Alibaba-Quark/LiveAvatar`：跟踪实时生成路线，当前显存门槛较高，不进入默认生产链路。
