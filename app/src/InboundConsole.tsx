import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  inboundRequest,
  type InboundAgentDetail,
  type InboundAgentSummary,
} from "./inboundApi";
import {
  loadPlatformAuth,
  platformAuthHeaders,
  type PlatformAuthSession,
} from "./platformAuth";

type Project = { id: string; name: string; role: string };
type KnowledgeBase = { id: string; name: string; document_count: number };
type BusinessTool = {
  id: string;
  name: string;
  description: string;
  policy: string;
};
type ContentAsset = { id: string; name: string; kind: string; status: string };
type ConsoleTab =
  | "overview"
  | "conversation"
  | "bindings"
  | "sessions"
  | "versions";

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

const initialConfig = {
  instructions:
    "你是一名耐心、清晰的企业电话服务助手。先理解客户的问题，再给出简洁准确的回答；不确定时明确说明并建议人工跟进。",
  welcome_message: "您好，很高兴为您服务。请问有什么可以帮您？",
  voice: "longanlingxin",
  language: "zh-CN",
  max_duration_seconds: 600,
  recording_mode: "off" as const,
  recording_disclosure: "",
  tools: [],
  knowledge_sources: [],
  content_sources: [],
  avatar_enabled: false,
  avatar_id: "",
};

export default function InboundConsole() {
  const auth = useMemo(() => loadPlatformAuth(), []);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [agents, setAgents] = useState<InboundAgentSummary[]>([]);
  const [selected, setSelected] = useState<InboundAgentDetail | null>(null);
  const [tab, setTab] = useState<ConsoleTab>("overview");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [sessions, setSessions] = useState<
    Array<Record<string, string | number>>
  >([]);
  const [versions, setVersions] = useState<
    Array<Record<string, string | number>>
  >([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [businessTools, setBusinessTools] = useState<BusinessTool[]>([]);
  const [contentAssets, setContentAssets] = useState<ContentAsset[]>([]);
  const [dirty, setDirty] = useState(false);
  const activeProject = projects.find((project) => project.id === projectId);
  const canEdit = ["owner", "admin", "member"].includes(
    activeProject?.role || "",
  );
  const canPublish = ["owner", "admin"].includes(activeProject?.role || "");

  useEffect(() => {
    if (!auth) return;
    platformRequest<{ items: Project[] }>("/api/platform/projects", auth)
      .then(({ items }) => {
        setProjects(items);
        setProjectId(items[0]?.id || "");
      })
      .catch((error) =>
        setMessage(error instanceof Error ? error.message : "无法读取项目"),
      );
  }, [auth]);

  useEffect(() => {
    if (projectId && auth) {
      loadAgents();
      inboundRequest<{ items: KnowledgeBase[] }>(
        `/inbound-api/projects/${projectId}/knowledge-bases`,
        {},
        auth,
      )
        .then((value) => setKnowledgeBases(value.items))
        .catch(() => setKnowledgeBases([]));
      inboundRequest<{ items: BusinessTool[] }>(
        `/inbound-api/projects/${projectId}/tools`,
        {},
        auth,
      )
        .then((value) =>
          setBusinessTools(
            value.items.filter((item) => item.policy !== "deny"),
          ),
        )
        .catch(() => setBusinessTools([]));
      inboundRequest<{ items: ContentAsset[] }>(
        `/inbound-api/projects/${projectId}/content-assets`,
        {},
        auth,
      )
        .then((value) =>
          setContentAssets(
            value.items.filter((item) => item.status === "published"),
          ),
        )
        .catch(() => setContentAssets([]));
    }
  }, [projectId]);

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (!showCreate) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setShowCreate(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [showCreate]);

  async function loadAgents() {
    if (!auth || !projectId) return;
    try {
      const result = await inboundRequest<{ items: InboundAgentSummary[] }>(
        `/inbound-api/projects/${projectId}/agents`,
        {},
        auth,
      );
      setAgents(result.items);
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : "无法读取智能呼入 Agent",
      );
    }
  }

  async function openAgent(agentId: string) {
    if (!auth) return;
    setBusy(true);
    try {
      const detail = await inboundRequest<InboundAgentDetail>(
        `/inbound-api/projects/${projectId}/agents/${agentId}`,
        {},
        auth,
      );
      setSelected(detail);
      setDirty(false);
      setTab("overview");
      setMessage("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "无法打开 Agent");
    } finally {
      setBusy(false);
    }
  }

  async function createAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!auth || !canEdit) return;
    setBusy(true);
    const form = new FormData(event.currentTarget);
    try {
      const agent = await inboundRequest<InboundAgentDetail>(
        `/inbound-api/projects/${projectId}/agents`,
        {
          method: "POST",
          body: JSON.stringify({
            name: form.get("name"),
            description: form.get("description"),
            kind: "enterprise",
            config: initialConfig,
          }),
        },
        auth,
      );
      setShowCreate(false);
      await loadAgents();
      await openAgent(agent.id);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "创建失败");
    } finally {
      setBusy(false);
    }
  }

  async function saveAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!auth || !selected) return;
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const updated = await inboundRequest<InboundAgentDetail>(
        `/inbound-api/projects/${projectId}/agents/${selected.id}`,
        {
          method: "PUT",
          body: JSON.stringify({
            expected_revision: selected.draft_revision,
            name: form.get("name"),
            description: form.get("description"),
            config: {
              ...selected.draft_config,
              instructions: form.get("instructions"),
              welcome_message: form.get("welcome_message"),
              voice: form.get("voice"),
              max_duration_seconds: Number(form.get("max_duration_seconds")),
              knowledge_sources: form.getAll("knowledge_sources").map(String),
              tools: form.getAll("tools").map(String),
            content_sources: form.getAll("content_sources").map(String),
            avatar_enabled: form.get("avatar_enabled") === "on",
            avatar_id: String(form.get("avatar_id") || ""),
            },
          }),
        },
        auth,
      );
      setSelected({ ...updated, bindings: selected.bindings });
      setDirty(false);
      setMessage("草稿已保存，线上版本未受影响。");
      await loadAgents();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function publish() {
    if (!auth || !selected) return;
    if (dirty) {
      setMessage("请先保存草稿，再发布版本。");
      return;
    }
    if (
      !window.confirm(
        `确认发布“${selected.name}”的修订 ${selected.draft_revision}？`,
      )
    )
      return;
    setBusy(true);
    try {
      await inboundRequest(
        `/inbound-api/projects/${projectId}/agents/${selected.id}/publish`,
        {
          method: "POST",
          body: JSON.stringify({ expected_revision: selected.draft_revision }),
        },
        auth,
      );
      await openAgent(selected.id);
      await loadAgents();
      setMessage("新版本已发布。已有入口继续使用原版本，确认后可切换。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "发布失败");
    } finally {
      setBusy(false);
    }
  }

  async function addBinding(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!auth || !selected) return;
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      await inboundRequest(
        `/inbound-api/projects/${projectId}/agents/${selected.id}/bindings`,
        {
          method: "POST",
          body: JSON.stringify({
            entry_type: form.get("entry_type"),
            destination: form.get("destination"),
            trunk_id: form.get("trunk_id"),
          }),
        },
        auth,
      );
      await openAgent(selected.id);
      event.currentTarget.reset();
      setMessage("接入入口已创建，等待 Dispatch 同步确认。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "入口创建失败");
    } finally {
      setBusy(false);
    }
  }

  async function loadTab(next: ConsoleTab) {
    if (
      dirty &&
      next !== tab &&
      !window.confirm("尚有未保存内容，切换页面会丢失这些修改。确认继续？")
    )
      return;
    if (dirty && next !== tab) setDirty(false);
    setTab(next);
    if (!auth || !selected) return;
    try {
      if (next === "sessions") {
        setSessions([]);
        const result = await inboundRequest<{
          items: Array<Record<string, string | number>>;
        }>(
          `/inbound-api/projects/${projectId}/agents/${selected.id}/sessions`,
          {},
          auth,
        );
        setSessions(Array.isArray(result.items) ? result.items : []);
      }
      if (next === "versions" || next === "bindings") {
        setVersions([]);
        const result = await inboundRequest<{
          items: Array<Record<string, string | number>>;
        }>(
          `/inbound-api/projects/${projectId}/agents/${selected.id}/versions`,
          {},
          auth,
        );
        setVersions(Array.isArray(result.items) ? result.items : []);
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "页面数据加载失败");
    }
  }

  async function activateVersion(versionId: string) {
    if (!auth || !selected || !canPublish) return;
    if (!window.confirm("确认将该版本设为当前推荐版本？已有入口不会自动切换。"))
      return;
    setBusy(true);
    try {
      await inboundRequest(
        `/inbound-api/projects/${projectId}/agents/${selected.id}/activate-version`,
        {
          method: "POST",
          body: JSON.stringify({ agent_version_id: versionId }),
        },
        auth,
      );
      await openAgent(selected.id);
      await loadTab("versions");
      setMessage("当前推荐版本已切换。");
    } catch (error) {
      await openAgent(selected.id);
      setMessage(
        error instanceof Error
          ? error.message
          : "推荐版本切换失败，线上配置未改变。",
      );
    } finally {
      setBusy(false);
    }
  }

  async function switchBindingVersion(bindingId: string, versionId: string) {
    if (!auth || !selected || !canPublish) return;
    setBusy(true);
    try {
      await inboundRequest(
        `/inbound-api/projects/${projectId}/agents/${selected.id}/bindings/${bindingId}/version`,
        {
          method: "PUT",
          body: JSON.stringify({ agent_version_id: versionId }),
        },
        auth,
      );
      await openAgent(selected.id);
      await loadTab("bindings");
      setMessage("入口版本已切换，新呼入将使用所选版本。");
    } catch (error) {
      await openAgent(selected.id);
      setMessage(
        error instanceof Error
          ? error.message
          : "入口切版失败，线上配置未改变。",
      );
    } finally {
      setBusy(false);
    }
  }

  async function disableBinding(bindingId: string) {
    if (
      !auth ||
      !selected ||
      !canPublish ||
      !window.confirm("确认停用这个接入入口？新来电将不再进入该 Agent。")
    )
      return;
    setBusy(true);
    try {
      await inboundRequest(
        `/inbound-api/projects/${projectId}/agents/${selected.id}/bindings/${bindingId}`,
        { method: "DELETE" },
        auth,
      );
      await openAgent(selected.id);
      await loadTab("bindings");
      setMessage("接入入口已停用。");
    } catch (error) {
      await openAgent(selected.id);
      setMessage(
        error instanceof Error
          ? error.message
          : "入口停用失败，线上配置未改变。",
      );
    } finally {
      setBusy(false);
    }
  }

  if (!auth) {
    return (
      <main className="inbound-auth-required">
        <img src="/assets/brand/call-logo.svg" alt="云声通" />
        <h1>请先登录控制台</h1>
        <p>智能呼入配置属于企业工作空间，需要登录后访问。</p>
        <a href="/login">前往登录</a>
      </main>
    );
  }

  return (
    <main className="inbound-console">
      <aside className="inbound-sidebar">
        <a href="/app/home" className="inbound-brand">
          <img src="/assets/brand/call-logo.svg" alt="云声通" />
        </a>
        <span className="sidebar-group">业务工作台</span>
        <a href="/app/home">工作台首页</a>
        <a className="active" href="/app/inbound/agents">
          Agent 配置
        </a>
        <a href="/app/inbound/knowledge">知识库</a>
        <a href="/app/inbound/integrations">业务系统</a>
        <a href="/app/inbound/evaluation">体验与评测</a>
        <a href="/app/inbound/content">展示素材</a>
        <span className="sidebar-group">设置</span>
        <a href="/app/dashboard">项目与成员</a>
      </aside>
      <section className="inbound-workspace">
        <header className="inbound-topbar">
          <div>
            <span>智能呼入</span>
            <strong>{selected ? selected.name : "Agent 管理"}</strong>
          </div>
          <select
            aria-label="选择项目"
            value={projectId}
            onChange={(event) => {
              if (
                dirty &&
                !window.confirm(
                  "尚有未保存内容，切换项目会丢失这些修改。确认继续？",
                )
              )
                return;
              setSelected(null);
              setAgents([]);
              setSessions([]);
              setVersions([]);
              setDirty(false);
              setProjectId(event.target.value);
            }}
          >
            {projects.map((project) => (
              <option value={project.id} key={project.id}>
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

        {!selected ? (
          <section className="agent-index">
            <div className="inbound-page-heading">
              <div>
                <span>企业专属 Agent</span>
                <h1>认真接好客户的每一次来电</h1>
                <p>配置对话、发布版本，再绑定企业号码或网页入口。</p>
              </div>
              <button
                disabled={!canEdit}
                title={!canEdit ? "当前角色只有查看权限" : ""}
                onClick={() => setShowCreate(true)}
                type="button"
              >
                新建 Agent
              </button>
            </div>
            <div className="agent-summary">
              <article>
                <strong>{agents.length}</strong>
                <span>全部 Agent</span>
              </article>
              <article>
                <strong>
                  {agents.filter((item) => item.status === "published").length}
                </strong>
                <span>已发布</span>
              </article>
              <article>
                <strong>
                  {agents.reduce(
                    (sum, item) => sum + Number(item.binding_count || 0),
                    0,
                  )}
                </strong>
                <span>启用入口</span>
              </article>
            </div>
            <div className="agent-table">
              <header>
                <span>Agent</span>
                <span>状态</span>
                <span>接入入口</span>
                <span>会话</span>
                <span>更新时间</span>
              </header>
              {agents.map((agent) => (
                <button
                  onClick={() => openAgent(agent.id)}
                  type="button"
                  key={agent.id}
                >
                  <span>
                    <b>{agent.name}</b>
                    <small>{agent.description || "暂无说明"}</small>
                  </span>
                  <span>
                    <i className={`agent-status ${agent.status}`} />
                    {agent.status === "published" ? "已发布" : "草稿"}
                  </span>
                  <span>{agent.binding_count || 0}</span>
                  <span>{agent.session_count || 0}</span>
                  <span>
                    {new Date(agent.updated_at).toLocaleString("zh-CN")}
                  </span>
                </button>
              ))}
              {!agents.length ? (
                <div className="agent-empty">
                  <h2>从第一个接听助手开始</h2>
                  <p>创建后先完成对话配置和测试，再发布到企业号码。</p>
                  <button
                    disabled={!canEdit}
                    title={!canEdit ? "当前角色只有查看权限" : ""}
                    onClick={() => {
                      if (canEdit) setShowCreate(true);
                    }}
                    type="button"
                  >
                    新建 Agent
                  </button>
                </div>
              ) : null}
            </div>
          </section>
        ) : (
          <section className="agent-detail">
            <div className="agent-detail-heading">
              <button
                className="text-button"
                onClick={() => {
                  if (!dirty || window.confirm("尚有未保存内容，确认离开？")) {
                    setDirty(false);
                    setSelected(null);
                  }
                }}
                type="button"
              >
                ← 返回列表
              </button>
              <div>
                <span className={`detail-state ${selected.status}`}>
                  {selected.status === "published"
                    ? "线上版本可用"
                    : "尚未发布"}
                </span>
                <h1>{selected.name}</h1>
                <p>
                  草稿修订 {selected.draft_revision} ·{" "}
                  {selected.bindings.length} 个接入入口
                </p>
              </div>
              <button
                disabled={busy || !canPublish}
                title={!canPublish ? "仅项目所有者或管理员可以发布" : ""}
                onClick={publish}
                type="button"
              >
                发布当前草稿
              </button>
            </div>
            <nav className="agent-tabs">
              {(
                [
                  "overview",
                  "conversation",
                  "bindings",
                  "sessions",
                  "versions",
                ] as ConsoleTab[]
              ).map((item) => (
                <button
                  className={tab === item ? "active" : ""}
                  onClick={() => loadTab(item)}
                  type="button"
                  key={item}
                >
                  {
                    {
                      overview: "概览",
                      conversation: "对话配置",
                      bindings: "号码与网页",
                      sessions: "会话记录",
                      versions: "版本",
                    }[item]
                  }
                </button>
              ))}
            </nav>
            {tab === "overview" || tab === "conversation" ? (
              <div className="recording-policy-readonly" role="status">
                <strong>当前录音策略：</strong>
                {selected.draft_config.recording_mode === "off"
                  ? "不录音"
                  : String(selected.draft_config.recording_mode)}
                {selected.draft_config.recording_disclosure
                  ? ` · ${selected.draft_config.recording_disclosure}`
                  : ""}
                <small>
                  录音策略在合规能力启用前仅供查看，不会由此表单改写。
                </small>
              </div>
            ) : null}
            {tab === "overview" || tab === "conversation" ? (
              <form
                className="agent-editor"
                onChange={() => setDirty(true)}
                onSubmit={saveAgent}
              >
                <section>
                  <h2>基本信息</h2>
                  <p>这些信息只用于企业内部识别。</p>
                </section>
                <div className="editor-fields">
                  <label>
                    Agent 名称
                    <input
                      name="name"
                      defaultValue={selected.name}
                      disabled={!canEdit}
                      required
                    />
                  </label>
                  <label>
                    用途说明
                    <input
                      name="description"
                      defaultValue={selected.description}
                      disabled={!canEdit}
                    />
                  </label>
                </div>
                <section>
                  <h2>对话与声音</h2>
                  <p>使用自然、明确的角色说明，不要在提示词中写入密钥。</p>
                </section>
                <div className="editor-fields">
                  <label className="full">
                    角色与目标
                    <textarea
                      name="instructions"
                      defaultValue={selected.draft_config.instructions}
                      disabled={!canEdit}
                      rows={8}
                      required
                    />
                  </label>
                  <label className="full">
                    欢迎语
                    <input
                      name="welcome_message"
                      defaultValue={selected.draft_config.welcome_message}
                      disabled={!canEdit}
                      required
                    />
                  </label>
                  <label>
                    声音
                    <select
                      name="voice"
                      defaultValue={selected.draft_config.voice}
                      disabled={!canEdit}
                    >
                      <option value="longanlingxin">龙安聆心（温暖）</option>
                      <option value="longanqian">龙安浅（自然）</option>
                      <option value="longanfengyue">龙安风悦（明快）</option>
                      <option value="loongmary">Loong Mary</option>
                      <option value="loongjohn">Loong John</option>
                    </select>
                  </label>
                  <label>
                    最长通话
                    <select
                      name="max_duration_seconds"
                      defaultValue={selected.draft_config.max_duration_seconds}
                      disabled={!canEdit}
                    >
                      <option value="300">5 分钟</option>
                      <option value="600">10 分钟</option>
                      <option value="900">15 分钟</option>
                      <option value="1800">30 分钟</option>
                    </select>
                  </label>
                </div>
                <section>
                  <h2>企业知识</h2>
                  <p>发布后，新会话只会检索勾选的知识库。</p>
                </section>
                <div className="knowledge-bindings">
                  {knowledgeBases.map((base) => (
                    <label key={base.id}>
                      <input
                        type="checkbox"
                        name="knowledge_sources"
                        value={base.id}
                        defaultChecked={selected.draft_config.knowledge_sources.includes(
                          base.id,
                        )}
                        disabled={!canEdit}
                      />
                      <span>
                        <strong>{base.name}</strong>
                        <small>{base.document_count} 份文档</small>
                      </span>
                    </label>
                  ))}
                  {!knowledgeBases.length ? (
                    <p>
                      尚无知识库。<a href="/app/inbound/knowledge">前往创建</a>
                    </p>
                  ) : null}
                </div>
                <section>
                  <h2>业务工具白名单</h2>
                  <p>模型只能看到这里勾选的工具；写操作仍需执行策略允许。</p>
                </section>
                <div className="knowledge-bindings">
                  {businessTools.map((tool) => (
                    <label key={tool.id}>
                      <input
                        type="checkbox"
                        name="tools"
                        value={tool.id}
                        defaultChecked={selected.draft_config.tools.includes(
                          tool.id,
                        )}
                        disabled={!canEdit}
                      />
                      <span>
                        <strong>{tool.name}</strong>
                        <small>
                          {tool.policy === "confirm"
                            ? "调用前确认"
                            : "自动允许"}{" "}
                          · {tool.description}
                        </small>
                      </span>
                    </label>
                  ))}
                  {!businessTools.length ? (
                    <p>
                      尚无可用工具。
                      <a href="/app/inbound/integrations">前往注册</a>
                    </p>
                  ) : null}
                </div>
                <section>
                  <h2>展示素材白名单</h2>
                  <p>只有审核发布并在此勾选的素材可以向客户展示。</p>
                </section>
                <div className="knowledge-bindings">
                  {contentAssets.map((asset) => (
                    <label key={asset.id}>
                      <input
                        type="checkbox"
                        name="content_sources"
                        value={asset.id}
                        defaultChecked={(selected.draft_config.content_sources || []).includes(asset.id)}
                        disabled={!canEdit}
                      />
                      <span><strong>{asset.name}</strong><small>{asset.kind}</small></span>
                    </label>
                  ))}
                  {!contentAssets.length ? <p>尚无已发布素材。<a href="/app/inbound/content">前往管理</a></p> : null}
                </div>
                <footer>
                  <span>保存草稿不会影响正在服务的线上版本。</span>
                  <button disabled={busy || !canEdit} type="submit">
                    {busy ? "保存中…" : "保存草稿"}
                  </button>
                </footer>
              </form>
            ) : null}
            {tab === "bindings" ? (
              <div className="binding-layout">
                <form className="binding-form" onSubmit={addBinding}>
                  <h2>添加接入入口</h2>
                  <label>
                    入口类型
                    <select name="entry_type" disabled={!canPublish}>
                      <option value="sip_did">企业电话号码</option>
                      <option value="web">网页语音入口</option>
                    </select>
                  </label>
                  <label>
                    号码或入口标识
                    <input
                      name="destination"
                      disabled={!canPublish}
                      placeholder="例如 +8613800000000"
                      required
                    />
                  </label>
                  <label>
                    LiveKit Trunk ID
                    <input
                      name="trunk_id"
                      disabled={!canPublish}
                      placeholder="电话入口必填"
                    />
                  </label>
                  <button
                    disabled={
                      !selected.active_version_id || busy || !canPublish
                    }
                    type="submit"
                  >
                    创建入口
                  </button>
                  <small>
                    创建前必须先发布 Agent。Dispatch 同步成功后才可正式接听。
                  </small>
                </form>
                <section className="binding-list">
                  <h2>当前入口</h2>
                  {selected.bindings.map((binding) => (
                    <article key={binding.id}>
                      <div>
                        <b>
                          {binding.entry_type === "sip_did"
                            ? "电话号码"
                            : "网页入口"}
                        </b>
                        <strong>{binding.destination}</strong>
                      </div>
                      <span
                        className={
                          binding.dispatch_rule_id ? "healthy" : "pending"
                        }
                      >
                        {binding.dispatch_rule_id ? "运行正常" : "等待同步"}
                      </span>
                      <label className="binding-version">
                        运行版本
                        <select
                          aria-label={`选择 ${binding.destination} 的版本`}
                          value={binding.agent_version_id}
                          disabled={!canPublish}
                          onChange={(event) =>
                            switchBindingVersion(binding.id, event.target.value)
                          }
                        >
                          {versions.map((version) => (
                            <option
                              value={String(version.id)}
                              key={String(version.id)}
                            >
                              修订 {version.revision}
                            </option>
                          ))}
                        </select>
                      </label>
                      <button
                        className="binding-disable"
                        disabled={!canPublish}
                        onClick={() => disableBinding(binding.id)}
                        type="button"
                      >
                        停用入口
                      </button>
                    </article>
                  ))}
                  {!selected.bindings.length ? (
                    <p className="empty-copy">尚未绑定号码或网页入口。</p>
                  ) : null}
                </section>
              </div>
            ) : null}
            {tab === "sessions" ? (
              <div className="simple-data-panel">
                <h2>会话记录</h2>
                <table>
                  <thead>
                    <tr>
                      <th>开始时间</th>
                      <th>入口</th>
                      <th>状态</th>
                      <th>时长</th>
                      <th>结束原因</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sessions.map((item) => (
                      <tr key={String(item.id)}>
                        <td>
                          {new Date(String(item.started_at)).toLocaleString(
                            "zh-CN",
                          )}
                        </td>
                        <td>{item.entry_type}</td>
                        <td>{item.status}</td>
                        <td>{item.duration_seconds || 0} 秒</td>
                        <td>{item.termination_reason || "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!sessions.length ? (
                  <p className="empty-copy">还没有呼入会话。</p>
                ) : null}
              </div>
            ) : null}
            {tab === "versions" ? (
              <div className="simple-data-panel">
                <h2>发布版本</h2>
                {versions.map((version) => (
                  <article className="version-row" key={String(version.id)}>
                    <span>修订 {version.revision}</span>
                    <code>{String(version.config_sha256).slice(0, 12)}</code>
                    <time>
                      {new Date(String(version.published_at)).toLocaleString(
                        "zh-CN",
                      )}
                    </time>
                    <button
                      disabled={
                        !canPublish ||
                        String(version.id) === selected.active_version_id
                      }
                      onClick={() => activateVersion(String(version.id))}
                      type="button"
                    >
                      {String(version.id) === selected.active_version_id
                        ? "当前推荐"
                        : "设为推荐"}
                    </button>
                  </article>
                ))}
                {!versions.length ? (
                  <p className="empty-copy">尚未发布版本。</p>
                ) : null}
              </div>
            ) : null}
          </section>
        )}
      </section>

      {showCreate && canEdit ? (
        <div
          className="inbound-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-agent-title"
        >
          <form onSubmit={createAgent}>
            <button
              aria-label="关闭新建 Agent 对话框"
              className="modal-close"
              onClick={() => setShowCreate(false)}
              type="button"
            >
              ×
            </button>
            <span>新建企业 Agent</span>
            <h2 id="create-agent-title">从清晰的服务目标开始</h2>
            <p>创建后可以继续调整对话、声音和接入入口。</p>
            <label>
              Agent 名称
              <input
                name="name"
                placeholder="例如：客户服务助手"
                autoFocus
                required
              />
            </label>
            <label>
              用途说明
              <textarea
                name="description"
                placeholder="说明它负责处理哪些来电"
                rows={3}
              />
            </label>
            <footer>
              <button onClick={() => setShowCreate(false)} type="button">
                取消
              </button>
              <button disabled={busy || !canEdit} type="submit">
                创建并配置
              </button>
            </footer>
          </form>
        </div>
      ) : null}
    </main>
  );
}
