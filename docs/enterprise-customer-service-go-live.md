# 企业知识语音与视频客服上线手册

更新时间：2026-07-30

## 部署顺序

1. 备份 PostgreSQL，部署仅向前兼容的新表；保持 `INBOUND_AGENT_SYSTEM_ENABLED=false`。
2. 配置独立的 metadata、Worker identity 与工具加密密钥。公开 Worker 不授予知识和工具 scope。
3. 部署 control、enterprise/public Worker 和前端，检查 `/inbound-api/health/ready`。
4. 为测试项目创建知识库、工具连接、已审核素材和 Agent 发布版本，再创建 Web 绑定。
   Agent 发布时会固化知识文档 ID 快照；知识更新后发布新的 Agent 版本进行灰度，回滚旧 Agent 版本会同时恢复旧知识快照。
5. 在“体验与评测”中验证文本、语音、引用、工具拒绝/确认、摄像头和视觉降级。
6. 先开启企业白名单项目，再开启公开体验；视频功能使用独立 `INBOUND_VISION_ENABLED` 灰度。

## 必需配置

- `CLOUD_PARITY_DATABASE_URL`：生产 PostgreSQL。
- `INBOUND_METADATA_SECRET`、`INBOUND_TOOL_ENCRYPTION_KEY`：各自独立的至少 32 字符随机密钥。
- `INBOUND_WORKER_IDENTITIES_JSON`：固定 subject 和最小 scope。
- `LIVEKIT_URL/API_KEY/API_SECRET`、`DASHSCOPE_API_KEY`。
- `INBOUND_KNOWLEDGE_OBJECT_STORE_ENABLED=true` 及独立知识库 bucket 的 S3 endpoint/access/secret；bucket 必须保持私有。
- 应用 `inbound-network-policy.yaml` 前，为集群外 PostgreSQL、S3、DashScope、LiveKit 和数字人服务补充已审批的静态 CIDR；策略默认拒绝未知出口。
- `INBOUND_VISION_ENABLED=false`、`INBOUND_VISION_MODEL=qwen3.5-omni-flash`；验证隐私告知后才开启。

## 验收与告警

- 跨项目知识、素材、工具和会话访问必须返回拒绝或不可见。
- 同一幂等键不得重复执行工具；`confirm` 工具在确认闭环前不得执行。
- 视觉失败必须返回明确状态并继续语音；摄像头默认关闭且默认不录制视频。
- 监控准入拒绝、活跃会话、首包延迟、知识未命中、工具失败、视觉超时和 Worker 负载。
- 上线前执行 Python 全量测试、前端生产构建和 Playwright 的 inbound/account/商业外呼回归。
- 本次基线：Python `166 passed, 10 skipped`；前端生产构建通过；Inbound、账户和商业外呼 Playwright `19 passed`。跳过项是需要外部 PostgreSQL/实时服务的环境测试，生产演练仍需执行。

## 回滚

先关闭新准入并等待会话排空，然后回滚 Worker、control 和前端镜像。入口绑定可切回旧 Agent 版本；旧版本包含发布时的知识文档快照。新增数据库表保留，不做紧急逆向删除。视觉可单独关闭，不影响语音、知识或外呼链路。
