# Audio Agents

本项目是一个本地语音电话服务端示例：使用 MicroSIP 拨入 LiveKit SIP，电话音频进入 LiveKit room 后由 Python Agent 接管，并调用 Qwen/DashScope 完成 ASR、LLM 和 TTS，实现电话语音交互。

## 主要功能

- 使用 `tools/microsip/MicroSIP.exe` 作为本地 SIP 电话客户端。
- 使用 Docker 启动本地 LiveKit Server、LiveKit SIP 和 Redis。
- 使用 LiveKit Agents Python 进程接听 SIP room job。
- 提供 AI 电话运营台，用于管理外呼任务、客户名单、通话队列和业务统计。
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
    start-ops-wsl.sh      # 启动运营台 Web 服务
    stop-infra-wsl.sh     # 停止 Docker 基础设施
  server/
    main.py               # FastAPI 运营台 API
    static/               # 运营台前端页面
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
QWEN_TURN_DETECTION_MODE=multilingual
QWEN_ENDPOINTING_MODE=dynamic

SIP_PORT=5065
SIP_RTP_PORT_RANGE=10000-10100
SIP_INBOUND_NUMBER=1000
```

如果 Windows 局域网 IP 变化，且 SIP/RTP 出现不可达问题，请更新 `LIVEKIT_NODE_IP` 后重启基础设施。

### 默认外呼线路：青川云非注册中继

青川云是当前默认外呼线路，青山云仅作为备用线路。以下配置已在真实手机通话中
验证：成功协商 `PCMU/8000`，连续下行 RTP 超过 218 秒，没有原线路约 95 秒的
固定断流现象。

当前默认线路的非敏感参数如下：

```env
# LiveKit SIP 对外公布的公网信令/媒体地址，不能使用 127.0.0.1。
LIVEKIT_NODE_IP=120.55.185.55
DEV_SIP_ADVERTISED_IP=120.55.185.55
SIP_PORT=5065
SIP_RTP_PORT_RANGE=10000-10100

# 青川云是 IP 白名单非注册中继；E.164 的 +86 在发送 INVITE 前移除。
QWEN_SIP_DIAL_PREFIX=
QWEN_SIP_STRIP_COUNTRY_CODE=86
QWEN_SIP_REGISTER_ENABLED=false
QWEN_SIP_REGISTER_HOST=160.202.254.71
QWEN_SIP_REGISTER_PORT=5060
QWEN_SIP_REGISTER_USERNAME=83450325
QWEN_SIP_REGISTER_AUTH_USERNAME=83450325
QWEN_SIP_REGISTER_DOMAIN=160.202.254.71
QWEN_SIP_REGISTER_CONTACT_HOST=120.55.185.55
QWEN_SIP_REGISTER_CONTACT_PORT=5065
QWEN_SIP_REGISTRATION_KEEPALIVE_PROFILES=
```

密码属于 Secret，只写入仓库根目录 `.env`，不要提交到 Git 或复制到本文：

```env
QWEN_SIP_QINGCHUANYUN_AUTH_PASSWORD=your_sip_password
```

LiveKit outbound trunk 应与注册配置一致：

```text
name: qingchuanyun-83450325-outbound
address: 160.202.254.71:5060
transport: UDP
number / auth username: 83450325
from host: cc.qingchuanyun.cn
advertised signaling address: 120.55.185.55:5065
media codecs: PCMU/8000
```

### 青川云外呼拨号方式

2026-08-05 的真实外呼日志已确认，青川云线路发送 INVITE 时必须把线路账号和
被叫号码分别传递，不能把两者拼接成一个号码：

```text
LiveKit SIP trunk ID: ST_Dg6YGoNir6S5
sip_number / From user: 83450325
sip_call_to / Request-URI user / To user: 11 位手机号码
codec: PCMU/8000
```

例如拨打 `18911129833`：

```python
participant = await lk.sip.create_sip_participant(
    api.CreateSIPParticipantRequest(
        room_name=room_name,
        sip_trunk_id="ST_Dg6YGoNir6S5",
        sip_number="83450325",
        sip_call_to="18911129833",
        participant_identity="callee",
        wait_until_answered=True,
        media=api.SIPMediaConfig(
            codecs=[api.SIPCodec(name="PCMU", rate=8000)],
            only_listed_codecs=True,
        ),
    )
)
```

号码规范化规则：输入为 `+86` 或 `86` 开头的中国大陆手机号时，先去掉国家码，
最终发送 11 位手机号。正确格式是 `sip_call_to="18911129833"`；不要使用
`83450325+18911129833`、`8345032518911129833` 或 `+8618911129833`。成功日志中的
实际 SIP 字段为 `fromUser=83450325`、`toUser=reqUser=18911129833`。

控制面默认使用 `qingchuanyun-83450325`（trunk ID
`8d004bd7-09dd-4937-a09c-c0c98d846de2`），对应 LiveKit trunk
`ST_Dg6YGoNir6S5`。旧青川云 trunk 已禁用；青山云 trunk 保持启用，仅用于人工选择
或故障切换。

该地址会响应 SIP OPTIONS，但不会响应 REGISTER，因此青川云不能加入
`QWEN_SIP_REGISTRATION_KEEPALIVE_PROFILES`。Agent 直接通过 LiveKit outbound trunk
发送 INVITE；若服务器要求 Digest，LiveKit 使用 trunk 中保存的账号和 Secret 完成鉴权。
青山云备用线路使用独立的 carrier-specific REGISTER profile，不受此配置影响。

正式通话必须把 UDP/TCP `5065` 和 UDP `10000-10100` 映射到本机，并确保云防火墙
仅对所需运营商地址放行。外发 INVITE 的 Via、Contact 和公网源端口必须统一为
`120.55.185.55:5065`；只做 `5065:5066` 端口映射不能保证外发源端口正确。
修改 `DEV_SIP_ADVERTISED_IP` 后需要重建 LiveKit SIP 容器：

```bash
docker compose --env-file qwen-telephony/config/dev.env \
  -f docker-compose.dev.yml up -d --no-deps --force-recreate livekit-sip
```

拨通后不要立即删除 LiveKit room。`CreateSIPParticipant(wait_until_answered=true)` 返回
表示电话已经接通，而不是通话已经结束；room 必须一直保留到对端挂机、Agent 请求结束
或达到最大通话时长。调用 `DeleteRoom` 会让 LiveKit SIP 立即发送 BYE，日志中表现为
`reason: ROOM_DELETED`。

## 安装依赖

在 Windows PowerShell 中执行：

```powershell
cd F:\ai-login-replica\agent
wsl -d Ubuntu -- bash -lc "cd /mnt/f/ai-login-replica/agent && qwen-telephony/scripts/bootstrap-wsl.sh"
```

脚本会创建 `qwen-telephony/.venv` 并安装 Python 依赖。首次启动 Agent
前还需在该虚拟环境中下载 Silero VAD 和文本 Turn Detector 模型：

```bash
cd qwen-telephony
.venv/bin/python -m livekit.agents download-files
```

## 混合开发模式

功能开发阶段建议只用 Docker 运行 PostgreSQL、Redis、LiveKit 和
LiveKit SIP；控制面、Dispatcher、Phone Agent 和 React 前端直接从源码运行，
以便使用自动重载、断点和前台日志。

首次准备：

```bash
cp qwen-telephony/config/dev.env.example qwen-telephony/config/dev.env
# 将真实 DASHSCOPE_API_KEY 写入根目录 .env
qwen-telephony/scripts/bootstrap-wsl.sh
```

启动基础设施：

```bash
qwen-telephony/scripts/dev-infra.sh up
qwen-telephony/scripts/dev-init-sip.sh
```

随后分别打开终端启动需要调试的源码服务：

```bash
qwen-telephony/scripts/dev-control-plane.sh
qwen-telephony/scripts/dev-frontend.sh
qwen-telephony/scripts/dev-agent.sh
```

创建项目并把 `telephony-worker` 加入项目后，在 `dev.env` 设置
`CLOUD_PARITY_TELEPHONY_PROJECT_IDS`，然后启动 Dispatcher：

```bash
qwen-telephony/scripts/dev-dispatcher.sh
```

状态检查与停止基础设施：

```bash
qwen-telephony/scripts/dev-check.sh
qwen-telephony/scripts/dev-infra.sh down
```

开发数据默认保留在 Docker 卷中。只有确认要删除 PostgreSQL 和 Redis 开发数据时，
才执行 `qwen-telephony/scripts/dev-infra.sh reset --delete-data`。

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
SIP: sip:1000@127.0.0.1:5065
```

## 启动运营台

运营台用于管理外呼任务、客户名单、通话队列和统计分析。

### 从 Windows 访问远程开发前端

远程服务器上的 Vite 前端监听 `127.0.0.1:5173` 时，在 Windows PowerShell 中执行：

```powershell
ssh -i "$env:USERPROFILE\.ssh\audioagents" -o ExitOnForwardFailure=yes -N -L 127.0.0.1:15173:127.0.0.1:5173 root@120.55.185.55
```

保持该 PowerShell 窗口运行，然后在 Windows 浏览器打开：

```text
http://127.0.0.1:15173/
```

该隧道只绑定 Windows 本机回环地址，并将本地 `15173` 映射到远程服务器的 `5173`。如果提示本地端口被占用，请先停止占用 `15173` 的本机进程；`ExitOnForwardFailure` 会在转发创建失败时立即退出。

设计方案见：

```text
docs/ai-call-ops-ui.md
```

```powershell
cd F:\ai-login-replica\agent
.\qwen-telephony\scripts\start-ops.ps1
```

然后打开：

```text
http://127.0.0.1:8090
```

首次启动会创建本地 SQLite 数据库：

```text
qwen-telephony/data/ops.sqlite3
```

该数据库是运行态数据，不提交到 Git。

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
Start-Process .\tools\microsip\MicroSIP.exe -ArgumentList "sip:1000@127.0.0.1:5065"
```

也可以打开 MicroSIP 后手动拨：

```text
sip:1000@127.0.0.1:5065
```

MicroSIP 要点：

- 使用 Local Account。
- 不需要 SIP 注册账号。
- 服务端 SIP 端口是 `5065`。
- MicroSIP 的 `Source Port=5062`、`RTP Ports=20000-20020` 可保持默认。
- 媒体编码建议启用 `G.711 A-law` 和 `G.711 u-law`。

## 自动测试

可用脚本自动拨号并验证 greeting 是否及时播放：

```powershell
cd F:\ai-login-replica\agent
powershell -ExecutionPolicy Bypass -File qwen-telephony\scripts\test-microsip-greeting.ps1 -Seconds 12 -MaxFirstFrameSeconds 4
```

测试通过时会看到 `RESULT direct_first_frame_after_job=...`。

## 运营台 API

常用接口：

```text
GET  /api/health
GET  /api/dashboard
GET  /api/campaigns
POST /api/campaigns
GET  /api/contacts
POST /api/contacts
GET  /api/calls
POST /api/calls
POST /api/campaigns/{campaign_id}/enqueue
POST /api/calls/{call_id}/dial
POST /api/calls/{call_id}/simulate
```

当前 `/api/calls/{call_id}/dial` 是 MVP 占位动作，会把通话状态改为 `dialing` 并生成 room 名称。接入真实 outbound SIP trunk 后，可在该接口中调用 LiveKit SIP `CreateSIPParticipant` 发起真实外呼。

没有真实 outbound SIP 线路时，可以在运营台使用 MicroSIP 模拟测试：

1. 在“通话队列”点击“拨号”，状态进入 `dialing`。
2. 点击“模拟接听”，状态进入 `active`，等价于 MicroSIP 接通。
3. 点击“模拟挂断”，状态进入 `completed`，统计数据会随刷新更新。
4. 也可以点击“无人接听”或“忙线”，验证失败分支和统计。

该模拟只覆盖运营台业务流程和状态机，不产生真实 SIP 信令或 RTP 音频。真实语音链路仍需 MicroSIP 拨入 `sip:1000@127.0.0.1:5065`，或配置 outbound SIP trunk 后由 `/api/calls/{call_id}/dial` 发起真实外呼。

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
- 会话 VAD 显式使用 Silero，并在每个 LiveKit Job 进程中预加载一次。
- Turn Detector 使用 `livekit/turn-detector` 多语言文本模型；同一 Worker 的并发
  会话通过 LiveKit 共享推理执行器运行，默认不会把转写文本发送到远端 EOT 服务。
- Endpointing 使用动态模式：`min_delay=0.5`、`max_delay=3.0`、`alpha=0.9`。
- Qwen TTS 使用 DashScope SSE 增量音频输出。
- 系统提示词要求回答尽量简短，减少 TTS 合成和播放时间。

可选环境变量：

```env
QWEN_USE_REALTIME_ASR=false
QWEN_TURN_DETECTION_MODE=vad
QWEN_LLM_WARMUP=false
QWEN_TTS_USE_SSE=false
```

`QWEN_TURN_DETECTION_MODE=vad` 仅用于模型故障时的显式降级；生产默认值是
`multilingual`。不要设置 `LIVEKIT_REMOTE_EOT_URL`，代码会拒绝该配置，以免通话
转写被意外发送到外部 Turn Detector 服务。Docker Agent 镜像在构建阶段自动执行
`python -m livekit.agents download-files`，并把模型保存在不会被运行时缓存卷覆盖的
`/app/models/huggingface`。

当前固定的 `livekit-plugins-turn-detector==1.6.6` 与工程的 LiveKit Agents 版本一致。
插件代码采用 Apache-2.0；模型权重采用
[LiveKit Model License](https://huggingface.co/livekit/turn-detector/blob/main/LICENSE)，
上线前需由交付方完成许可审核。该文本插件已被 LiveKit 标记为 deprecated，但仍是
当前版本中避开内置音频 Turn Detector、使用可检查 ONNX 文本模型的兼容方案。
`livekit-local-inference` 仍是 `livekit-agents` 的强制传递依赖，因此其二进制文件仍会
安装，但本工程的 VAD 和 Turn Detection 调用链不再选择它的默认模型。

## GitHub 提交说明

本仓库提交的是当前语音电话服务端工程代码。运行时依赖通过 `requirements.txt`、Docker 镜像和 MicroSIP 工具提供，不需要提交本地克隆的 LiveKit 上游源码仓库。
