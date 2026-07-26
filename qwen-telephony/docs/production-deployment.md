# 生产部署手册

本手册部署应用侧的四个无状态组件：商用 Web Console、Cloud-Parity 控制面、电话 Dispatcher 和 LiveKit Agent。PostgreSQL、LiveKit Server/SIP/Egress、对象存储、企业 IdP、入口 TLS/WAF、Prometheus/Alertmanager 由目标基础设施提供。

## 1. 构建不可变镜像

后端三个 Dockerfile 的构建上下文是 `agent` 目录；前端镜像的上下文是 `app`。使用提交哈希或发布版本作为不可变标签，不要在生产使用 `latest`。
该目录的白名单式 `.dockerignore` 只允许运行源码、配置模板和锁文件进入 BuildKit，并明确排除 `config/local.env`、数据库、录音、日志、虚拟环境和测试产物；不要删除或放宽它。

```bash
cd /path/to/ai-login-replica/agent
VERSION=replace-with-release-version
REGISTRY=registry.example.com/qwen-telephony

docker build -f qwen-telephony/Dockerfile.control-plane -t "$REGISTRY/control-plane:$VERSION" .
docker build -f qwen-telephony/Dockerfile.dispatcher -t "$REGISTRY/dispatcher:$VERSION" .
docker build -f qwen-telephony/Dockerfile.agent -t "$REGISTRY/agent:$VERSION" .
docker push "$REGISTRY/control-plane:$VERSION"
docker push "$REGISTRY/dispatcher:$VERSION"
docker push "$REGISTRY/agent:$VERSION"

cd app
docker build -t "$REGISTRY/voice-console:$VERSION" \
  --build-arg VITE_OIDC_AUTHORIZATION_ENDPOINT=https://idp.example.com/oauth2/authorize \
  --build-arg VITE_OIDC_TOKEN_ENDPOINT=https://idp.example.com/oauth2/token \
  --build-arg VITE_OIDC_CLIENT_ID=voice-console \
  --build-arg VITE_OIDC_SCOPE='openid profile email offline_access' \
  --build-arg VITE_OIDC_REDIRECT_URI=https://voice.example.com/ .
docker push "$REGISTRY/voice-console:$VERSION"
```

所有镜像以非 root 用户运行，并提供编排健康探针。构建完成后应执行镜像扫描、生成 SBOM，并按组织策略签名。

## 2. 配置外部依赖

部署前必须具备：

- PostgreSQL 高可用实例，启用 TLS、备份/PITR 和最小权限应用账号。
- 可接收双向 SIP 的 LiveKit Server/SIP；开启录音时还需 LiveKit Egress 及受控 S3 兼容对象存储。Egress 写入优先使用工作负载身份。控制面当前的浏览器短时 URL 签名器使用独立、只读对象权限的 `QWEN_RECORDING_S3_ACCESS_KEY`/`QWEN_RECORDING_S3_SECRET`，只通过 Secret 注入，并设置 HTTPS `QWEN_RECORDING_S3_ENDPOINT`；不要复用 Egress 写入凭据。
- 企业 OIDC Issuer/Audience/JWKS 和 Dispatcher/Agent 的 Client Credentials 服务身份。Web SPA 客户端需启用 Authorization Code + PKCE、Refresh Token Rotation、`offline_access` 与精确 Token Endpoint CORS。
- 已授权的主叫号码、运营商线路、并发和 CPS 限额，以及目标司法辖区的外呼与录音批准。

从 [`deploy/kubernetes/secrets.example.yaml`](../deploy/kubernetes/secrets.example.yaml) 创建真实 Secret 时，通过 Vault、KMS 或 External Secrets 注入，禁止提交真实值。`CLOUD_PARITY_MASTER_KEY` 与 `CLOUD_PARITY_DISPATCH_METADATA_KEY` 必须是两个不同的 Fernet Key；后者只用于保护 LiveKit Agent Dispatch 中的号码和租约信息。

服务 Token 以文件挂载。Dispatcher 和 Agent 每次请求都重新读取文件，因此 Secret Controller 可以无重启轮换短期 Token。创建项目后，需要将专用 Client Credentials 服务主体以 `worker` 角色加入项目；不要给人类账号分配该角色，也不要让 Agent/Dispatcher 复用 owner、admin 或 member 令牌。`worker` 仅具有呼叫租约执行与会话写入权限。

录音持久引用优先使用 `s3://bucket/object`。如果 Egress/Webhook 必须回传可直接访问的 HTTPS URL，生产环境必须用逗号分隔的 `CLOUD_PARITY_RECORDING_HTTPS_HOSTS` 配置精确主机白名单；HTTP URL、URL 内嵌凭据及未列入白名单的主机会被拒绝。

## 3. 部署 Kubernetes 参考栈

编辑 [`deploy/kubernetes/commercial-stack.yaml`](../deploy/kubernetes/commercial-stack.yaml)：

1. 替换 IdP、项目 ID、LiveKit Agent 名称、录音 bucket/region 和四个镜像标签；`OIDC_CONNECT_SRC` 必须是 Token Endpoint 的源。
2. 按运营商实测值设置项目级并发、Trunk `max_concurrent_calls` 和 `max_calls_per_second`，不要只依赖 Pod 数量。
3. 根据 PostgreSQL 连接预算设置连接池，使 `最大 API Pod 数 × 每 Pod 最大连接数` 留有迁移、运维和故障切换余量。
4. 为跨可用区集群配置节点拓扑、网络策略、私有镜像仓库凭据和入口 TLS；只公开控制面 Service，Dispatcher/Agent 健康端口保留在集群内部。
5. 将 `FORWARDED_ALLOW_IPS` 设置为实际入口代理的精确 IP/CIDR；镜像默认只信任 `127.0.0.1`，生产禁止使用 `*`。

```bash
kubectl apply -f deploy/kubernetes/secrets.generated.yaml
kubectl apply -f deploy/kubernetes/commercial-stack.yaml
kubectl apply -f deploy/kubernetes/network-policy.yaml
kubectl -n qwen-telephony rollout status deployment/cloud-parity
kubectl -n qwen-telephony rollout status deployment/voice-console
kubectl -n qwen-telephony rollout status deployment/telephony-dispatcher
kubectl -n qwen-telephony rollout status deployment/phone-agent
```

控制面多副本会在 PostgreSQL advisory lock 下自动迁移到 Schema v19，并验证历史迁移校验和。任何副本 Ready 失败都应停止发布，不要跳过迁移错误。

## 4. 可观测性与保留任务

- 将 `config/telephony-alerts.yml` 加载到 Prometheus/Alertmanager。
- 抓取控制面 `:8091/metrics` 和 Dispatcher `:9091/metrics` 时发送 `Authorization: Bearer <CLOUD_PARITY_METRICS_TOKEN>`；探针 `/live`、`/ready` 不使用该 Token。
- 使用 Prometheus Operator 时可直接应用 `deploy/kubernetes/observability.prometheus-operator.yaml`，其 ServiceMonitor 从 Secret 读取指标 Bearer Token，并安装关键电话告警。
- `commercial-stack.yaml` 的 Dispatcher/Agent HPA 同时使用 CPU 和外部队列/活跃通话指标。先把 `deploy/kubernetes/prometheus-adapter-values.yaml` 合并到 Prometheus Adapter 配置并确认 External Metrics API 可查询，再发布 HPA；没有 Adapter 时删除两条 External 指标，不能让 HPA 长期处于 `FailedGetExternalMetric`。
- 日志进入集中平台并设置敏感数据检测；业务号码不应出现在控制面返回给 Viewer 的数据、指标或 Dispatch 明文中。
- 确认 `retention-purge` CronJob 每日成功。数据库清理不会自动删除 S3 录音对象，bucket 生命周期和法律保留策略必须使用同一保留期限单独配置。

## 5. 发布验收

先运行自动化回归，再只用已授权号码执行预生产端到端测试：

```bash
export COMMERCIAL_E2E_BASE_URL=https://control.example.com
export COMMERCIAL_E2E_PROJECT_ID=replace-with-project-id
export COMMERCIAL_E2E_BEARER_TOKEN_FILE=/run/secrets/e2e-token
export COMMERCIAL_E2E_DESTINATION_NUMBER=replace-with-authorized-number
export COMMERCIAL_E2E_SOURCE_NUMBER=replace-with-authorized-caller-id
python scripts/commercial-e2e-smoke.py
```

该脚本验证控制面 Ready、有效出局 Trunk、幂等入队和终态收敛。呼入、AMD、转人工、录音回放、故障切换及峰值 1.5 倍压测需要在真实 SIP/LiveKit/IdP 环境执行，并逐项签署 [`commercial-go-live.md`](commercial-go-live.md)。全部 NO-GO 门禁通过前不得对真实客户开放。
