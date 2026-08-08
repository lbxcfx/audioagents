# AudioAgents Docker Compose 部署包

这个目录把外呼链路打包为一个可重复部署的 Compose 项目，包括：

- PostgreSQL、Redis、MinIO
- LiveKit Server、LiveKit SIP、LiveKit Egress
- AudioAgent 控制面、任务调度器、语音 Worker
- Hermes Agent、微信扫码登录和 AudioAgent 插件
- 项目、Worker、外呼线路映射、通话策略的自动初始化

它不会读取、停止或修改仓库根目录当前运行的开发服务。Compose 使用独立项目名和独立命名卷；只有在同一台机器真正启动时，才需要处理端口占用。

## 1. 部署要求

- Linux x86_64 或 arm64
- Docker Engine 24+
- Docker Compose v2
- 建议至少 4 核 CPU、8 GB 内存
- 可访问 DeepSeek、DashScope、微信 iLink 和 SIP 服务商
- 公网服务器需要开放：
  - `7881/tcp`：LiveKit RTC TCP
  - `7882/udp`：LiveKit RTC UDP
  - `5065/tcp,udp`：SIP（可通过 `.env` 修改）
  - `10000-10100/udp`：SIP RTP（可通过 `.env` 修改）

## 2. 快速部署

在仓库根目录执行：

```bash
cd server
./scripts/prepare.sh
```

编辑 `server/.env`，至少填写：

```dotenv
PUBLIC_IP=服务器公网IP
DASHSCOPE_API_KEY=DashScope密钥
DEEPSEEK_API_KEY=DeepSeek密钥
LIVEKIT_SIP_TRUNK_ID=已在LiveKit创建的外呼Trunk ID
AUDIOAGENT_SOURCE_NUMBER=外显号码
```

SIP 服务商需要 REGISTER 时，再填写 `QWEN_SIP_REGISTER_*`。所有线路信息都保留在 `.env` 中，不会写进镜像。

启动：

```bash
./scripts/deploy.sh
./scripts/health.sh
```

默认语音管线是当前已验证的 `realtime + server_vad`，因此构建时不下载 Hugging Face 的经典管线模型。如果切换为 `classic`，请设置 `DOWNLOAD_LIVEKIT_MODELS=true`，并确保构建主机能够访问 `HF_ENDPOINT`。

首次连接微信：

```bash
./scripts/wechat-login.sh
```

终端显示二维码后，用需要连接的微信扫码并在手机确认。登录凭据保存在该 Compose 项目的 `hermes-data` 卷中，正常重启、升级容器不会丢失。

## 3. 自动初始化内容

`bootstrap` 一次性容器会以幂等方式完成：

- 创建外呼项目；
- 创建调度 Worker 权限；
- 将外部 `LIVEKIT_SIP_TRUNK_ID` 映射为 AudioAgent 线路；
- 允许全天、全星期外呼；
- 取消每日拨打次数限制；
- 保持录音始终开启，但不播放录音提示语；
- 启用 Hermes AudioAgent 插件和微信结果回传；
- 配置 DeepSeek 为 Hermes 模型；
- 将动态生成的项目 ID、线路 ID 写入独立持久化卷。

重复执行 `docker compose up -d` 不会重复创建项目，也不会重复建立线路。

## 4. 部署不同微信账号

每个独立 Compose 项目拥有自己的 Hermes 数据卷。部署第二个微信账号时，使用另一份 `server/.env` 和不同项目名，例如：

```dotenv
COMPOSE_PROJECT_NAME=audioagents-customer-b
```

然后启动并重新扫码。不要让两个 Hermes 实例共用同一个 `hermes-data` 卷，也不要用两个实例同时登录同一个微信/iLink token。

如果多个实例部署在同一台主机，还必须为第二套修改 LiveKit、SIP、RTP、控制面和 MinIO 的宿主机端口。大规模部署更建议共享 AudioAgent/LiveKit 基础设施，只为每个微信账号单独部署 Hermes。

## 5. 常用运维命令

```bash
# 状态与健康检查
./scripts/health.sh

# 所有日志
./scripts/logs.sh

# 仅查看语音和微信日志
./scripts/logs.sh voice-agent hermes

# 停止服务，但保留数据库、录音和微信登录状态
./scripts/stop.sh

# 重新启动
docker compose up -d

# 更新本地代码后重新构建三个 AudioAgent 镜像
docker compose build control-plane dispatcher voice-agent
docker compose up -d
```

不要执行 `docker compose down -v`，该命令会删除数据库、录音索引、动态项目配置和微信登录状态。

## 6. 数据与备份

必须纳入备份的命名卷：

- `postgres-data`：项目、任务、通话结果和线路映射
- `minio-data`：通话录音
- `hermes-data`：微信凭据、Hermes 配置、会话和回传去重状态
- `redis-data`：LiveKit/SIP 运行状态
- `egress-backup`：录音上传失败时的本地备份

升级前至少备份 PostgreSQL、MinIO 和 Hermes 三部分。镜像中不包含任何业务数据或密钥。

## 7. 安全边界

快速部署模式将控制面限制绑定到 `127.0.0.1`，并只在 Compose 内部网络向 Hermes 和 Worker开放。不要直接把控制面端口暴露到公网。

如果需要通过公网访问控制台，应在前面增加具备 TLS 和身份认证的反向代理，并把控制面切换为 OIDC 或可信代理认证。`server/.env` 权限由准备脚本设置为 `0600`，不得提交到 Git。

## 8. 当前工程共存

这个部署包本身不会影响当前服务。当前机器已有开发版 LiveKit、SIP、数据库和 AudioAgent 进程时，不要直接执行 `deploy.sh`，因为默认宿主机端口相同。可选择：

1. 在新服务器使用默认端口部署；或
2. 在 `server/.env` 中配置一套未占用端口做并行验证。

外呼切换完成前，不需要停止现有 systemd 服务。
