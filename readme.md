# Audio Agents

本项目是一个本地语音电话服务端示例：使用 MicroSIP 拨入 LiveKit SIP，电话音频进入 LiveKit room 后由 Python Agent 接管，并调用 Qwen/DashScope 完成 ASR、LLM 和 TTS，实现电话语音交互。

## 主要功能

- 使用 `tools/microsip/MicroSIP.exe` 作为本地 SIP 电话客户端。
- 使用 Docker 启动本地 LiveKit Server、LiveKit SIP 和 Redis。
- 使用 LiveKit Agents Python 进程接听 SIP room job。
- 使用 Qwen/DashScope：
  - Realtime ASR：`qwen3-omni-flash-realtime` + `qwen3-asr-flash-realtime`
  - LLM：默认 `qwen-plus`
  - TTS：默认 `qwen3-tts-flash`
- 拨通后直接播放本地 `greeting_8k.wav` 问候语，不等待 LLM/TTS。
- 支持一键健康检查：服务异常时自动启动或修复。

## 目录结构

```text
qwen-telephony/
  agent/
    phone_agent.py        # LiveKit Agent 主流程
    qwen_providers.py     # Qwen ASR/TTS provider
  config/
    local.env.example     # 本地配置模板
  scripts/
    bootstrap-wsl.sh      # 安装 WSL Python 依赖
    start-infra-wsl.sh    # 启动 Redis/LiveKit/LiveKit SIP
    init-sip.py           # 创建 SIP trunk 和 dispatch rule
    start-agent-wsl.sh    # 前台启动 Agent
    start-agent-bg-wsl.sh # 后台启动 Agent
    health-start-wsl.sh   # 健康检查与自启动
    stop-infra-wsl.sh     # 停止 Docker 基础设施
tools/
  microsip/
    MicroSIP.exe          # SIP 客户端
```

`livekit/`、`agents/`、`sip/`、`agents-js/` 是开发研究时克隆的上游仓库，当前运行链路不直接依赖这些本地源码目录。上游来源记录见 `UPSTREAM_REPOS.md`。

## 环境要求

- Windows
- WSL Ubuntu
- Docker Desktop，并启用 WSL 集成
- Python 3.12 或兼容版本，运行在 WSL 中
- 可访问 DashScope/Qwen API
- 根目录 `.env` 中配置 `DASHSCOPE_API_KEY`

## 配置

1. 在项目根目录创建 `.env`：

```env
DASHSCOPE_API_KEY=your_dashscope_api_key
```

2. 创建本地运行配置：

```powershell
cd F:\ai-login-replica\agent
copy qwen-telephony\config\local.env.example qwen-telephony\config\local.env
```

3. 按需修改 `qwen-telephony/config/local.env`。

常用配置：

```env
LIVEKIT_URL=ws://127.0.0.1:7880
LIVEKIT_HTTP_URL=http://127.0.0.1:7880
LIVEKIT_NODE_IP=127.0.0.1

QWEN_LLM_MODEL=qwen-plus
QWEN_ASR_MODEL=qwen3-asr-flash
QWEN_TTS_MODEL=qwen3-tts-flash
QWEN_TTS_VOICE=Cherry

SIP_PORT=5066
SIP_RTP_PORT_RANGE=10000-10100
SIP_INBOUND_NUMBER=1000
```

如果 Windows 局域网 IP 变化，且 SIP/RTP 出现不可达问题，请更新 `LIVEKIT_NODE_IP` 后重启基础设施。

## 安装依赖

在 Windows PowerShell 中执行：

```powershell
cd F:\ai-login-replica\agent
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && qwen-telephony/scripts/bootstrap-wsl.sh"
```

脚本会创建 `qwen-telephony/.venv` 并安装 Python 依赖。

## 启动方式

推荐使用健康检查脚本启动。它会检查 Docker、LiveKit、SIP、SIP trunk/dispatch rule、Agent 进程和 worker 注册状态；如果发现异常，会自动启动或修复。

```powershell
cd F:\ai-login-replica\agent
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && qwen-telephony/scripts/health-start-wsl.sh"
```

正常输出应包含：

```text
System healthy
LiveKit: ws://127.0.0.1:7880
SIP: sip:1000@127.0.0.1:5066
```

## 手动启动

如需分步启动：

1. 启动 LiveKit、Redis、LiveKit SIP：

```powershell
cd F:\ai-login-replica\agent
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && qwen-telephony/scripts/start-infra-wsl.sh"
```

2. 初始化 SIP trunk 和 dispatch rule：

```powershell
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && qwen-telephony/scripts/init-sip-wsl.sh"
```

3. 后台启动 Agent：

```powershell
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && qwen-telephony/scripts/start-agent-bg-wsl.sh"
```

4. 查看 Agent 日志：

```powershell
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && tail -n 120 qwen-telephony/logs/agent.log"
```

正常应看到 `registered worker`。

## 使用 MicroSIP 拨号

启动 MicroSIP：

```powershell
cd F:\ai-login-replica\agent
Start-Process .\tools\microsip\MicroSIP.exe -ArgumentList "sip:1000@127.0.0.1:5066"
```

也可以打开 MicroSIP 后手动拨：

```text
sip:1000@127.0.0.1:5066
```

MicroSIP 要点：

- 使用 Local Account。
- 不需要 SIP 注册账号。
- 服务端 SIP 端口是 `5066`。
- MicroSIP 的 `Source Port=5062`、`RTP Ports=20000-20020` 可保持默认。
- 媒体编码建议启用 `G.711 A-law` 和 `G.711 u-law`。

## 自动测试

可用脚本自动拨号并验证 greeting 是否及时播放：

```powershell
cd F:\ai-login-replica\agent
powershell -ExecutionPolicy Bypass -File qwen-telephony\scripts\test-microsip-greeting.ps1 -Seconds 12 -MaxFirstFrameSeconds 4
```

测试通过时会看到 `RESULT direct_first_frame_after_job=...`。

## 停止服务

停止 Docker 基础设施：

```powershell
cd F:\ai-login-replica\agent
.\qwen-telephony\scripts\stop-infra.ps1
```

停止 MicroSIP：

```powershell
taskkill /IM MicroSIP.exe /F
```

停止 Agent：

```powershell
wsl -d Ubuntu -- bash -lc "pgrep -f 'python -u phone_agent.py start' | xargs -r kill"
```

## 常用日志

Agent 日志：

```powershell
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && tail -n 160 qwen-telephony/logs/agent.log"
```

LiveKit 日志：

```powershell
wsl -d Ubuntu -- bash -lc "docker logs --tail 120 qwen-livekit"
```

LiveKit SIP 日志：

```powershell
wsl -d Ubuntu -- bash -lc "docker logs --tail 120 qwen-livekit-sip"
```

## 低延迟策略

当前 Agent 已启用：

- greeting 使用本地 8 kHz WAV 直接推送到 LiveKit 音频轨。
- 播放 greeting 时后台线程执行一次 LLM warm-up，不阻塞音频发送。
- ASR 默认使用 DashScope Qwen Realtime WebSocket。
- LiveKit turn endpointing 使用较短等待：`min_delay=0.1`、`max_delay=0.6`。
- Qwen TTS 使用 DashScope SSE 增量音频输出。
- 系统提示词要求回答尽量简短，减少 TTS 合成和播放时间。

可选环境变量：

```env
QWEN_USE_REALTIME_ASR=false
QWEN_LLM_WARMUP=false
QWEN_TTS_USE_SSE=false
```

## GitHub 提交说明

本仓库提交的是当前语音电话服务端工程代码。运行时依赖通过 `requirements.txt`、Docker 镜像和 MicroSIP 工具提供，不需要提交本地克隆的 LiveKit 上游源码仓库。
