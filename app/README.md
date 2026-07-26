# 商用语音运营前端

该 Vite/React 工程连接 `/api/platform` 商用控制面，覆盖企业 OIDC Authorization Code + PKCE/Token 刷新、租户项目、实时呼入/外呼、联系人游标分页与批量导入、活动、DNC/同意证据、SIP Trunk、人工转接、CDR、短时签名录音访问、并发/CPS、Analytics、Insights/Console、Agent Builder、构建部署/回滚、Secrets、Inference、Embed、项目 RBAC、审计与数据保留。

生产构建默认只展示真实 API 闭环模块，并强制使用同源 `/api`，避免把访问令牌发送到浏览器本地配置的任意地址。历史复刻页面和 API 地址覆盖仅用于开发诊断，除非明确设置 `VITE_ENABLE_LEGACY_REPLICA=true` 或 `VITE_ALLOW_API_OVERRIDE=true`，否则不会进入生产包的可见导航。

```bash
npm install
npm run dev
npm run build
```

本地开发可在登录页使用 `X-User-ID`。生产构建必须配置企业 IdP，且不应启用开发身份：

```bash
VITE_OIDC_AUTHORIZATION_ENDPOINT=https://idp.example.com/oauth2/authorize
VITE_OIDC_TOKEN_ENDPOINT=https://idp.example.com/oauth2/token
VITE_OIDC_CLIENT_ID=voice-console
VITE_OIDC_SCOPE="openid profile email offline_access"
VITE_OIDC_REDIRECT_URI=https://voice.example.com/
npm run build
```

IdP 客户端必须启用 Authorization Code + PKCE、精确 Redirect URI、Token Endpoint CORS 和 SPA Refresh Token Rotation。Access/Refresh Token 仅保存在当前标签页的 `sessionStorage`；前端会在到期前刷新 Access Token，安全退出时调用控制面的 Token Revocation API。若 IdP 不签发 Refresh Token，令牌到期后会要求用户重新登录，不会用失效令牌继续请求。

录音对象使用 `s3://` 时，控制面通过 `QWEN_RECORDING_S3_ENDPOINT` 和专用只读 S3 签名凭据签发最长 1 小时、前端默认 5 分钟的临时 GET URL；对象存储长期密钥不会返回浏览器。Egress 写入权限可独立使用工作负载身份，不能与控制面的只读签名凭据混用。
