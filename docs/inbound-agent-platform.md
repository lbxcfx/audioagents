# 智能呼入 Agent 平台开发与上线方案

## 1. 目标与边界

在不修改现有智能外呼页面、外呼 API、任务调度器、外呼 Agent 和外呼状态机的前提下，新增两类能力：

1. **公开体验 Agent**：访客可通过公开电话号码或浏览器实时语音入口，与平台维护的公共 Agent 进行一次隔离、限时的体验会话。
2. **企业专属 Agent**：企业在项目空间内配置提示词、声音、知识、工具权限和接入号码，发布不可变版本后承接该企业的呼入会话。

系统不接入 Grok API。LiveKit 负责 SIP、WebRTC、房间、媒体传输和 Agent 调度；语音识别、语言模型和语音合成第一阶段复用现有 Qwen/DashScope 链路，后续允许切换为本地开源模型。

### 硬性约束

- 现有外呼前后端代码语义与接口保持不变。
- 入站能力使用独立命名空间、独立服务、独立 Worker 名称和独立功能开关。
- 企业数据必须按 `project_id` 强隔离；公开体验不得获得企业数据或高风险工具权限。
- Agent 配置发布后形成不可变快照，正在进行的会话不受后续编辑影响。
- 所有外部入口必须具备身份校验、限流、并发上限、审计与可回滚能力。

## 2. 总体架构

```text
公开电话号码 ─┐
企业专属号码 ─┼─> SIP 运营商 ─> LiveKit SIP ─> Dispatch Rule ─┐
浏览器语音入口 ┘              LiveKit Room ──────────────────┤
                                                               v
                                                    inbound-agent-worker
                                                               |
                          ┌────────────────────────────────────┼──────────────┐
                          v                                    v              v
                    inbound-control                     Qwen 实时语音链路   会话/审计
                  配置、版本、绑定、限额                ASR + LLM + TTS    指标/录音
                          |
                     PostgreSQL / Redis
```

### 新增部署单元

- `inbound-control`：独立 FastAPI 控制面，暴露 `/inbound-api`，负责配置、发布、入口绑定、限流、运行时解析和会话管理。
- `inbound-agent-worker`：独立 LiveKit Agent Worker，仅接收 `public-demo-agent` 和 `tenant-voice-agent` 任务。
- `inbound-web`：智能呼入体验和企业配置前端；视觉语言沿用现有外呼控制台。
- `inbound-dispatch-sync`：幂等同步 SIP Trunk、号码绑定与 LiveKit Dispatch Rule。

通过反向代理挂载新入口，不要求修改现有外呼业务：

```text
/app/*                    当前外呼前端
/experience/*             公开体验
/app/inbound/*            企业智能呼入
/api/*                    当前后端
/inbound-api/*            新入站控制面
```

## 3. 产品模型

### 3.1 公开体验 Agent

- 平台只维护一个公开配置版本，访客不会创建自己的 Agent。
- 每位访客、每通电话创建独立 LiveKit Room 和独立会话上下文。
- 默认单次 3 分钟，可配置为 1～10 分钟。
- 按主叫号码哈希、来源 IP 和自然日限制次数与总时长。
- 禁止任意外部工具、企业知识库、转账、修改数据等高风险动作。
- 开场明确告知 AI 身份；启用录音时必须先播报录音说明。
- 只保留脱敏数据，采用短保留周期。

### 3.2 企业专属 Agent

- 每个 Agent 属于唯一项目；管理员可编辑草稿并发布不可变版本。
- 企业号码或浏览器入口绑定到明确的已发布版本。
- Worker 通过服务身份读取运行时配置，Dispatch Metadata 只携带不透明 ID。
- 默认共享横向扩容的多租户 Worker；高合规客户可选择独立 Worker 或独立 LiveKit 实例。
- 工具调用采用项目级白名单和独立凭证，Worker 不接受浏览器直接指定工具或项目。

## 4. 核心调用链

### 4.1 电话呼入

1. SIP 运营商将来电送至 LiveKit Inbound Trunk。
2. Dispatch Rule 根据被叫号码创建唯一房间并显式调度命名 Agent。
3. `inbound-agent-worker` 校验签名 Metadata，调用准入接口。
4. 控制面检查绑定状态、版本状态、项目状态、并发、使用额度和防滥用策略。
5. Worker 获取不可变运行时快照，启动 Qwen `AgentSession`。
6. 事件、用量和结束原因以幂等方式写入会话记录。
7. 录音、转写和摘要按项目保留策略异步归档。

### 4.2 浏览器体验

1. 浏览器向控制面申请短时 WebRTC Session。
2. 控制面执行 IP 限流、挑战校验和全局容量检查。
3. 后端生成唯一房间名、短时 LiveKit Token，并创建显式 Agent Dispatch。
4. 浏览器只能加入服务端分配的房间，不能指定项目、版本或 Agent Name。

## 5. 数据模型

### `inbound_agents`

- `id`, `project_id`, `kind(public_demo|enterprise)`
- `name`, `description`, `status(draft|published|disabled)`
- `active_version_id`, `created_by`, `created_at`, `updated_at`

### `inbound_agent_versions`

- `id`, `agent_id`, `project_id`, `revision`
- `config_json`, `config_sha256`, `published_by`, `published_at`
- 唯一约束：`(agent_id, revision)`

### `inbound_agent_bindings`

- `id`, `project_id`, `agent_id`, `agent_version_id`
- `entry_type(sip_did|web)`, `destination`, `trunk_id`
- `dispatch_rule_id`, `status`, `created_at`, `updated_at`
- 一个启用中的入口只能绑定一个版本。

### `inbound_agent_sessions`

- `id`, `project_id`, `agent_id`, `agent_version_id`
- `entry_type`, `room_name`, `provider_call_id`
- `caller_hash`, `caller_last4`, `status`
- `started_at`, `ended_at`, `duration_seconds`, `termination_reason`
- `recording_ref`, `transcript_ref`, `retention_until`

### `public_demo_usage`

- `subject_hash`, `usage_date`, `call_count`, `total_seconds`, `blocked_until`

## 6. API

### 公开接口

```text
GET  /inbound-api/public/demo
POST /inbound-api/public/demo/web-sessions
GET  /inbound-api/public/demo/sessions/{session_id}
```

### 企业接口

```text
GET    /inbound-api/projects/{project_id}/agents
POST   /inbound-api/projects/{project_id}/agents
GET    /inbound-api/projects/{project_id}/agents/{agent_id}
PATCH  /inbound-api/projects/{project_id}/agents/{agent_id}
POST   /inbound-api/projects/{project_id}/agents/{agent_id}/publish
POST   /inbound-api/projects/{project_id}/agents/{agent_id}/bindings
DELETE /inbound-api/projects/{project_id}/agents/{agent_id}/bindings/{binding_id}
POST   /inbound-api/projects/{project_id}/agents/{agent_id}/test-sessions
GET    /inbound-api/projects/{project_id}/agents/{agent_id}/sessions
GET    /inbound-api/projects/{project_id}/agents/{agent_id}/analytics
```

### Worker 内部接口

```text
POST /inbound-api/internal/calls/admit
GET  /inbound-api/internal/runtime/{version_id}
POST /inbound-api/internal/sessions/{session_id}/events
POST /inbound-api/internal/sessions/{session_id}/complete
```

内部接口使用短时服务 JWT 或双向 TLS，严禁仅凭 `project_id` 访问配置。

## 7. 前端信息架构

### 公开体验页

- 首屏：简洁说明“打一个电话，体验自然对话”，展示公开号码、扫码拨号和浏览器体验入口。
- 工作方式：来电、理解、回答、形成记录四步轻量说明。
- 隐私与边界：AI 身份、时长限制、数据保存、禁止事项清晰可见。
- 实时体验：连接前、连接中、重连、静音、结束、额度耗尽和服务繁忙状态完整。

页面逻辑借鉴成熟语音 Agent 产品的低门槛体验方式，但不复制 Grok 的品牌、图形或文案。

### 企业智能呼入

- Agent 列表：状态、当前版本、绑定号码、最近会话、健康状态。
- Agent 编辑：基本信息、角色与目标、欢迎语、声音、知识、工具、转人工、录音和限制。
- 发布：配置校验、差异预览、发布确认、版本历史和一键回滚。
- 入口绑定：号码、Trunk、Dispatch 状态和测试呼叫。
- 会话：筛选、摘要、意图、录音、转写、结束原因和审计。

视觉上复用现有外呼控制台的色板、圆角、间距、表单和状态组件，导航中将“智能呼入”作为与“智能外呼”并列的一级业务域。

## 8. 安全与合规

- Metadata 使用签名或只携带不透明 ID；禁止携带密钥、完整 Prompt 和明文电话。
- 企业运行时配置必须同时校验服务身份、`project_id` 和版本归属。
- 公开入口实行 IP、号码哈希、设备和全局四级限流。
- 每个 Agent 配置工具白名单、参数 Schema、超时、审计和熔断。
- 会话、录音、转写执行项目保留策略；公开体验使用更短保留期。
- 录音前播报披露文案；未完成披露不得启动录音。
- 所有创建、编辑、发布、绑定、回滚和敏感数据访问写入审计日志。
- 生产环境只允许 PostgreSQL，不允许 SQLite；密钥来自 Secret Manager 或环境注入。

## 9. 可观测性与容量

关键指标：

- 呼入请求、接通率、准入拒绝率、房间创建失败率。
- 首包语音延迟、ASR/LLM/TTS 分段延迟、打断成功率。
- 活跃会话、Worker 负载、队列等待、异常断开和重连。
- 每项目与公开体验的分钟数、模型用量和估算成本。
- Dispatch Rule 同步漂移、版本解析失败和越权拒绝。

生产告警必须区分公开体验和企业流量，避免体验流量挤占企业容量。Worker 采用独立队列和并发阈值，按会话数横向扩容。

## 10. 质量门与测试矩阵

每个模块必须依次通过：实现者自测、独立代码评审、安全评审、产品/可用性评审和回归测试。

### 后端

- 单元：配置校验、版本不可变、绑定唯一性、限流、签名、状态机和幂等。
- 集成：SQLite 测试适配器与 PostgreSQL 生产适配器行为一致。
- 隔离：跨项目读取、更新、发布、绑定和会话查询全部返回拒绝。
- 并发：重复发布、重复准入、重复完成事件不会产生脏数据。

### Worker 与 LiveKit

- Metadata 篡改、未知版本、禁用 Agent、容量耗尽和控制面超时均安全失败。
- 一通电话一个房间；多企业并发不会加载错误配置。
- Worker 重启、网络抖动、重复 Webhook 和 SIP 异常结束可恢复或正确收敛。

### 前端

- 桌面、平板和移动端响应式布局。
- 键盘可操作、焦点可见、表单标签完整、状态使用 `aria-live`。
- 空态、加载、失败、无权限、服务繁忙和发布冲突均有明确反馈。
- 生产构建、TypeScript、Playwright 主流程和视觉回归通过。

### 外呼回归

- 现有外呼 API 契约、调度器测试、外呼 Agent Metadata 测试、商业前端 E2E 和生产构建必须保持通过。

## 11. 发布与回滚

1. 所有能力受 `INBOUND_AGENT_SYSTEM_ENABLED=false` 控制，默认关闭。
2. 先部署数据库增量表，再部署控制面和 Worker，最后开放前端入口。
3. 公开体验先白名单和小并发灰度，企业按项目逐一启用。
4. Worker、Dispatch Rule 和前端均可独立回滚；数据库迁移只做向前兼容，不在紧急回滚时删除数据。
5. 停用时先关闭新准入，等待活跃会话排空，再停止 Worker。

## 12. 开发阶段

1. **架构与数据层**：独立服务骨架、迁移、配置、鉴权、版本和绑定。
2. **运行链路**：准入、Runtime Resolver、LiveKit Dispatch、Worker 与会话状态机。
3. **公开体验**：电话与 WebRTC 入口、限流、时长、隐私说明和体验页。
4. **企业能力**：Agent 编辑、发布、号码绑定、测试会话、记录与分析。
5. **工业化**：HA、监控、压测、安全测试、灾备、灰度和运维手册。

每个阶段通过质量门后方可进入下一阶段。

## 13. 当前实现状态与安全降级

截至本次开发，代码已落地以下独立模块：

- `server/inbound_control`：Agent 草稿、不可变发布版本、版本激活/回滚、入口绑定/切版/停用、公开配额、会话、分析、SIP 准入和短时 Worker JWT。
- `agent/inbound_agent.py`：公开与企业共用镜像、按 Agent Name 分池部署、Qwen Realtime、远端音频、欢迎语、硬时长和幂等完成。
- `scripts/sync-inbound-dispatch.py`：为每个 SIP DID 创建 Individual Room Dispatch，使用静态 `sip_inbound` 类型标识；项目和版本由控制面依据真实 Trunk/DID 解析，不把短时 Web Token 写入长期 Rule。
- `app/src/InboundExperience.tsx`：公开 WebRTC 体验、远端音轨播放、麦克风控制、重连、服务端状态和额度反馈。
- `app/src/InboundConsole.tsx`：企业 Agent 配置、发布、入口、版本切换和会话记录。
- Kubernetes、NetworkPolicy、Secret 示例、开发 Compose Profile 和 GitHub Actions 质量门。

为避免“界面存在但运行时不安全”的情况，以下能力在当前版本中明确 **fail-closed**：

- 录音固定为 `off`。完成披露确认、Egress、私有存储、短签访问和联合删除前，API 不接受录音配置。
- 工具和企业知识源固定为空。完成项目归属校验、Secret 引用、SSRF 防护、受控出口和执行审计前，API 不接受相关配置。
- 整套入站系统默认 `INBOUND_AGENT_SYSTEM_ENABLED=false`；未显式开启时，公开、企业和内部业务入口统一返回 503，Worker 拒绝启动。
- 公开体验同时执行来源日配额和 `INBOUND_PUBLIC_MAX_CONCURRENT` 全局容量门；PostgreSQL 使用事务级互斥，避免多控制面副本并发超发。
- WebRTC 准入先创建 5 分钟有效的预约；未接通预约自动过期。内部维护接口可回收遗留预约及异常超长的 active 会话，运维系统应定时调用并监控回收数量。
- Public、Enterprise、Dispatch Sync 与 Maintenance 使用彼此独立的工作负载密钥；控制面按 subject 固定 Scope 白名单，拒绝 Worker 自声明提权。Kubernetes 示例已拆分 Secret，并为回收和 Dispatch 同步提供最小权限 CronJob。

这三项属于有意的生产安全边界，不应通过前端绕过。后续开放时必须新增数据库迁移、威胁模型和对应质量门。
