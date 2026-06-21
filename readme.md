# Qwen LiveKit SIP Voice Server

## MicroSIP ringing 最新排查结论

现象：MicroSIP 呼叫 `sip:1000@127.0.0.1:5066` 后一直停在 `ringing`。

结论：这次不是 MicroSIP 账号、Source Port 或 RTP Port 配置问题。SIP INVITE 已经进入 LiveKit SIP，并创建了 `qwen-phone-room` 的 SIP participant；真正原因是 Python Agent worker 没有注册到 LiveKit，所以没有 Agent 接听该 room job。

已修复项：
- `qwen-telephony/agent/phone_agent.py` 的 `AgentServer` 已显式设置 `http_proxy=None`。
- 原因是 `livekit-agents` 默认读取 `HTTP_PROXY/HTTPS_PROXY`，导致 worker 连接本机 `ws://127.0.0.1:7880/agent` 时走系统代理，代理返回 404。

验证结果：
- 手工 WebSocket 握手 `ws://127.0.0.1:7880/agent` 成功。
- Agent 重启后日志出现 `registered worker`。
- LiveKit server 日志出现 `worker registered`，worker ID 为 `AW_tU3eGFxNpHNK`。
- `curl http://127.0.0.1:18081/worker` 返回 `worker_type: JT_ROOM`。

如果再次出现 ringing，优先检查：
```powershell
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && tail -n 120 qwen-telephony/logs/agent.log"
wsl -d Ubuntu -- bash -lc "docker logs --since 5m qwen-livekit 2>&1 | grep -E 'worker registered|agent|404|401|error|warn' | tail -n 120"
```

正常状态必须看到 `registered worker`；如果只有 SIP 日志有 `ringing`，但没有 Agent worker 注册，MicroSIP 会一直等待接听。

本目录已构建一个本地语音电话服务端：

- `tools/microsip/MicroSIP.exe` 作为 SIP 电话客户端。
- `livekit/livekit-server` 作为本地媒体/房间服务。
- `livekit/sip` 作为 SIP 到 LiveKit room 的桥接服务。
- `livekit-agents` Python Agent 接入 Qwen ASR、LLM、TTS。
- DashScope/Qwen API Key 从根目录 `.env` 读取，不写入日志或文档。

## 模块 1：仓库与基础环境

方案：
- 已下载并保留源码仓库：`livekit`、`agents`、`sip`、`agents-js`。
- Windows 负责 MicroSIP 客户端；WSL Ubuntu 负责 Docker、LiveKit/SIP 容器和 Python Agent。
- Python Agent 运行目录为 `qwen-telephony/`。

测试结果：
- WSL Python 3.12 可用，Python 依赖已安装到 `qwen-telephony/.venv`。
- Windows Python venv 也已安装到 `qwen-telephony/.venv-win`，但 Windows LiveKit FFI 测试不稳定，最终采用 WSL Agent。
- Docker Desktop 可由 WSL 调用；Docker credential 配置已调整为可拉取镜像。

## 模块 2：LiveKit/SIP 基础设施

方案：
- `qwen-telephony/scripts/start-infra-wsl.sh` 启动：
  - `qwen-livekit-redis`
  - `qwen-livekit`
  - `qwen-livekit-sip`
- SIP 监听端口为 `5066`，避免和本机已有服务冲突。
- RTP 范围为 `10000-10100/udp`。
- LiveKit `node-ip` 设置为当前 Windows/LAN 可达地址 `192.168.1.7`，避免向 WSL Agent 广告不可达的 Docker bridge 地址。

测试结果：
- `qwen-livekit` HTTP 健康检查通过：`http://127.0.0.1:7880` 返回 OK。
- LiveKit 日志确认：`nodeIP=192.168.1.7`。
- SIP 容器和 LiveKit 容器均运行，SIP 日志显示呼叫进入后 ICE candidate pair 成功。

## 模块 3：SIP Trunk 与 MicroSIP

方案：
- `qwen-telephony/scripts/init-sip.py` 创建 inbound trunk 和 dispatch rule。
- 当前呼叫目标：`sip:1000@127.0.0.1:5066`。
- MicroSIP 配置已备份到 `tools/microsip/microsip.ini.bak-qwen-telephony`。
- MicroSIP 使用 local account 呼叫，不走 REGISTER；LiveKit SIP 只处理 INVITE。

测试结果：
- 最新初始化结果：
  - inbound trunk: `ST_DLzaFKpsHQtV`
  - dispatch rule: `SDR_Ykczs5aEP93P`
  - room: `qwen-phone-room`
- MicroSIP 发起呼叫后，LiveKit room 中出现 SIP participant，SIP 音频 track 发布成功。

## 模块 4：Qwen Providers

方案：
- `qwen-telephony/agent/qwen_providers.py` 提供：
  - `QwenASR`：LiveKit STT provider，调用 DashScope OpenAI-compatible `qwen3-asr-flash`。
  - `QwenTTS`：LiveKit TTS provider，调用 DashScope Qwen-TTS。
- TTS 在 WSL 中使用 DashScope SSE 音频块，绕过 WSL 访问 OSS 临时 URL 的 DNS/网络问题。
- Windows TTS 可设置 `QWEN_TTS_USE_SSE=false` 使用非流式 URL 下载。

测试结果：
- TTS WSL 小样本通过：`audio/wav`，122924 字节，文件头 `RIFF`。
- TTS Windows 小样本通过：`audio/x-wav`，153644 字节，文件头 `RIFF`。
- ASR 小样本通过：识别 TTS 生成音频，返回 `你好，这是语音电话系统测试。`，confidence `1.0`。
- LLM 通话中调用 `qwen-plus` 成功，Agent 日志记录 LLM metrics。

## 模块 5：Agent 与端到端通话

方案：
- `qwen-telephony/agent/phone_agent.py` 使用 `AgentServer` + `AgentSession`。
- 进入 job 时先显式执行 `ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)`，再启动 `AgentSession`。
- STT 使用 `StreamAdapter(QwenASR + Silero VAD)`；LLM 使用 Qwen OpenAI-compatible；TTS 使用 `QwenTTS`。
- WSL Agent 使用 `livekit 1.1.10` 规避 RTC FFI 问题。

测试结果：
- WSL Agent 已注册到 LiveKit，日志显示 `rtc-version: 1.1.10`。
- MicroSIP 呼叫后，Agent 收到 job request。
- LiveKit 日志显示 Agent participant `ACTIVE`，connection type `udp`，downtrack connected。
- Agent 日志显示：
  - `start reading stream` from SIP participant
  - `LLM metrics` for `qwen-plus`
  - `TTS metrics` for `qwen3-tts-flash`
  - assistant greeting: `您好！我是您的语音电话助手，现在可以开始通话了。`
- LiveKit 日志显示 Agent 发布 `roomio_audio` 音频 track，SIP participant 可订阅该 track。

## 启动方式

### 完整链路启动过程

1. 确认根目录 `.env` 中存在 `DASHSCOPE_API_KEY`，并按需配置：
   - `QWEN_LLM_MODEL=qwen-plus`
   - `QWEN_ASR_MODEL=qwen3-asr-flash`
   - `QWEN_TTS_MODEL=qwen3-tts-flash`

2. 启动 LiveKit、Redis、LiveKit SIP，并初始化 SIP trunk/dispatch rule：

```powershell
cd F:\ai-login-replica\agent
.\qwen-telephony\scripts\start-system.ps1
```

该脚本会通过 WSL/Docker 启动 `qwen-livekit-redis`、`qwen-livekit`、`qwen-livekit-sip`，并创建/更新 inbound trunk、dispatch rule 和默认房间 `qwen-phone-room`。

3. 启动 Python Agent：

```powershell
cd F:\ai-login-replica\agent
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && qwen-telephony/scripts/start-agent-wsl.sh"
```

也可以后台启动并写入日志：

```powershell
cd F:\ai-login-replica\agent
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && pgrep -f '[p]hone_agent.py dev' | xargs -r kill; nohup qwen-telephony/scripts/start-agent-wsl.sh > qwen-telephony/logs/agent.log 2>&1 < /dev/null &"
```

确认 Agent 已注册：

```powershell
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && tail -n 80 qwen-telephony/logs/agent.log"
```

正常应看到 `starting worker`、`registered worker`、`url: ws://localhost:7880`。

4. 启动 MicroSIP 并直拨：

```powershell
cd F:\ai-login-replica\agent
Start-Process .\tools\microsip\MicroSIP.exe -ArgumentList "sip:1000@127.0.0.1:5066"
```

MicroSIP 要点：
- 使用 Local Account。
- 不需要注册 SIP 账号。
- 不要拨 `5060` 或 `5070`。
- 目标固定为 `sip:1000@127.0.0.1:5066`。
- `Source Port=5062`、`RTP Ports=20000-20020` 可保持默认，不需要改成服务端端口。

5. 验证通话是否完整接通：

```powershell
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && tail -n 160 qwen-telephony/logs/agent.log"
```

正常通话中应看到 `received job request`、`start reading stream`、`STT metrics`、`received user transcript`、`LLM metrics`、`TTS metrics`。

6. 停止基础设施：

```powershell
cd F:\ai-login-replica\agent
.\qwen-telephony\scripts\stop-infra.ps1
```

若 MicroSIP 状态异常，可先强制退出再重新直拨：

```powershell
taskkill /IM MicroSIP.exe /F
cd F:\ai-login-replica\agent
Start-Process .\tools\microsip\MicroSIP.exe -ArgumentList "sip:1000@127.0.0.1:5066"
```

## 当前注意事项

- 首次启动 Agent 预热较慢，可能需要 60-120 秒才注册 worker。
- `livekit-agents 1.6.2` 声明依赖 `livekit==1.1.9`，但本环境中 `1.1.9` 会触发 RTC FFI ready timeout；已在 WSL venv 中手动升级到 `livekit==1.1.10`。
- `LIVEKIT_NODE_IP=192.168.1.7` 是当前机器网络地址；如果本机 IP 变化，需要更新 `qwen-telephony/config/local.env` 并重启基础设施。

## 响应速度策略

当前 Agent 已启用以下低延迟策略：
- 问候语使用 `greeting_8k.wav` 通过 LiveKit 临时音频轨直接播放，不经过 LLM、TTS 或 AgentSession。
- 播放问候语时在后台线程启动一次最小 Qwen LLM warm-up 请求，降低第一轮正式回答的冷启动概率，同时不阻塞 greeting 音频发送。
- ASR 默认使用 DashScope Qwen Realtime WebSocket：`qwen3-omni-flash-realtime` + `qwen3-asr-flash-realtime`。
- `preemptive_generation.enabled=true`，允许在 turn 最终确认前提前启动 LLM。
- `preemptive_generation.preemptive_tts=true`，允许在 LLM 输出过程中更早启动 TTS。
- `endpointing.mode=fixed`，`min_delay=0.1`，`max_delay=0.6`，减少用户停顿后的等待时间。
- Qwen Realtime ASR 的服务端 VAD 使用 `silence_duration_ms=300`、`prefix_padding_ms=200`。
- Qwen TTS 使用 DashScope SSE 增量音频，避免等待完整音频文件生成后才开始输出。
- 系统提示词要求每次回答优先控制在一到两句话，减少 TTS 合成和播放时长。

可通过环境变量回退到原 HTTP ASR + 本地 VAD：

```powershell
QWEN_USE_REALTIME_ASR=false
```

可通过环境变量关闭 LLM warm-up：

```powershell
QWEN_LLM_WARMUP=false
```

模块测试结果：
- Realtime ASR WebSocket 已完成连接测试，服务端返回 `session.updated`。
- 使用本地 `greeting_8k.wav` 上行测试 Realtime ASR，服务端返回 `conversation.item.input_audio_transcription.completed`。
- Qwen TTS SSE 已确认返回多段增量音频 chunk。
- Agent 已重启并注册到 LiveKit：日志出现 `registered worker`。

## MicroSIP ringing 排查记录

方案：
- LiveKit SIP 不提供传统 SIP 账号注册服务，MicroSIP 应使用 local account 直拨，不应依赖 `Account1` 注册成功。
- 推荐直接拨号：`sip:1000@127.0.0.1:5066`。
- 若 MicroSIP 界面停在 `ringing`，先挂断并从托盘彻底退出 MicroSIP，再重新拨号；必要时重启 `qwen-livekit-sip`、`qwen-livekit` 和 Agent 以清理旧 room/job。

测试结果：
- 当前 `microsip.ini` 已启用 `enableLocalAccount=1`、`accountId=0`，本地直拨模式可用。
- Agent 日志确认新呼叫已进入：`received job request`。
- Agent 后续日志确认 Qwen 链路已工作：出现 `LLM metrics` 和 `TTS metrics`。
- SIP 日志曾出现 `reason=cancelled` 和 `reason=cannot-subscribe`；这是客户端取消、旧呼叫或重复 job 残留时的典型现象，需清掉旧呼叫后重新直拨。
