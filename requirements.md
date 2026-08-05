# 阿里云单机双并发外呼测试系统需求

## 1. 文档目的

本文定义在一台阿里云 ECS 上部署本工程，并通过商用语音平台前端完成 SIP 出局线路配置、测试号码导入和两路并发智能外呼验收的最低软件需求。

本阶段目标是验证完整业务链路，不是建设高可用生产集群。测试通过后，再根据真实并发、可用性目标和合规要求迁移到 Kubernetes、PostgreSQL HA、企业 OIDC、集中监控和合规录音架构。

## 2. 目标与范围

### 2.1 必须实现

1. 在单台阿里云 ECS 上使用 Docker Compose 启动全部测试组件。
2. 前端与控制面同源访问，前端可创建项目并配置 LiveKit SIP 线路。
3. 前端可导入 CSV/JSON 联系人、跨页选择联系人并创建外呼活动。
4. Dispatcher 能可靠领取任务，并通过 LiveKit Agent Dispatch 调度指定 Phone Agent。
5. Phone Agent 能通过 LiveKit Outbound SIP Trunk 向两个已授权测试号码拨号。
6. 两通电话能够同时处于 `ringing` 或 `active` 状态，且每通电话均可完成双向音频和 AI 对话。
7. 控制面能够展示呼叫状态、活动进度、终态、失败原因和 CDR。
8. 重复请求、任务领取重试或组件短暂故障不得造成同一业务任务重复拨号。
9. 所有容器具备健康检查、自动重启、持久化卷和有界日志。

### 2.2 本阶段不要求

- Kubernetes、ACR 企业版和多节点容器编排。
- PostgreSQL HA、跨可用区容灾、PITR 和自动故障切换。
- 企业 OIDC、KMS/Vault、WAF 和公网多租户开放。
- LiveKit Egress、OSS/S3 录音存储和录音回放。
- 多地域部署、运营商线路自动采购和超过两路的容量承诺。
- 面向真实客户的商业投产和 SLA。

本阶段必须将项目录音策略设置为 `recording_mode=off`。如启用录音，必须另行部署 LiveKit Egress、对象存储、录音告知和合规保留策略，不属于本需求范围。

## 3. 测试环境条件

### 3.1 ECS 推荐规格

| 项目 | 最低建议 | 推荐配置 |
| --- | --- | --- |
| CPU | 4 vCPU | 8 vCPU |
| 内存 | 8 GiB | 16 GiB |
| 系统盘 | 80 GiB SSD | 100 GiB 或以上 SSD |
| 公网带宽 | 5 Mbps | 10 Mbps 或以上 |
| 操作系统 | 受支持的 64 位 Linux | Ubuntu LTS |
| 公网地址 | 固定公网 IPv4 | 固定 EIP |

4 vCPU/8 GiB 仅作为可运行下限。由于 PostgreSQL、Redis、LiveKit、LiveKit SIP、控制面、Dispatcher、Phone Agent 和前端均运行在同一台机器上，验收环境优先使用 8 vCPU/16 GiB。

### 3.2 外部前置条件

部署前必须获得：

1. 可从 ECS 公网地址接入的真实 SIP 出局线路。
2. 运营商 SIP 地址、传输协议、鉴权方式、信令来源 IP 和 RTP 来源 IP。
3. 至少两个并发通话额度；如要求近似同时发起，需要至少 2 CPS，否则允许以 1 CPS 间隔发起后形成两路并发。
4. 至少一个合法且允许透传的 E.164 主叫号码。
5. 两个已授权接听测试的 E.164 被叫号码。
6. 可用的 DashScope API Key，以及本工程配置的 ASR、LLM、TTS 模型访问权限。
7. 管理员固定公网 IP，或能够使用 SSH 本地端口转发。

运营商是否允许互联网 SIP、是否要求专线/IP 白名单、允许的主叫号码和地域拨打范围是测试能否成功的首要外部条件。

## 4. 单机软件架构

必须提供 `docker-compose.ecs.yml`，至少包含以下服务：

| 服务 | 职责 | 副本 |
| --- | --- | --- |
| `voice-console` | React 静态前端及 `/api/` 反向代理 | 1 |
| `cloud-parity` | FastAPI 控制面、项目、线路、联系人、活动和呼叫 API | 1 |
| `postgres` | PostgreSQL 16 测试数据库 | 1 |
| `redis` | LiveKit 单机状态和任务协调 | 1 |
| `livekit` | 房间、参与者、Agent Dispatch 和媒体控制 | 1 |
| `livekit-sip` | SIP 信令、运营商 RTP 和 LiveKit SIP Participant | 1 |
| `telephony-dispatcher` | 活动物化、容量领取、Dispatch 和对账 | 1 |
| `phone-agent` | ASR、对话、LLM、TTS 和 `CreateSIPParticipant` | 1 |

### 4.1 Compose 要求

1. 所有镜像必须使用固定版本或提交哈希标签，不得使用 `latest`。
2. 所有应用服务必须设置 `restart: unless-stopped` 或等价策略。
3. PostgreSQL 数据目录必须挂载持久化卷。
4. 控制面数据、必要缓存和有限日志必须挂载到明确目录，不得写入临时容器层后丢失。
5. PostgreSQL、Redis、控制面、Dispatcher 和 Agent 必须具备健康检查。
6. Dispatcher 必须在控制面 Ready、LiveKit 可用后启动；Agent 必须在 LiveKit 可用后启动。
7. 容器日志必须配置轮转上限，防止填满系统盘。
8. PostgreSQL、Redis、控制面内部端口、Dispatcher 健康端口和 Agent 健康端口不得暴露到公网。
9. 前端通过 Docker 内部网络代理控制面，实现浏览器同源访问。

## 5. 网络与安全组

### 5.1 允许的公网入口

| 端口 | 协议 | 来源限制 | 用途 |
| --- | --- | --- | --- |
| 22 | TCP | 仅管理员固定 IP | SSH 和端口转发 |
| 80/443 | TCP | 测试期仅管理员固定 IP，可不开放 | 前端管理入口 |
| 5065 | UDP/TCP | 仅运营商信令 IP | SIP 信令 |
| 10000-10100 | UDP | 仅运营商 RTP IP | SIP 媒体 |

LiveKit RTC/TURN 端口是否开放由最终容器网络模式决定。对于所有组件同机且不开放浏览器 WebRTC 的本测试，应优先通过主机网络或受控 Docker 网络完成内部通信，避免无必要的公网暴露。

### 5.2 禁止公网暴露

- PostgreSQL `5432`
- Redis `6379`
- 控制面 `8091`
- Dispatcher `9091`
- Phone Agent `18081`

管理前端优先绑定到 `127.0.0.1`，通过以下形式的 SSH 隧道访问：

```bash
ssh -L 8090:127.0.0.1:8080 <user>@<ecs-public-ip>
```

浏览器访问 `http://127.0.0.1:8090`。如必须直接通过公网访问前端，则必须增加 HTTPS、强认证和精确来源限制，不得公开开发身份头认证。

## 6. LiveKit 与 SIP 要求

### 6.1 LiveKit 基础配置

1. `LIVEKIT_API_KEY` 和 `LIVEKIT_API_SECRET` 必须使用测试环境独立的高强度随机值，不得使用 `devkey/secret`。
2. `LIVEKIT_URL` 在容器内部使用可解析的 LiveKit 服务地址；对外使用时必须与实际域名、协议和证书一致。
3. LiveKit 必须正确识别 ECS 外部 IP，不能向远端参与者发布 `127.0.0.1`。
4. LiveKit、SIP、Dispatcher 和 Agent 必须使用完全一致的 API Key/Secret。
5. Agent 必须以显式名称 `commercial-agent` 注册。

### 6.2 SIP 公网/NAT 配置

现有本地启动脚本将以下字段配置为 `127.0.0.1`，只能用于本地 MicroSIP 测试，不能直接用于 ECS：

- `LIVEKIT_NODE_IP`
- `sip_hostname`
- `nat_1_to_1_ip`
- `media_nat_1_to_1_ip`
- `use_external_ip`

ECS 配置必须使用实际固定 EIP或正确的外部 IP 发现模式，并确保 SIP SDP 中发布的信令和媒体地址可被运营商访问。部署验收必须检查 SIP INVITE/200 OK/ACK、SDP 地址以及双向 RTP，不能只以“电话振铃”作为音频链路成功标准。

### 6.3 Outbound Trunk

1. 必须在 LiveKit 创建真实 Outbound SIP Trunk，而不是复用本地呼入测试 Trunk。
2. 运营商口令、Token 或 IP 鉴权信息只保存在 LiveKit Secret/配置中，不写入前端、联系人或活动 metadata。
3. 创建成功后取得 `ST_...` 格式的 LiveKit Trunk ID。
4. 前端“线路与策略”保存该 Trunk ID、供应商、合法主叫号码、并发和 CPS。
5. 平台线路状态必须为 `active`，方向必须为 `outbound` 或 `bidirectional`。
6. 活动填写的主叫号码必须属于线路允许号码，并被运营商授权。

应提供一个只读取 Secret 的出局 Trunk 初始化或校验脚本，支持幂等创建、列出和健康检查，不在日志中输出运营商密码。

## 7. 应用与环境变量

### 7.1 控制面

单机测试使用 PostgreSQL，但保持开发环境身份模式：

```dotenv
CLOUD_PARITY_ENV=development
CLOUD_PARITY_AUTH_MODE=development
CLOUD_PARITY_DATABASE_URL=postgresql://<user>:<password>@postgres:5432/cloud_parity
CLOUD_PARITY_DB_POOL_MIN_SIZE=1
CLOUD_PARITY_DB_POOL_MAX_SIZE=10
CLOUD_PARITY_CORS_ALLOWED_ORIGINS=
```

数据库密码、LiveKit Secret、DashScope Key、Fernet Key 和其他凭据必须放入 ECS 本地的未跟踪 `.env` 或 Docker Secret，不得写入 Git 仓库、镜像层、命令行参数或日志。

### 7.2 前端

测试镜像允许显示开发登录入口：

```text
VITE_ALLOW_DEVELOPMENT_AUTH=true
VITE_ENABLE_LEGACY_REPLICA=false
VITE_ALLOW_API_OVERRIDE=false
```

该配置只允许在管理入口未公开公网、仅通过 SSH 隧道或固定 IP 白名单访问时使用。

### 7.3 Dispatcher

```dotenv
CLOUD_PARITY_CONTROL_URL=http://cloud-parity:8091
CLOUD_PARITY_TELEPHONY_PROJECT_IDS=<project-uuid>
CLOUD_PARITY_SERVICE_USER_ID=telephony-worker
CLOUD_PARITY_TELEPHONY_CLAIM_BATCH=2
CLOUD_PARITY_TELEPHONY_PROJECT_CONCURRENCY=1
CLOUD_PARITY_TELEPHONY_POLL_SECONDS=0.5
CLOUD_PARITY_TELEPHONY_HEALTH_PORT=9091
```

`telephony-worker` 必须在目标项目中具有 `worker` 角色。Dispatcher 和 Agent 使用的项目 UUID 必须与前端当前项目一致。

### 7.4 Phone Agent

```dotenv
QWEN_AGENT_EXPLICIT_NAME=commercial-agent
LIVEKIT_AGENT_NAME=commercial-agent
QWEN_AGENT_PORT=18081
QWEN_AMD_ENABLED=true
QWEN_RECORDING_S3_BUCKET=
```

Agent 必须使用可用的 DashScope Key、模型名称和内部控制面地址。两路并发测试期间不得启用强制录音。

## 8. 前端业务流程

### 8.1 线路配置

前端必须允许 Owner/Admin：

1. 创建或选择测试项目。
2. 在“线路与策略”中填写线路名称、方向、状态、供应商和 LiveKit Trunk ID。
3. 填写允许的 E.164 主叫号码。
4. 将线路并发设置为 `2`。
5. 将线路 CPS 设置为运营商允许值；近似同时发起两通电话时设置为 `2`。
6. 保存后在线路清单中显示相同配置。
7. 重启控制面后线路配置仍然存在。

### 8.2 容量与合规

测试项目配置：

```text
总并发：2
外呼并发：2
线路并发：2
活动并发：2
每分钟呼叫：不低于 10
录音模式：off
Agent 名称：commercial-agent
```

只允许使用已授权号码。测试可以为两个号码记录有效同意证据，也可以在封闭测试项目中按批准方案关闭强制同意校验；不得利用测试配置向未授权客户拨号。DNC 和 `suppressed` 联系人必须继续被拦截。

### 8.3 联系人与活动

1. CSV 至少包含 `external_id,phone_number`，号码使用 E.164。
2. 导入两个状态为 `active` 的授权联系人。
3. 在联系人列表中选择两位联系人。
4. 创建活动，选择有效出局线路和合法主叫号码。
5. `max_concurrent_calls` 设置为 `2`。
6. 选择立即启动，或创建后手动启动。

## 9. 可观测性与运维

单机测试至少提供：

1. `cloud-parity` 的 `/api/platform/health/live` 和 `/api/platform/health/ready`。
2. Dispatcher 的 `/live` 和 `/ready`，仅限 Docker 网络或本机访问。
3. Phone Agent 的 `/worker`，仅限 Docker 网络或本机访问。
4. LiveKit 和 SIP 容器健康状态。
5. 一条命令查看所有服务状态和最近日志。
6. 日志不得打印完整电话号码、数据库 URL、SIP 密码、LiveKit Secret、DashScope Key 或租约 Token。
7. 容器日志轮转建议设置单文件不超过 20 MiB、保留不超过 5 个文件。
8. 一个部署前检查脚本，验证端口、安全组提示、EIP、配置完整性、控制面 Ready、Dispatcher Ready、Agent 在线和有效 Outbound Trunk。

## 10. 验收标准

只有以下项目全部通过，单机两并发测试才算成功：

### 10.1 部署验收

- `docker compose ps` 中所有必需服务为 running/healthy。
- ECS 重启或 Docker 服务重启后，所有服务能够自动恢复。
- PostgreSQL 数据卷在容器重建后保留项目、线路、联系人和活动。
- 管理端口未对公网开放，安全组仅允许管理员和运营商必要来源。

### 10.2 单通电话验收

- 前端创建单通外呼后，任务依次进入 `queued`、`dispatching`、`dialing/ringing`、`active` 和终态。
- 被叫显示授权主叫号码。
- 双方均能听到对方声音，无单向音频和明显持续丢包。
- AI 能播放开场白、识别测试人员语音并返回 TTS。
- 任一方挂断后，通话在合理时间内收敛到终态并生成 CDR。

### 10.3 两路并发验收

- 导入并选中两个授权联系人。
- 同一活动的并发上限为 2，项目和线路外呼并发均为 2。
- 两个呼叫在同一时间窗口内同时处于 `ringing` 或 `active`。
- 两通电话均能独立完成 ASR、LLM、TTS，不串音、不串 Session、不串联系人数据。
- 活动进度最终为 `2/2` 终态，结果与实际接听情况一致。
- 同一联系人/活动任务没有重复外呼。
- 暂停活动后不再领取新的呼叫；取消活动后未开始任务不会拨号。

### 10.4 故障与边界验收

- 错误 Trunk ID、非法主叫号码和停用线路被明确拒绝。
- 运营商忙线、无人接和拒接能够进入相应终态。
- Dispatcher 在创建 Dispatch 结果不确定时进入对账，不盲目重拨。
- DNC、无有效同意（启用强制同意时）和 `suppressed` 联系人不会产生运营商拨号副作用。

## 11. 上云实施前必须补齐的工程交付

在阿里云 ECS 拉取代码并部署前，应在后续开发中完成并提交：

1. `docker-compose.ecs.yml`。
2. 不含真实凭据的 `.env.ecs.example`。
3. LiveKit ECS 配置模板和公网 IP/NAT 参数说明。
4. LiveKit SIP ECS 配置模板，移除所有 `127.0.0.1` SDP/NAT 硬编码。
5. 幂等的 Outbound SIP Trunk 创建/校验脚本。
6. `deploy-ecs.sh`、`stop-ecs.sh`、`status-ecs.sh` 和配置预检脚本。
7. 两联系人 CSV 示例。
8. 两路并发端到端验收脚本和人工验收记录模板。
9. 单机部署、回滚、数据备份和故障排查文档。

上述文件应以安全默认值为原则，不得把真实 `.env`、API Key、运营商密码、数据库卷、录音、缓存或日志提交到 GitHub。

## 12. Go/No-Go 门禁

满足以下条件才允许发起真实测试外呼：

- ECS 使用固定公网 IP，SIP/SDP/RTP 地址已验证。
- 运营商确认该公网 IP、主叫号码、并发和目标地域已授权。
- LiveKit Outbound Trunk 可用且 ID 已登记到平台。
- `commercial-agent` 在线，Dispatcher Ready。
- 两个被叫号码均由测试参与者明确授权。
- 录音关闭，或已经另行完成录音合规审批和 Egress/存储配置。
- 安全组和管理入口已经按本文限制。

任一项不满足均为 No-Go，不得向真实客户号码发起外呼。

## 13. 部署所需输入清单

开始阿里云部署前，实施人员需要获得以下信息，但不得将真实值提交到仓库：

1. ECS 公网 IP、SSH 用户和管理员来源 IP。
2. 运营商名称、SIP 地址、协议、鉴权方式、信令/RTP IP 白名单。
3. 授权主叫号码、线路并发和 CPS。
4. 两个授权测试被叫号码。
5. DashScope API Key 和已开通模型。
6. 可选域名和 TLS 证书；仅使用 SSH 隧道时可暂不提供。
7. 项目名称、项目 UUID 和测试服务身份名称。
