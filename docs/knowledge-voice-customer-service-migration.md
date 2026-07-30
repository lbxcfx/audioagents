# 企业知识语音客服兼容迁移与边界审计

更新时间：2026-07-30

## 审计结论

- 外呼继续由 `commercial-agent`、`server/cloud_parity` 和 `/api` 命名空间承载；本次不修改其表、调度器、Worker 名称或状态机。
- 呼入已使用独立 `inbound-control`、`tenant-voice-agent`、`/inbound-api` 和默认关闭的 `INBOUND_AGENT_SYSTEM_ENABLED`，适合作为知识客服唯一接入点。
- 项目成员关系和 `agent.read` / `agent.write` 权限是现有租户边界。知识库所有查询必须同时带服务端解析的 `project_id`，不能只凭知识库 ID。
- Agent 发布版本是不可变快照。知识绑定保存知识库 ID；正在通话的会话继续使用准入时固定的 Agent 版本。
- Worker 身份采用短时 JWT 和固定 scope。知识检索使用新增的 `knowledge:read`，公开体验 Worker 不获得该 scope。
- 生产数据层为 PostgreSQL；SQLite 只作为测试适配器。知识表采用现有数据库抽象和仅向前迁移，不影响外呼表。

## 已落地的兼容切口

新增 `inbound_knowledge_bases`、`inbound_knowledge_documents` 和
`inbound_knowledge_chunks`，每层均保存 `project_id`。企业控制面支持知识库创建、
TXT/Markdown 文本索引、带来源的检索测试，以及企业 Agent 的知识库绑定校验。
公开体验仍强制清空知识和工具；业务工具仍保持关闭。

当前检索实现是可运行的轻量词项检索，用于先验证权限、引用契约和端到端接线。
在生产放量前，应将文档解析和 embedding 移到隔离任务 Worker，并将原文件与解析结果
写入独立 MinIO bucket；向量索引可替换检索实现，但不得改变项目级过滤和引用响应契约。

## 迁移顺序与回滚

1. 先部署新增表和控制面，保持总功能开关关闭。
2. 导入单个测试项目知识，验证跨项目访问拒绝和检索引用。
3. 给 enterprise Worker 增加 `knowledge:read`，公开 Worker 保持原 scope。
4. 发布绑定知识库的新 Agent 版本，只把测试入口切到新版本。
5. 通过文本与语音 E2E 后逐项目灰度；失败时将入口切回旧版本，无需删除知识表。

## 后续上线门槛

- PDF/DOCX 安全解析、文件魔数/大小/压缩炸弹检查、异步状态与失败重试。
- 对象存储、embedding/rerank、索引版本、灰度发布和回滚。
- Worker Function Calling、引用卡片数据消息、未命中策略及会话审计。
- 并发、提示注入、跨租户和外呼全量回归测试。
