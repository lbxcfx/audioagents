# 双向语音系统商用上线门禁

本文把“应用代码完成”和“目标环境获准上线”分开。任何标为 **NO-GO** 的项目未通过时，不得向真实客户发起外呼或开放公网呼入。

## 1. 身份与安全（NO-GO）

- [ ] 生产使用 `CLOUD_PARITY_AUTH_MODE=oidc` 或受控可信代理，不允许开发身份头。
- [ ] 企业 IdP 的 Issuer、Audience、JWKS、算法白名单、Client Credentials 和项目成员映射已用真实租户联调。
- [ ] `CLOUD_PARITY_MASTER_KEY`、独立的 `CLOUD_PARITY_DISPATCH_METADATA_KEY`、`CLOUD_PARITY_PHONE_HASH_KEY`、`CLOUD_PARITY_METRICS_TOKEN` 和运营商凭据来自 KMS/Vault，均未写入仓库或日志。
- [ ] Dispatcher 与 Agent 使用可轮换的文件挂载短期 Service Token；确认 Token 轮换不要求重启进程，旧 Token 被 IdP 拒绝。
- [ ] Access Token 自撤销、过期、错误 Audience/Issuer/算法、JWKS 轮换和被撤销 Token 重放测试全部通过。
- [ ] 公网入口已配置精确 CORS、TLS、WAF；可信代理会删除客户端伪造的身份头。
- [ ] `CLOUD_PARITY_API_REQUESTS_PER_MINUTE` 根据压测设定，多个 API 副本共用 PostgreSQL 限流状态。

## 2. 数据与高可用（NO-GO）

- [ ] PostgreSQL 使用 TLS 和最小权限账号，连接池总量不超过数据库连接预算。
- [ ] 数据库部署高可用、自动备份和 PITR，并完成一次恢复演练；记录 RPO：____，RTO：____。
- [ ] Schema 迁移 v19 在预生产多副本并发启动时通过，迁移校验和无漂移。
- [ ] 明确 CDR、同意证据、审计、转写和录音的保存期限、删除流程、数据驻留区域和访问审计。
- [ ] 每日 Retention CronJob 正常运行；完成联系人删除、终态呼叫清理、录音对象生命周期和法律保留例外的演练。

## 3. 运营商与电话链路（NO-GO）

- [ ] 已取得号码和线路的合法使用权，运营商确认主叫号码、地域、TPS/CPS、并发、日限额和紧急停呼方式。
- [ ] LiveKit Inbound/Outbound Trunk、Dispatch Rule、Agent 名称和 SIP TLS/SRTP 策略与预生产一致。
- [ ] 用已授权号码验证：外呼接通/拒接/忙线/无人接、呼入、用户挂断、AI 挂断、重复请求、服务重启和 Provider 超时。
- [ ] Webhook 签名、重复投递、乱序投递、SIP Call ID 关联和 CDR 收敛测试通过。
- [ ] 响铃超时和最大通话时长已经按资费与业务设定，验证超时会释放并发容量。
- [ ] `dispatching`/`dialing` 时崩溃只进入 `reconciling`，已证明不会产生重复外呼。
- [ ] 人工冷转接目标全部在白名单内；AI 满载时的呼入拒绝或溢出转接符合业务预期。
- [ ] LiveKit Egress 服务和合规对象存储已部署；验证录音告知先于录音启动、双声道文件可回放、Egress 失败会按强制录音策略终止呼叫。
- [ ] AMD 已用真人、IVR、语音信箱、不可达提示和不确定场景验收；分类与业务 disposition 正确落入 CDR。

## 4. 法务与外呼合规（NO-GO）

- [ ] 法务按呼叫来源地、目的地和业务类型批准外呼许可、拨打时段、节假日、同意证据、DNC 和单号码频控规则。
- [ ] 拒呼名单和撤销同意的上游数据源、同步延迟、申诉/删除流程和责任人已确定。
- [ ] 生产策略默认拒绝缺失同意的外呼；确认入队与领取任务两次合规检查均开启。
- [ ] 如录音/转写，开场告知、双向同意、敏感字段脱敏、加密、访问控制和保存期限已经法务批准。
- [ ] 如使用 AMD/语音信箱留言，留言内容、身份披露和自动化呼叫规则已经批准。

## 5. 容量、可观测性与故障演练（NO-GO）

- [ ] 以计划峰值的至少 1.5 倍完成 API 入队、Dispatcher 抢占、Agent 并发、数据库连接和 SIP CPS 压测。
- [ ] `/api/platform/health/live`、`/api/platform/health/ready`、Dispatcher `/live`/`/ready` 已接入编排探针。
- [ ] 控制面与 Dispatcher `/metrics` 只在受控网络暴露，并使用 `CLOUD_PARITY_METRICS_TOKEN` Bearer；`config/telephony-alerts.yml` 已加载到 Prometheus/Alertmanager。
- [ ] 队列积压/停滞、过期租约、长时间对账、Webhook 不匹配和转接失败告警能通知值班人员。
- [ ] 演练 API/Dispatcher/Agent 单副本崩溃、数据库主从切换、LiveKit 暂时不可用和运营商故障；确认恢复后不重复拨号。
- [ ] 值班手册包含停呼开关、DNC 紧急导入、失败任务处置、凭据轮换、回滚和事件通报。
- [ ] 使用 `scripts/commercial-e2e-smoke.py` 从预生产发起一通授权外呼并成功收敛；另外从授权号码完成一通真实呼入、转人工和录音回放验收。

## 6. 发布批准

发布记录必须包含：版本/提交：____；Schema：`18`；测试环境：____；运营商：____；LiveKit/Egress 项目：____；容量上限：____；法务批准人：____；技术批准人：____；回滚版本：____。

全部 NO-GO 项通过后才可进入小流量灰度。灰度期间限制租户、号码段、时间窗和并发，观察队列、失败率、接通率、转接率、合规拦截和 CDR 匹配后再逐级放量。
