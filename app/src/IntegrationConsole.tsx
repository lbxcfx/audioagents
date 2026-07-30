import { FormEvent, useEffect, useMemo, useState } from "react";
import { inboundRequest } from "./inboundApi";
import {
  loadPlatformAuth,
  platformAuthHeaders,
  type PlatformAuthSession,
} from "./platformAuth";

type Project = { id: string; name: string; role: string };
type Connection = {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  status: string;
  has_credentials: boolean;
};
type Tool = {
  id: string;
  connection_id: string;
  name: string;
  description: string;
  method: string;
  path: string;
  policy: string;
  status: string;
};
type McpTool = {
  name: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
};
const platformBase =
  import.meta.env.VITE_PLATFORM_API_BASE ||
  (import.meta.env.DEV ? "http://127.0.0.1:8091" : window.location.origin);
async function platformRequest<T>(
  path: string,
  auth: PlatformAuthSession,
): Promise<T> {
  const response = await fetch(`${platformBase}${path}`, {
    headers: platformAuthHeaders(auth),
  });
  if (!response.ok) throw new Error(`项目服务请求失败（${response.status}）`);
  return response.json() as Promise<T>;
}

export default function IntegrationConsole() {
  const auth = useMemo(() => loadPlatformAuth(), []);
  const [projects, setProjects] = useState<Project[]>([]),
    [projectId, setProjectId] = useState("");
  const [connections, setConnections] = useState<Connection[]>([]),
    [tools, setTools] = useState<Tool[]>([]);
  const [discovered, setDiscovered] = useState<Record<string, McpTool[]>>({});
  const [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false);
  const canManage = ["owner", "admin"].includes(
    projects.find((item) => item.id === projectId)?.role || "",
  );
  useEffect(() => {
    if (!auth) return;
    platformRequest<{ items: Project[] }>("/api/platform/projects", auth)
      .then(({ items }) => {
        setProjects(items);
        setProjectId(items[0]?.id || "");
      })
      .catch((error) => setMessage(String(error)));
  }, [auth]);
  useEffect(() => {
    if (auth && projectId) void reload();
  }, [projectId]);
  async function reload() {
    if (!auth) return;
    try {
      const [connectionValue, toolValue] = await Promise.all([
        inboundRequest<{ items: Connection[] }>(
          `/inbound-api/projects/${projectId}/tool-connections`,
          {},
          auth,
        ),
        inboundRequest<{ items: Tool[] }>(
          `/inbound-api/projects/${projectId}/tools`,
          {},
          auth,
        ),
      ]);
      setConnections(connectionValue.items);
      setTools(toolValue.items);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "业务系统加载失败");
    }
  }
  async function createConnection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!auth || !canManage) return;
    const form = new FormData(event.currentTarget);
    const headerName = String(form.get("header_name") || "").trim(),
      headerValue = String(form.get("header_value") || "");
    setBusy(true);
    try {
      await inboundRequest(
        `/inbound-api/projects/${projectId}/tool-connections`,
        {
          method: "POST",
          body: JSON.stringify({
            name: form.get("name"),
            kind: form.get("kind"),
            base_url: form.get("base_url"),
            headers: headerName ? { [headerName]: headerValue } : {},
          }),
        },
        auth,
      );
      event.currentTarget.reset();
      await reload();
      setMessage("连接已保存，密钥只在服务端加密存储。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "连接创建失败");
    } finally {
      setBusy(false);
    }
  }
  async function createTool(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!auth || !canManage) return;
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const inputSchema = JSON.parse(String(form.get("input_schema") || "{}"));
      await inboundRequest(
        `/inbound-api/projects/${projectId}/tools`,
        {
          method: "POST",
          body: JSON.stringify({
            connection_id: form.get("connection_id"),
            name: form.get("name"),
            description: form.get("description"),
            method: form.get("method"),
            path: form.get("path"),
            policy: form.get("policy"),
            timeout_seconds: 10,
            input_schema: inputSchema,
          }),
        },
        auth,
      );
      event.currentTarget.reset();
      await reload();
      setMessage("工具已创建，可在 Agent 草稿中加入白名单。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "工具创建失败");
    } finally {
      setBusy(false);
    }
  }
  async function discover(connectionId: string) {
    if (!auth) return;
    setBusy(true);
    try {
      const value = await inboundRequest<{ items: McpTool[] }>(
        `/inbound-api/projects/${projectId}/tool-connections/${connectionId}/discover`,
        { method: "POST" },
        auth,
      );
      setDiscovered((current) => ({ ...current, [connectionId]: value.items }));
      setMessage(`发现 ${value.items.length} 个 MCP 工具，可按其契约注册到白名单。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "MCP 工具发现失败");
    } finally {
      setBusy(false);
    }
  }
  if (!auth)
    return (
      <main className="inbound-auth-required">
        <h1>请先登录控制台</h1>
        <a href="/login">前往登录</a>
      </main>
    );
  return (
    <main className="inbound-console">
      <aside className="inbound-sidebar">
        <a href="/app/home" className="inbound-brand">
          <img src="/assets/brand/call-logo.svg" alt="云声通" />
        </a>
        <span className="sidebar-group">智能客服</span>
        <a href="/app/inbound/agents">Agent 配置</a>
        <a href="/app/inbound/knowledge">知识库</a>
        <a className="active" href="/app/inbound/integrations">
          业务系统
        </a>
        <a href="/app/inbound/evaluation">体验与评测</a>
        <a href="/app/inbound/content">展示素材</a>
      </aside>
      <section className="inbound-workspace">
        <header className="inbound-topbar">
          <div>
            <span>智能客服</span>
            <strong>业务系统与工具</strong>
          </div>
          <select
            value={projectId}
            aria-label="选择项目"
            onChange={(event) => setProjectId(event.target.value)}
          >
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </header>
        {message ? (
          <div className="inbound-notice" role="status">
            {message}
            <button onClick={() => setMessage("")} type="button">
              ×
            </button>
          </div>
        ) : null}
        <div className="knowledge-page">
          <div className="inbound-page-heading">
            <div>
              <span>CONTROLLED ACTIONS</span>
              <h1>把业务动作关进白名单</h1>
              <p>
                浏览器永远不会读取已保存的 Header 或密钥；写操作默认需要确认。
              </p>
            </div>
          </div>
          <div className="integration-grid">
            <form onSubmit={createConnection}>
              <h2>添加连接</h2>
              <label>
                名称
                <input name="name" required />
              </label>
              <label>
                类型
                <select name="kind">
                  <option value="http_api">企业 HTTP API</option>
                  <option value="mcp_streamable_http">
                    MCP Streamable HTTP
                  </option>
                </select>
              </label>
              <label>
                服务地址
                <input
                  name="base_url"
                  type="url"
                  placeholder="https://crm.example.com"
                  required
                />
              </label>
              <label>
                认证 Header
                <input name="header_name" placeholder="Authorization" />
              </label>
              <label>
                Header 密钥
                <input
                  name="header_value"
                  type="password"
                  autoComplete="new-password"
                />
              </label>
              <button disabled={busy || !canManage}>保存连接</button>
              {connections
                .filter((item) => item.kind === "mcp_streamable_http")
                .map((item) => (
                  <div key={item.id} className="mcp-discovery">
                    <button type="button" disabled={busy} onClick={() => discover(item.id)}>
                      发现 {item.name} 的工具
                    </button>
                    {discovered[item.id]?.map((tool) => (
                      <small key={tool.name} title={JSON.stringify(tool.inputSchema || {})}>
                        {tool.name}：{tool.description || "无说明"}
                      </small>
                    ))}
                  </div>
                ))}
            </form>
            <form onSubmit={createTool}>
              <h2>注册白名单工具</h2>
              <label>
                连接
                <select name="connection_id" required>
                  {connections.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                工具名称
                <input
                  name="name"
                  pattern="[a-zA-Z_][a-zA-Z0-9_]*"
                  placeholder="query_order"
                  required
                />
              </label>
              <label>
                模型可见说明
                <textarea name="description" rows={3} required />
              </label>
              <label>
                方法
                <select name="method">
                  <option>GET</option>
                  <option>POST</option>
                  <option>PUT</option>
                  <option>PATCH</option>
                  <option>DELETE</option>
                </select>
              </label>
              <label>
                固定路径
                <input name="path" placeholder="/v1/orders/query；MCP 可填 /" required />
              </label>
              <label>
                参数 JSON Schema
                <textarea
                  name="input_schema"
                  rows={5}
                  defaultValue={'{"type":"object","properties":{},"additionalProperties":false}'}
                  required
                />
              </label>
              <label>
                策略
                <select name="policy">
                  <option value="confirm">调用前确认</option>
                  <option value="auto">自动允许（只读）</option>
                  <option value="deny">禁止</option>
                </select>
              </label>
              <button disabled={busy || !canManage || !connections.length}>
                注册工具
              </button>
            </form>
          </div>
          <section className="tool-list">
            <h2>已注册工具</h2>
            {tools.map((tool) => (
              <article key={tool.id}>
                <div>
                  <strong>{tool.name}</strong>
                  <small>{tool.description}</small>
                </div>
                <code>
                  {tool.method} {tool.path}
                </code>
                <span className={`tool-policy ${tool.policy}`}>
                  {tool.policy === "confirm"
                    ? "需确认"
                    : tool.policy === "auto"
                      ? "自动"
                      : "禁止"}
                </span>
              </article>
            ))}
            {!tools.length ? (
              <p className="empty-copy">尚未注册工具。</p>
            ) : null}
          </section>
        </div>
      </section>
    </main>
  );
}
